"""Engineering acceptance criteria of a P0 run, as pure functions over the run's material (task c2b3a).

The contract's engineering_acceptance has three criteria (contract.ENGINEERING_CRITERIA) and one rule: all must hold;
outcome metrics are never judged (plan section 10). Each criterion here returns a JSON-ready result whose status is
passed, failed or incomplete. engineering_status combines exactly those three: any failed is failed; else a criterion
missing or extra, a status outside the three, or any incomplete is incomplete; else passed. Nothing here reads a
clock, writes a file or decides anything for a strategy, and nothing here is a run's acceptance: the runner (task
c2b3b) wires the criteria into a run and binds the result.

known_accounting_answers(): plan section 9's fixtures, rounding cases that tell the floor of a win from rounding to
the nearest penny and the two commission roundings apart (per_market_ceil_penny, per_market_half_up_penny), two
variants in one market and every unresolved case of the draft (void, dead heat, own and other runner removed after
the decision, result revised after close, missing BSP), each settled by accounting.settle under a fixed synthetic
rule set (commission 2%, per_market_ceil_penny unless the case names the other rounding) and compared with
constants written here by hand (KAT_CASES), never derived from the implementation being checked: per bet its status,
full reasons, settlement case and gross pence; per (variant, market) its commission, net and unresolved stake; and
the total net. A case whose settlement does not hold exactly one result per bet and per (variant, market), or that
cannot be settled or compared, fails. market_void False appears only in these synthetic
fixtures (real BASIC outcomes keep it unknown).

issuance_exposure_reconciliation(rules, records, decisions, ledger_bytes, bets, settlement) is failed when
cohort.issuance_counts refuses the decisions (one outcome per row for each contract variant, intents, reasons,
draws, at most one issue per market, no duplicate market or decision id); when a row holds other variants than the
contract's, a decision that is not eligible holds rows, or a row is not its decision's (market, plan, axis, anchor,
decision time, and the decision id recomputed from them); when the decisions' (plan, market id) set is not the input
records' (each record once); when the persisted issuance ledger bytes are not the decisions' canonical ledger
(cohort.ledger_lines); when the issued rows, keyed by (variant, decision_id), differ from the bets given to
accounting or from the settled bets in key, market, runner or stake (a duplicate, missing or swapped bet cannot
cancel out in a total); when the settlement was made for other variants or cannot be measured per variant; or when,
per variant, issued is not resolved + unresolved bets or issued stake is not resolved + unresolved stake. Otherwise
a variant that issued nothing leaves it incomplete (0 = 0 is no evidence).

prefix_invariance, by primary-source prefix consistency (Astra's A1-A6): check_prefix for one record, PrefixTally
over a run. check_prefix first checks the decision against the record alone (stage record): a decision whose market,
plan, class, reason, anchor or decision time t differs from the record's own classification (cohort.classify) is
failed, and so is one for a record without a view, or whose anchor is published after t, that differs in anything
from deciding that record again (such a record is never replayed, so it has no rows); otherwise such a record is not
a candidate (counted apart, by reason). For every other record (eligible, excluded, unclassifiable, stopped; stage
prefix), the record's primary member, checked against the manifest as replay checks it (replay._member and
safe_target; a missing or unreadable file is ReplayProblem), is streamed again: every compressed byte is hashed and
must equal the manifest's sha256 and size (ReplayProblem otherwise), and its raw decompressed lines are kept in one
buffer, in file order and whole, up to the end of the batch at t: until the first trustworthy message published
after t (valid JSON, an mcm with an integer pt not below the previous one) or the end of the stream, or until a
message at or before t that the index cannot read with a known pt (kept, and the boundary, since the index reads
nothing after it). Lines the index skips (blank, not mcm) are kept as they are; nothing is cleaned or reserialised.
It is unexercised, with a bounded reason, when a line read before the boundary is established, the boundary line
included, gives no trustworthy pt (unknown_pt_before_boundary:<the index's reason>: line_too_long,
decompressed_too_large over the member's running total, not_json, malformed_message, invalid_or_decreasing_pt or
not_bz2), or when keeping the next line would take the retained bytes past the limit (prefix_buffer_limit;
PREFIX_LIMIT_BYTES, checked before appending). The kept prefix is folded from fresh state with the index's own fold
(market_index.fold_member_stream) and replay's price fold, the market's index record is rebuilt from that fold alone
(with the same provenance: the primary and the verified path universe are held fixed), and the market is decided
again (cohort.decide_market, the view from replay.view_from_fold). On this route view_from_fold's anchor, outcome and
index-projection checks compare the rebuilt record with the fold it came from, so they cannot fail there; the check
is the comparison: every field of the MarketDecision (market, plan, population and reason, anchor pt and time,
decision time, every row: ids, inputs, outcomes, reasons, draws, intents, and so its ledger lines) must equal the
run's as canonical JSON, field by field. Any difference, the market missing from the prefix, or a refused
re-decision is failed. Each compared prefix gets a tagged sha256 and size beside its parent member's provenance;
neither enters a decision id or a seed. One prefix is held at a time. The read limits must be the run's (the index
and replay's): each result records them, with the prefix limit. Every result is bound to the decision it checked:
decision_sha256, a tagged sha256 of that MarketDecision's every field as canonical JSON, and its row count.

PrefixTally(variants, record_keys, limits, prefix_limit) takes every input record's result exactly once, with the
decision it checked. It refuses (ValueError) no variants, a duplicate or unknown record, a result without exactly
the result keys, a decision for another record, a result under other limits, evidence without the shape its stage
and status give it (a parent provenance of two sha256 and a path; a row count and decision sha256; at stage record,
no prefix and a not_candidate reason of no_view or anchor_not_known_by_decision or a failed reason; at stage prefix,
a prefix of byte and line counts, with a sha256 and a known boundary when exercised and neither when unexercised,
and a reason only when not passed), and a decision whose sha256 or row count is not the result's. It checks
everything before it changes anything, so a refused result leaves the tally as it was. It reports records,
not_candidate (by reason), failed_against_record, candidates, unexercised (by reason), assessable (candidates whose
prefix was bounded; every one is compared), compared, passed, failed (comparisons), missing records, compared rows
and per-variant issuance among compared markets, so records = not_candidate + failed_against_record + candidates +
missing, candidates = unexercised + assessable and assessable = compared = passed + failed; and one digest over
every result's canonical tuple (decision sha256 and row count included), digested per record and taken in (plan,
market id) order. Status: failed when any check failed or the counts do not reconcile (counts_reconcile); else
incomplete when a record is missing, a candidate was unexercised, nothing was compared, or a variant issued nothing
among compared markets; else passed. This establishes conditional primary-source prefix consistency on the real
input: it does not test alternative real futures, source selection, ingest refusals or markets absent from the
index, which the synthetic certification covers (task c2b3b).

Standard library only (bz2, dataclasses, datetime, decimal, hashlib); it reuses the package's folds,
classification and settlement.
"""

import bz2
from dataclasses import asdict, fields
from datetime import datetime, timezone
from decimal import Decimal
import hashlib

from .accounting import AccountingRules, IssuedBet, MarketOutcome, RunnerOutcome, Settlement, settle
from .archive import DEFAULT_LIMITS, ArchiveProblem, HashingReader, safe_target
from .cohort import (ANCHOR_NOT_KNOWN, ELIGIBLE, ISSUED, NO_VIEW, CohortProblem, classify, decide_market, decision_id,
                     issuance_counts, ledger_lines)
from .contract import ENGINEERING_CRITERIA, canonical_json
from .market_index import _checked_changes, _Unreadable, fold_member_stream, loads_strict
from .metrics import metrics_by_variant
from .replay import PriceFold, ReplayProblem, _member, view_from_fold

PASSED, FAILED, INCOMPLETE = "passed", "failed", "incomplete"
NOT_CANDIDATE, UNEXERCISED = "not_candidate", "unexercised"
RECORD_STAGE, PREFIX_STAGE = "record", "prefix"
UNKNOWN_PT, BUFFER_LIMIT = "unknown_pt_before_boundary", "prefix_buffer_limit"
PREFIX_LIMIT_BYTES = 64 * 1024 * 1024
PREFIX_DIGEST_TAG = b"betangel-research/backtest/primary-prefix/v1\x00"
TALLY_DIGEST_TAG = b"betangel-research/backtest/prefix-tally/v1\x00"
DECISION_DIGEST_TAG = b"betangel-research/backtest/market-decision/v1\x00"
END_OF_STREAM, AFTER_T, KNOWN_FAILURE = "end_of_stream", "first_message_after_t", "known_failure_at_or_before_t"
BOUNDARIES = (AFTER_T, END_OF_STREAM, KNOWN_FAILURE)
PREFIX_METHOD = "primary_source_prefix_consistency"

# --- known accounting answers -------------------------------------------------------------------------------------

KAT_VARIANTS = ("kat", "kat_b")
KAT_OFF = datetime(2015, 6, 1, 14, 0, tzinfo=timezone.utc)
KAT_RATE, KAT_ROUNDING = Decimal("0.02"), "per_market_ceil_penny"
_RESOLVED = {"read_complete": True, "closed": True, "market_void": False, "result_revised_after_close": False,
             "bsp_reconciled": True, "dead_heat": False, "declared_winners": 1, "removed_after_decision": ()}
_WIN_LOSE = {1: ("WINNER", "2.0"), 2: ("LOSER", "2.0")}
# Each case: bets (variant, market, runner, stake pence), outcomes (market: (indicator changes, runners {id: (status,
# bsp)})), expected bets in bet order (status, reasons, settlement case, gross pence), expected (variant, market)
# results (commission, net, unresolved stake), the expected total net over resolved markets and, optionally, the
# commission rounding. Every number is worked by hand from plan sections 7 to 9: a win is stake * (bsp - 1) rounded
# down to the penny, commission is 2% of a (variant, market)'s positive net rounded up to the penny (or to the nearest
# penny, halves up, under per_market_half_up_penny), and an unresolved bet keeps its stake as exposure.
KAT_CASES = (
    {"id": "s9_win_and_loss_in_two_markets",            # +100 (commission 2) and -100: total -2
     "bets": [("kat", "1.000000001", 1, 100), ("kat", "1.000000002", 2, 100)],
     "outcomes": {"1.000000001": ({}, _WIN_LOSE), "1.000000002": ({}, _WIN_LOSE)},
     "expected_bets": [("won", (), None, 100), ("lost", (), None, -100)],
     "expected_markets": {("kat", "1.000000001"): (2, 98, 0), ("kat", "1.000000002"): (0, -100, 0)},
     "expected_net": -2},
    {"id": "s9_plus_and_minus_ten_in_one_market",        # +1000 and -1000 net to 0: no commission
     "bets": [("kat", "1.000000003", 1, 1000), ("kat", "1.000000003", 2, 1000)],
     "outcomes": {"1.000000003": ({}, _WIN_LOSE)},
     "expected_bets": [("won", (), None, 1000), ("lost", (), None, -1000)],
     "expected_markets": {("kat", "1.000000003"): (0, 0, 0)}, "expected_net": 0},
    {"id": "s9_plus_and_minus_ten_in_two_markets",       # commission 20 on the winning market: total -20
     "bets": [("kat", "1.000000004", 1, 1000), ("kat", "1.000000005", 2, 1000)],
     "outcomes": {"1.000000004": ({}, _WIN_LOSE), "1.000000005": ({}, _WIN_LOSE)},
     "expected_bets": [("won", (), None, 1000), ("lost", (), None, -1000)],
     "expected_markets": {("kat", "1.000000004"): (20, 980, 0), ("kat", "1.000000005"): (0, -1000, 0)},
     "expected_net": -20},
    {"id": "penny_rounding_against_us",                  # 100 * 1.375 = 137.5 -> 137; 2% of 137 = 2.74 -> 3; 134
     "bets": [("kat", "1.000000010", 1, 100)],
     "outcomes": {"1.000000010": ({}, {1: ("WINNER", "2.375"), 2: ("LOSER", "3.0")})},
     "expected_bets": [("won", (), None, 137)],
     "expected_markets": {("kat", "1.000000010"): (3, 134, 0)}, "expected_net": 134},
    {"id": "winnings_floor_and_commission_ceiling",       # 100 * 1.1075 = 110.75 -> 110; 2.2 -> 3; 107
     "bets": [("kat", "1.000000014", 1, 100)],
     "outcomes": {"1.000000014": ({}, {1: ("WINNER", "2.1075"), 2: ("LOSER", "3.0")})},
     "expected_bets": [("won", (), None, 110)],
     "expected_markets": {("kat", "1.000000014"): (3, 107, 0)}, "expected_net": 107},
    {"id": "commission_half_up_rounding", "rounding": "per_market_half_up_penny",   # 2.2 -> 2 (108); 2.5 -> 3 (122)
     "bets": [("kat", "1.000000015", 1, 100), ("kat", "1.000000016", 1, 100)],
     "outcomes": {"1.000000015": ({}, {1: ("WINNER", "2.1075"), 2: ("LOSER", "3.0")}),
                  "1.000000016": ({}, {1: ("WINNER", "2.25"), 2: ("LOSER", "3.0")})},
     "expected_bets": [("won", (), None, 110), ("won", (), None, 125)],
     "expected_markets": {("kat", "1.000000015"): (2, 108, 0), ("kat", "1.000000016"): (3, 122, 0)},
     "expected_net": 230},
    {"id": "variants_are_never_netted",                  # one market, two variants: +100 (2) and -100 apart
     "bets": [("kat", "1.000000013", 1, 100), ("kat_b", "1.000000013", 2, 100)],
     "outcomes": {"1.000000013": ({}, _WIN_LOSE)},
     "expected_bets": [("won", (), None, 100), ("lost", (), None, -100)],
     "expected_markets": {("kat", "1.000000013"): (2, 98, 0), ("kat_b", "1.000000013"): (0, -100, 0)},
     "expected_net": -2},
    {"id": "void_is_unresolved_with_exposure",
     "bets": [("kat", "1.000000006", 1, 100)],
     "outcomes": {"1.000000006": ({"market_void": True}, _WIN_LOSE)},
     "expected_bets": [("unresolved", ("market_void",), "market_void", None)],
     "expected_markets": {("kat", "1.000000006"): (None, None, 100)}, "expected_net": 0},
    {"id": "dead_heat_is_unresolved_with_exposure",
     "bets": [("kat", "1.000000007", 1, 100)],
     "outcomes": {"1.000000007": ({"dead_heat": True}, {1: ("WINNER", "2.0"), 2: ("WINNER", "2.0")})},
     "expected_bets": [("unresolved", ("dead_heat",), "dead_heat", None)],
     "expected_markets": {("kat", "1.000000007"): (None, None, 100)}, "expected_net": 0},
    {"id": "own_runner_removed_after_decision_is_unresolved",
     "bets": [("kat", "1.000000008", 1, 100)],
     "outcomes": {"1.000000008": ({"removed_after_decision": (1,)},
                                  {1: ("REMOVED", None), 2: ("WINNER", "2.0"), 3: ("LOSER", "3.0")})},
     "expected_bets": [("unresolved", ("own_runner_removed_after_decision",), "own_runner_removed_after_decision",
                        None)],
     "expected_markets": {("kat", "1.000000008"): (None, None, 100)}, "expected_net": 0},
    {"id": "other_runner_removed_after_decision_is_unresolved",
     "bets": [("kat", "1.000000011", 1, 100)],
     "outcomes": {"1.000000011": ({"removed_after_decision": (2,)},
                                  {1: ("WINNER", "2.0"), 2: ("REMOVED", None), 3: ("LOSER", "3.0")})},
     "expected_bets": [("unresolved", ("other_runner_removed_after_decision",),
                        "other_runner_removed_after_decision", None)],
     "expected_markets": {("kat", "1.000000011"): (None, None, 100)}, "expected_net": 0},
    {"id": "result_revised_after_close_is_unresolved",
     "bets": [("kat", "1.000000012", 1, 100)],
     "outcomes": {"1.000000012": ({"result_revised_after_close": True}, {1: ("WINNER", "2.0"), 2: ("LOSER", "3.0")})},
     "expected_bets": [("unresolved", ("result_revised_after_close",), "result_revised_after_close", None)],
     "expected_markets": {("kat", "1.000000012"): (None, None, 100)}, "expected_net": 0},
    {"id": "missing_bsp_is_unresolved",
     "bets": [("kat", "1.000000009", 1, 100)],
     "outcomes": {"1.000000009": ({}, {1: ("WINNER", None), 2: ("LOSER", "2.0")})},
     "expected_bets": [("unresolved", ("bsp_missing",), "missing_bsp_or_result", None)],
     "expected_markets": {("kat", "1.000000009"): (None, None, 100)}, "expected_net": 0},
)


def _kat_outcome(market_id, changes, runners):
    indicators = dict(_RESOLVED, **changes)
    return MarketOutcome(market_id=market_id, first_published_off=KAT_OFF,
                         runners={rid: RunnerOutcome(status, None if bsp is None else Decimal(bsp))
                                  for rid, (status, bsp) in runners.items()}, **indicators)


def known_accounting_answers():
    """The known_accounting_answers criterion (module docstring): every bet's status, full reasons, settlement case
    and gross pence, every (variant, market)'s commission, net and unresolved stake, and the total net."""
    problems, cases = [], 0
    for case in KAT_CASES:
        cases += 1
        try:
            rules = AccountingRules(commission_rate=KAT_RATE, rounding=case.get("rounding", KAT_ROUNDING),
                                    variants=KAT_VARIANTS)
            bets = [IssuedBet(decision_id=f"{case['id']}.{index}", variant=variant, market_id=market_id,
                              selection_id=selection_id, stake_pence=stake, side="BACK", basis="BSP")
                    for index, (variant, market_id, selection_id, stake) in enumerate(case["bets"])]
            outcomes = [_kat_outcome(market_id, *spec) for market_id, spec in case["outcomes"].items()]
            settlement = settle(bets, outcomes, rules)
            if (sorted(bet.decision_id for bet in settlement.bets) != sorted(bet.decision_id for bet in bets)
                    or len({(m.variant, m.market_id) for m in settlement.markets}) != len(settlement.markets)):
                raise ValueError("kat:settlement_not_one_result_per_bet_and_market")
            by_id = {bet.decision_id: bet for bet in settlement.bets}
            got_bets = [(by_id[b.decision_id].status, tuple(by_id[b.decision_id].reasons),
                         by_id[b.decision_id].settlement_case, by_id[b.decision_id].gross_pence) for b in bets]
            got_markets = {(m.variant, m.market_id): (m.commission_pence, m.net_pence, m.unresolved_stake_pence)
                           for m in settlement.markets}
            got_net = sum(market.net_pence for market in settlement.markets if market.resolved)
        except (ValueError, KeyError, TypeError, AttributeError, ArithmeticError):
            problems.append(case["id"])
            continue
        if (got_bets, got_markets, got_net) != (case["expected_bets"], case["expected_markets"], case["expected_net"]):
            problems.append(case["id"])
    return {"status": FAILED if problems else PASSED, "cases": cases, "failed_cases": problems}


# --- issuance and exposure reconciliation ------------------------------------------------------------------------

def _failed(problems):
    return {"status": FAILED, "problems": problems, "unexercised_variants": [], "by_variant": {}}


def issuance_exposure_reconciliation(rules, records, decisions, ledger_bytes, bets, settlement):
    """The issuance_exposure_reconciliation criterion (module docstring). records are the run's input index
    records, ledger_bytes the issuance ledger as persisted."""
    decisions = list(decisions)
    try:
        counts = issuance_counts(rules, decisions)
        expected_ledger = b"".join(ledger_lines(decisions))
    except (ValueError, KeyError, TypeError, CohortProblem) as error:
        return _failed([f"issuance:{type(error).__name__}:{error}"])
    if not isinstance(settlement, Settlement):
        return _failed(["settlement:not_a_settlement"])
    problems = []
    if tuple(sorted(settlement.rules.variants or ())) != tuple(sorted(rules.variants)):
        problems.append("settlement:other_variants")
    variants, not_eligible, not_theirs = set(rules.variants), 0, 0
    other_variants = sum(1 for decision in decisions for row in decision.rows if set(row["variants"]) != variants)
    for decision in decisions:
        if decision.population != ELIGIBLE and decision.rows:
            not_eligible += 1
        expected = {"axis": rules.axis, "decision_ms": decision.decision_ms, "anchor_pt": decision.anchor_pt,
                    "anchor_ms": decision.anchor_ms}
        for row in decision.rows:
            if ((row["market_id"], row["plan"], row["decision"]) != (decision.market_id, decision.plan, expected)
                    or row["decision_id"] != decision_id(rules, decision.market_id, row["selection_id"])):
                not_theirs += 1
    if other_variants:
        problems.append(f"rows:{other_variants}_without_exactly_the_contract_variants")
    if not_eligible:
        problems.append(f"decisions:{not_eligible}_not_eligible_with_rows")
    if not_theirs:
        problems.append(f"rows:{not_theirs}_not_their_decisions")
    keys = [(record["provenance"]["plan"], record["market_id"]) for record in records]
    decided = {(decision.plan, decision.market_id) for decision in decisions}
    if len(set(keys)) != len(keys):
        problems.append("records:duplicate")
    if set(keys) - decided:
        problems.append(f"decisions:missing_for_{len(set(keys) - decided)}_records")
    if decided - set(keys):
        problems.append(f"decisions:{len(decided - set(keys))}_without_an_input_record")
    if ledger_bytes != expected_ledger:
        problems.append("ledger:not_the_decisions")
    issued = {}
    for decision in decisions:
        for row in decision.rows:
            for variant in rules.variants:              # each checked by issuance_counts
                outcome = row["variants"][variant]
                if outcome["outcome"] == ISSUED:
                    issued[(variant, row["decision_id"])] = (row["market_id"], row["selection_id"],
                                                           outcome["intent"]["stake_pence"])
    for name, items in (("bets", bets), ("settled", settlement.bets)):
        found = {}
        for bet in items:
            key = (bet.variant, bet.decision_id)
            if key in found:
                problems.append(f"{name}:duplicate:{bet.variant}:{bet.decision_id}")
            found[key] = (bet.market_id, bet.selection_id, bet.stake_pence)
        if found != issued:
            problems.append(f"{name}:not_the_issued_rows")
    try:
        metrics = {item.variant: item for item in metrics_by_variant(settlement, rules.variants)}
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        return _failed(problems + [f"metrics:{type(error).__name__}:{error}"])
    by_variant, unexercised = {}, []
    for variant in rules.variants:
        m = metrics[variant]
        n_issued = counts[variant]["issued"]
        by_variant[variant] = {"rows": counts[variant]["rows"], "issued": n_issued,
                               "issued_stake_pence": counts[variant]["issued_stake_pence"],
                               "resolved_bets": m.resolved_bets, "unresolved_bets": m.unresolved_bets,
                               "resolved_stake_pence": m.resolved_stake_pence,
                               "unresolved_stake_pence": m.unresolved_stake_pence}
        if n_issued != m.resolved_bets + m.unresolved_bets:
            problems.append(f"{variant}:issued_not_resolved_plus_unresolved")
        if counts[variant]["issued_stake_pence"] != m.resolved_stake_pence + m.unresolved_stake_pence:
            problems.append(f"{variant}:issued_stake_not_s_r_plus_s_u")
        if n_issued == 0:
            unexercised.append(variant)
    status = FAILED if problems else INCOMPLETE if unexercised else PASSED
    return {"status": status, "problems": problems, "unexercised_variants": unexercised, "by_variant": by_variant}


# --- primary-source prefix consistency ---------------------------------------------------------------------------

class _PrefixReader:
    """readline() over the kept prefix, held in one bytearray, for fold_member_stream."""

    def __init__(self, data):
        self._data, self._at = data, 0

    def readline(self, limit=-1):
        data, at = self._data, self._at
        if at >= len(data):
            return b""
        end = data.find(b"\n", at)
        end = len(data) if end < 0 else end + 1
        if limit is not None and 0 <= limit < end - at:
            end = at + limit
        self._at = end
        return bytes(data[at:end])


def _line_kind(line, previous, t):
    """(kind, pt or reason) of one raw line for the boundary, as market_index._messages would read it: keep (blank or
    not mcm), unknown (no trustworthy pt: the index's reason), after (an mcm published after t), failure (an mcm at
    or before t the index cannot read past, a known pt) or apply (an mcm at or before t)."""
    if not line.strip():
        return "keep", None
    try:
        message = loads_strict(line)
    except (ValueError, RecursionError):
        return "unknown", "not_json"
    if not isinstance(message, dict):
        return "unknown", "malformed_message"
    if message.get("op") != "mcm":
        return "keep", None
    pt = message.get("pt")
    if type(pt) is not int or pt < previous:
        return "unknown", "invalid_or_decreasing_pt"
    if pt > t:
        return "after", pt
    if message.get("segmentType") is not None:
        return "failure", pt
    try:
        _checked_changes(message)
    except _Unreadable:
        return "failure", pt
    return "apply", pt


def _capture(raw, member, t, limits, prefix_limit):
    """(prefix bytearray, tagged sha256, line count, boundary or None, unexercised reason or None) of one member;
    its compressed bytes are all hashed and must be the manifest's (ReplayProblem otherwise)."""
    data, digest, lines, previous, total = bytearray(), hashlib.sha256(PREFIX_DIGEST_TAG), 0, -1, 0
    boundary = reason = None
    with open(raw, "rb") as handle:
        reader = HashingReader(handle)
        try:
            with bz2.BZ2File(reader) as stream:
                while True:
                    line = stream.readline(limits.max_line_bytes + 1)
                    if not line:
                        boundary = END_OF_STREAM
                        break
                    total += len(line)                 # as the index counts: every line, the boundary line too
                    if len(line) > limits.max_line_bytes:
                        reason = f"{UNKNOWN_PT}:line_too_long"
                        break
                    if total > limits.max_member_decompressed_bytes:
                        reason = f"{UNKNOWN_PT}:decompressed_too_large"
                        break
                    kind, value = _line_kind(line, previous, t)
                    if kind == "unknown":
                        reason = f"{UNKNOWN_PT}:{value}"
                        break
                    if kind == "after":
                        boundary = AFTER_T
                        break
                    if len(data) + len(line) > prefix_limit:
                        reason = BUFFER_LIMIT
                        break
                    data += line
                    digest.update(line)
                    lines += 1
                    if value is not None:
                        previous = value
                    if kind == "failure":
                        boundary = KNOWN_FAILURE
                        break
        except (OSError, EOFError, ValueError):
            if boundary is None and reason is None:
                reason = f"{UNKNOWN_PT}:not_bz2"
        hashed = reader.drain()
    if hashed != (member["sha256"], member["size_bytes"]):
        raise ReplayProblem(f"prefix:{member['path']}:member_hash_mismatch")
    return data, digest.hexdigest(), lines, boundary, reason


def _differing(decision, again):
    """The MarketDecision fields whose canonical JSON differs between two decisions."""
    return sorted(item.name for item in fields(decision)
                  if canonical_json(getattr(decision, item.name)) != canonical_json(getattr(again, item.name)))


def decision_sha256(decision):
    """The tagged sha256 of a MarketDecision's every field as canonical JSON: the decision a prefix result checked."""
    return hashlib.sha256(DECISION_DIGEST_TAG + canonical_json({item.name: getattr(decision, item.name)
                                                                for item in fields(decision)})).hexdigest()


def _refuse_replay(record):
    raise ReplayProblem(f"prefix:{record['market_id']}:replay_without_a_view")


def check_prefix(rules, record, decision, root, manifest, limits, prefix_limit=PREFIX_LIMIT_BYTES):
    """One record's prefix consistency result (module docstring): status not_candidate, passed, failed or
    unexercised, the stage that decided it (record or prefix), the reason, the read and prefix limits used, the
    derived prefix's tagged sha256, size, line count and boundary, the parent member's provenance, and the checked
    decision's sha256 (decision_sha256) and row count. ReplayProblem when the member is not the manifest's or cannot
    be read."""
    market_id, plan = record["market_id"], record["provenance"]["plan"]
    base = {"market_id": market_id, "plan": plan, "stage": RECORD_STAGE, "read_limits": asdict(limits),
            "prefix_limit_bytes": prefix_limit,
            "parent": {"archive_sha256": record["provenance"]["archive_sha256"],
                       "member_path": record["provenance"]["member_path"],
                       "member_sha256": record["provenance"]["member_sha256"]},
            "prefix": None, "decision_sha256": decision_sha256(decision), "rows": len(decision.rows)}
    population, reason, timing = classify(rules, record)
    timing = timing if timing is not None else (None, None, None)
    if ((decision.market_id, decision.plan, decision.population, decision.reason) != (market_id, plan, population,
                                                                                         reason)
            or (decision.anchor_pt, decision.anchor_ms, decision.decision_ms) != timing):
        return dict(base, status=FAILED, reason="decision_not_the_records_classification")
    if population in (NO_VIEW, ANCHOR_NOT_KNOWN):
        differing = _differing(decision, decide_market(rules, record, _refuse_replay))
        if differing:
            return dict(base, status=FAILED, reason="decision_differs:" + ",".join(differing))
        return dict(base, status=NOT_CANDIDATE, reason=population)
    base["stage"] = PREFIX_STAGE
    member = _member(manifest, record)
    where = f"prefix:{market_id}"
    try:
        raw = safe_target(root, manifest["archive"]["sha256"], member["path"])
    except ArchiveProblem as error:
        raise ReplayProblem(f"{where}:{error}") from None
    try:
        if not raw.is_file():
            raise ReplayProblem(f"{where}:member_missing")
        data, digest, lines, boundary, unexercised = _capture(raw, member, timing[2], limits, prefix_limit)
    except OSError:
        raise ReplayProblem(f"{where}:member_unreadable_file") from None
    prefix = {"sha256": digest, "bytes": len(data), "lines": lines, "boundary": boundary}
    if unexercised is not None:
        return dict(base, status=UNEXERCISED, reason=unexercised, prefix=dict(prefix, sha256=None))
    prices = PriceFold()
    markets, unreadable, complete = fold_member_stream(
        _PrefixReader(data), limits, observe=lambda pt, mid, change: prices.change(pt, change) if mid == market_id
        else None)
    del data
    market = markets.get(market_id)
    if market is None:
        return dict(base, status=FAILED, reason="market_absent_from_prefix", prefix=prefix)
    if unreadable:
        market.truncate(complete)
    rebuilt = market.record(record["provenance"], (unreadable, complete) if unreadable else None)
    try:
        again = decide_market(rules, rebuilt, lambda item: view_from_fold(item, market, prices, rules.offset_s))
    except (ReplayProblem, CohortProblem, ValueError) as error:
        return dict(base, status=FAILED, reason=f"redecision:{error}", prefix=prefix)
    differing = _differing(decision, again)
    if differing:
        return dict(base, status=FAILED, reason="decision_differs:" + ",".join(differing), prefix=prefix)
    return dict(base, status=PASSED, reason=None, prefix=prefix)


def _is_count(value):
    return type(value) is int and value >= 0


def _is_sha256(value):
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _evidence_problem(result):
    """None, or why a result's evidence does not have the shape its stage and status give it (module docstring)."""
    stage, status, reason, prefix, parent = (result["stage"], result["status"], result["reason"], result["prefix"],
                                             result["parent"])
    if not (isinstance(parent, dict) and sorted(parent) == ["archive_sha256", "member_path", "member_sha256"]
            and _is_sha256(parent["archive_sha256"]) and _is_sha256(parent["member_sha256"])
            and isinstance(parent["member_path"], str)):
        return "parent"
    if not _is_count(result["rows"]) or not _is_sha256(result["decision_sha256"]):
        return "decision"
    if stage == RECORD_STAGE:
        if prefix is not None:
            return "prefix"
        if status == NOT_CANDIDATE:
            return None if reason in (NO_VIEW, ANCHOR_NOT_KNOWN) else "reason"
        return None if status == FAILED and isinstance(reason, str) else "status"
    if stage != PREFIX_STAGE or status not in (PASSED, FAILED, UNEXERCISED):
        return "status"
    if not (isinstance(prefix, dict) and sorted(prefix) == ["boundary", "bytes", "lines", "sha256"]
            and _is_count(prefix["bytes"]) and _is_count(prefix["lines"])):
        return "prefix"
    if status == UNEXERCISED:
        if (prefix["sha256"], prefix["boundary"]) != (None, None):
            return "prefix"
        return None if reason == BUFFER_LIMIT or isinstance(reason, str) and reason.startswith(UNKNOWN_PT + ":") \
            else "reason"
    if not _is_sha256(prefix["sha256"]) or prefix["boundary"] not in BOUNDARIES:
        return "prefix"
    return None if (reason is None) == (status == PASSED) and (reason is None or isinstance(reason, str)) \
        else "reason"


class PrefixTally:
    """The prefix_invariance criterion over a run (module docstring): PrefixTally(variants, record_keys, limits,
    prefix_limit), then add(result, decision) once per input record, then result(). add checks everything before it
    changes anything, so a refused result leaves the tally as it was (that record still missing, and retryable)."""

    RESULT_KEYS = ("decision_sha256", "market_id", "parent", "plan", "prefix", "prefix_limit_bytes", "read_limits",
                   "reason", "rows", "stage", "status")
    COUNTS = ("candidates", "assessable", "compared", PASSED, FAILED, UNEXERCISED, NOT_CANDIDATE,
              "failed_against_record", "compared_rows")

    def __init__(self, variants, record_keys, limits=DEFAULT_LIMITS, prefix_limit=PREFIX_LIMIT_BYTES):
        self.variants = tuple(variants)
        if not self.variants:
            raise ValueError("prefix_tally:no_variants")
        keys = [tuple(key) for key in record_keys]
        self.expected = set(keys)
        if len(self.expected) != len(keys):
            raise ValueError("prefix_tally:duplicate_record_key")
        self.read_limits, self.prefix_limit = asdict(limits), prefix_limit
        self.counts = {name: 0 for name in self.COUNTS}
        self.unexercised, self.not_candidate, self.failures = {}, {}, []
        self.issued = {variant: 0 for variant in self.variants}
        self._digests = {}                  # (plan, market id): sha256 of the result's canonical tuple

    def add(self, result, decision):
        if not isinstance(result, dict) or sorted(result) != sorted(self.RESULT_KEYS):
            raise ValueError("prefix_tally:malformed_result")
        key = (result["plan"], result["market_id"])
        where = f"prefix_tally:{result['market_id']}"
        if key not in self.expected:
            raise ValueError(f"{where}:not_an_input_record")
        if key in self._digests:
            raise ValueError(f"{where}:duplicate")
        if (decision.plan, decision.market_id) != key:
            raise ValueError(f"{where}:decision_for_another_record")
        if (result["read_limits"], result["prefix_limit_bytes"]) != (self.read_limits, self.prefix_limit):
            raise ValueError(f"{where}:other_limits")
        problem = _evidence_problem(result)
        if problem is not None:
            raise ValueError(f"{where}:malformed_{problem}")
        if (result["decision_sha256"], result["rows"]) != (decision_sha256(decision), len(decision.rows)):
            raise ValueError(f"{where}:decision_not_the_one_checked")
        stage, status, reason = result["stage"], result["status"], result["reason"]
        changes, issued = {}, {}
        if stage == RECORD_STAGE:
            changes["failed_against_record" if status == FAILED else NOT_CANDIDATE] = 1
        elif status == UNEXERCISED:
            changes.update(candidates=1, **{UNEXERCISED: 1})
        else:
            changes.update(candidates=1, assessable=1, compared=1, compared_rows=result["rows"], **{status: 1})
            for row in decision.rows:
                for variant in self.variants:
                    if row["variants"].get(variant, {}).get("outcome") == ISSUED:
                        issued[variant] = issued.get(variant, 0) + 1
        line = canonical_json([result["plan"], result["market_id"], stage, status, reason, result["prefix"],
                               result["parent"], result["decision_sha256"], result["rows"]])
        # Everything is checked: apply the addition whole.
        self._digests[key] = hashlib.sha256(line).digest()
        for name, value in changes.items():
            self.counts[name] += value
        for variant, value in issued.items():
            self.issued[variant] += value
        if status == FAILED and len(self.failures) < 100:
            self.failures.append([result["plan"], result["market_id"], stage, reason])
        if stage == RECORD_STAGE and status == NOT_CANDIDATE:
            self.not_candidate[reason] = self.not_candidate.get(reason, 0) + 1
        if status == UNEXERCISED:
            self.unexercised[reason] = self.unexercised.get(reason, 0) + 1

    def result(self):
        missing = sorted(self.expected - set(self._digests))
        unissued = [variant for variant, count in self.issued.items() if count == 0]
        counts = dict(self.counts, records=len(self.expected), missing=len(missing))
        reconciles = (counts["records"] == counts[NOT_CANDIDATE] + counts["failed_against_record"]
                      + counts["candidates"] + counts["missing"]
                      and counts["candidates"] == counts[UNEXERCISED] + counts["assessable"]
                      and counts["assessable"] == counts["compared"] == counts[PASSED] + counts[FAILED]
                      and len(self._digests) + len(missing) == len(self.expected))
        if counts[FAILED] or counts["failed_against_record"] or not reconciles:
            status = FAILED
        elif missing or counts[UNEXERCISED] or not counts["compared"] or unissued:
            status = INCOMPLETE
        else:
            status = PASSED
        digest = hashlib.sha256(TALLY_DIGEST_TAG)
        for key in sorted(self._digests):
            digest.update(self._digests[key])
        return {"status": status, "method": PREFIX_METHOD, "read_limits": dict(self.read_limits),
                "prefix_limit_bytes": self.prefix_limit, "counts": counts, "counts_reconcile": reconciles,
                "unexercised_by_reason": dict(sorted(self.unexercised.items())),
                "not_candidate_by_reason": dict(sorted(self.not_candidate.items())),
                "issued_among_compared": dict(self.issued), "unissued_variants": unissued,
                "missing_records": [list(key) for key in missing[:100]], "failures": self.failures,
                "results_sha256": digest.hexdigest()}


def engineering_status(criteria):
    """failed when any criterion failed; else incomplete unless the criteria are exactly ENGINEERING_CRITERIA, each
    passed or incomplete, and none incomplete; else passed."""
    statuses = [item.get("status") if isinstance(item, dict) else None for item in criteria.values()]
    if FAILED in statuses:
        return FAILED
    if sorted(criteria) != sorted(ENGINEERING_CRITERIA) or any(status != PASSED for status in statuses):
        return INCOMPLETE
    return PASSED
