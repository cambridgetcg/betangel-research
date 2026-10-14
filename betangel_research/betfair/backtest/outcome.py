"""The evaluator's side of a decided market: accounting inputs from the issuance ledger and the index (task c2b, D11).

Nothing here makes or changes a decision. issued_bets turns a cohort.MarketDecision's issued rows into
accounting.IssuedBet records; market_outcome turns the same market's index record into the accounting.MarketOutcome
that settles them. Only the evaluator reads these; no strategy view ever holds them.

market_outcome(record, decision), for the record a cohort.MarketDecision was made from (same market id, plan, anchor
and decision time, else ValueError), field by field (accounting.py's input docstring; plan sections 7 and 8; Astra's
design agreement D11 and Q5 for void):
- first_published_off: the UTC time of the index's first-published marketTime (the anchor), aware.
- read_complete: evaluator_only_outcome.read_complete when it is a bool, else None. A listed final runner or a cohort
  runner whose id is not a positive integer (the index accepts 0; accounting refuses it) also makes it None: the
  outcome cannot then be held whole, so every bet of the market is unresolved (outcome_completeness_unknown), never
  settled from part of it, and the run never aborts on it.
- closed: final_status == "CLOSED" when final_status is text, else None (the index's final_status, not
  closed_reached: a market reopened or reset after CLOSED is not closed at the end).
- market_void: always None. Betfair documents no market-level void flag, and CLOSED means only "settled", so a
  BASIC file cannot show that a market was not void; under Q5 unknown stays unknown and every P0 bet is unresolved
  (void_unknown). Nothing here infers False from CLOSED, winner counts or the absence of a revision.
- result_revised_after_close: the index's results_revised_after_close when a bool, else None.
- bsp_reconciled: the last bspReconciled value in outcome_definition when a bool, else None.
- dead_heat: the index's dead_heat when a bool, else None.
- declared_winners: declared_number_of_winners when a non-negative int (not a bool), else None.
- removed_after_decision: the cohort runners (ACTIVE and listed at t, as decided) removed after t: every entry of the
  index's decision_time removals with pt > t (removals published in the decision window, between t and the cutoff)
  together with every entry of evaluator_only_outcome.removals_after_cutoff, restricted to the cohort (the runner
  rows' ids). None (removals_unknown, never "not removed") when the member was not read whole (later removals are
  then unknown), when a cohort id is not a positive integer, when a cohort runner is not listed at the end (it left
  the market without a removal the index saw), or when a cohort runner's own history after t, across both of the
  index's partitions (decision_time runners entries with pt > t, and runners_after_cutoff), shows an interval whose
  state cannot be assessed before any removal of it was seen: a status that is null or not one the Historical Data
  spec lists (an image reset, "REMOVED?"), or listed false. A later result that lists the runner again does not
  resolve that interval. A pt with no new entry carries the known state forward and is no unknown; entries at t
  itself belong to the view at t and are not after it; a runner already seen removed stays removed whatever follows.
- runners: every final runner listed at the end, {id: RunnerOutcome(status when text else None, bsp)}, the bsp
  through decimal_from_json_number (never Decimal(float)); a bsp that is not a JSON number makes that runner's bsp
  None (bsp_missing for a bet on it), never zero.
Malformed values come out unknown (None), so odd data leaves bets unresolved with a reason instead of aborting the
run; a record whose shape is not the index's (a missing key) raises KeyError or TypeError, a run-integrity failure
the runner reports. Standard library only.
"""

from .accounting import (BASES, SIDES, IssuedBet, MarketOutcome, RunnerOutcome, decimal_from_json_number)
from ...temporal import timestamp_ms
from .archive import utc_moment
from .cohort import ISSUED
from .market_index import KNOWN_RUNNER_STATUSES

REMOVED_STATUSES = ("REMOVED", "REMOVED_VACANT")


def _bool_or_none(value):
    return value if type(value) is bool else None


def cohort_ids(decision):
    """The selection ids of a MarketDecision's runner rows (ACTIVE and listed at t), sorted."""
    return tuple(sorted(row["selection_id"] for row in decision.rows if row["selection_id"] is not None))


def issued_bets(decision):
    """One accounting.IssuedBet per issued (row, variant) of a MarketDecision, in row then variant order."""
    bets = []
    for row in decision.rows:
        for variant in sorted(row["variants"]):
            outcome = row["variants"][variant]
            if outcome["outcome"] != ISSUED:
                continue
            intent = outcome["intent"]
            if intent["side"] not in SIDES or intent["price"] not in BASES:
                raise ValueError(f"outcome:{row['decision_id']}:{variant}:intent_not_supported")
            bets.append(IssuedBet(decision_id=row["decision_id"], variant=variant, market_id=row["market_id"],
                                  selection_id=row["selection_id"], stake_pence=intent["stake_pence"],
                                  side=intent["side"], basis=intent["price"]))
    return tuple(bets)


def _history_after(record, decision_ms, selection_id):
    """{pt: [(field, value), ...]} of one runner's status and listed entries after decision_ms, from both index
    partitions, each pt's entries in timeline order (status, then listed)."""
    entries = {}
    sources = [item for item in record["decision_time"]["runners"] if item["id"] == selection_id]
    sources += [item for item in record["evaluator_only_outcome"]["runners_after_cutoff"] if item["id"] == selection_id]
    for source in sources:
        for field in ("status", "listed"):
            for pt, value in source.get(field, []):
                if pt > decision_ms:
                    entries.setdefault(pt, []).append((field, value))
    return entries


def _assessable(record, decision_ms, selection_id):
    """(removed, assessable): whether the runner was seen removed after decision_ms, and whether no interval of
    unknown state came before that (or at all, when it never was)."""
    for pt, entries in sorted(_history_after(record, decision_ms, selection_id).items()):
        if any(field == "status" and value in REMOVED_STATUSES for field, value in entries):
            return True, True
        if any((field == "status" and value not in KNOWN_RUNNER_STATUSES) or (field == "listed" and value is not True)
               for field, value in entries):
            return False, False
    return False, True


def _removed_after(record, decision_ms, cohort, listed_ids):
    outcome = record["evaluator_only_outcome"]
    if outcome["read_complete"] is not True or any(selection_id not in listed_ids for selection_id in cohort):
        return None
    removed = set()
    for selection_id in cohort:
        seen, assessable = _assessable(record, decision_ms, selection_id)
        if not assessable:
            return None
        if seen:
            removed.add(selection_id)
    later = [item for item in record["decision_time"]["removals"] if item["pt"] > decision_ms]
    later += list(outcome["removals_after_cutoff"])
    removed |= {item["id"] for item in later
                if item["id"] in cohort and item["status"] in REMOVED_STATUSES and item["pt"] > decision_ms}
    return frozenset(removed)


def _bsp(value):
    try:
        return decimal_from_json_number(value)
    except ValueError:
        return None


def market_outcome(record, decision):
    """The accounting.MarketOutcome of the index record a cohort.MarketDecision was made from (module docstring)."""
    outcome = record["evaluator_only_outcome"]
    first = record["decision_time"]["first_published_market_time"]
    off = utc_moment(first["value"]) if first is not None else None
    if off is None:
        raise ValueError(f"outcome:{record['market_id']}:no_anchor")
    if ((record["market_id"], record["provenance"]["plan"], first["pt"], timestamp_ms(first["value"]))
            != (decision.market_id, decision.plan, decision.anchor_pt, decision.anchor_ms)):
        raise ValueError(f"outcome:{record['market_id']}:record_is_not_the_decisions")
    cohort = set(cohort_ids(decision))
    final_status = outcome["final_status"]
    reconciled = (outcome["outcome_definition"].get("bspReconciled") or [[None, None]])[-1][1]
    declared = outcome["declared_number_of_winners"]
    listed = [runner for runner in outcome["final_runners"] if runner["listed"] is True]
    whole = all(type(selection_id) is int and selection_id >= 1
                for selection_id in [runner["id"] for runner in listed] + list(cohort))
    runners = {runner["id"]: RunnerOutcome(status=runner["status"] if isinstance(runner["status"], str) else None,
                                           bsp=_bsp(runner["bsp"])) for runner in listed} if whole else {}
    return MarketOutcome(
        market_id=record["market_id"], first_published_off=off,
        read_complete=_bool_or_none(outcome["read_complete"]) if whole else None,
        closed=final_status == "CLOSED" if isinstance(final_status, str) else None,
        market_void=None,
        result_revised_after_close=_bool_or_none(outcome["results_revised_after_close"]),
        bsp_reconciled=_bool_or_none(reconciled),
        dead_heat=_bool_or_none(outcome["dead_heat"]),
        declared_winners=declared if type(declared) is int and declared >= 0 else None,
        removed_after_decision=_removed_after(record, decision.decision_ms, cohort,
                                              {runner["id"] for runner in listed}) if whole else None,
        runners=runners)
