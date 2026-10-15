"""Engine certification cases (task c2b3b2a): the protocol's case registry and run_cases, which runs every case
through the real ingest, index and runner evaluation on the synthetic worlds of certification_fixtures, and compares
each case's observed projection with its hand-labelled expected constant (certification_fixtures.EXPECTED).

Scope: these cases certify the engine's evaluation, which is what run_backtest computes before it writes anything:
ingest, the index, runner._inputs, _first_pass and _evaluate (decisions, issuance, the prefix check and its tally,
settlement), the issuance and exposure reconciliation, and accounting at each parameter point. Run finalisation
(results and receipt writing, the bounded read-back, the engineering_acceptance status), the guards, the stop rule
and the trial ledger are left to the runner's own tests. The bootstrap plan is carried into each run but its seed is
not witnessed here: every synthetic scenario bet is unresolved, so no resample has data.

The registry (CASES) is the protocol's: each case's id, kind and the exact counts its observed projection must
report (COUNTERS reads each count from the projection, never from the case). Kinds:
- check: the engine must give the expected answer;
- negative_control: an input built to be wrong must be caught exactly as stated (a refusal, or a failed status or
  prefix result with its reason); the control passes only when that exact outcome is observed, and its projection
  must show the refusal or the failure;
- incomplete_control: an input that exercises nothing must give incomplete (the tally and the reconciliation),
  never passed.
A case's result is as_expected when its observed projection equals its expected constant as canonical JSON, its
counts are the registry's and its kind's invariant holds; not_as_expected otherwise; and error when an exception the
case did not ask for escapes (never counted as a successful negative control). run_cases never skips a case.

What the cases cover (Astra's design decisions 2 to 6 and control A on report_8cc414cf, and the pre-review):
- kat: known_accounting_answers over its exact case set (the runner's KAT_CASE_IDS).
- scenario_full: the scenario world's full form through ingest, index, runner._inputs, _first_pass and _evaluate:
  every market's class, reason and timing, each row's outcomes, input, decision id, decision block and each
  variant's intent on that row, the set of issued intents, the ledger's row order, every prefix result, every bet's
  settlement and the rules it was settled under, the tally's counts and the reconciliation's status, against hand
  constants.
- scenario_cut, scenario_deleted, scenario_revised: the same anchor-by-t membership, the same decision for every
  market in it (canonical JSON) and the same issuance ledger bytes as the full form, the fixed diagnostics (records
  outside that population), and how the form's futures differ from the full form's (_distinct: the records whose
  outcome changed, the primaries read past t, those bearing an outcome, and the definition fields and removals
  published after t), so no form is compared with itself. Three population markets have no future to change in any
  form (1.400000007, 1.400000009, 1.400000017) and 1.400000008's is read the same way in the revised form; the
  changed outcomes name the rest.
- second_point: the full form under the second parameter point (offset, stake, seed; rows as in scenario_full, and
  the reconciliation's status).
- settlement_witness: one resolved winning bet settled under each point's accounting rules (stake, commission rate
  and rounding), where the two roundings and rates give different nets.
- market_conditions: one market per cohort rule the scenario world does not reach (classes and absent class
  fields, the anchor's month rather than the path's, market conditions 2 to 6 and their order, values present but
  of the wrong type, the price input, a marketTime revised before t), each with its timing, row inputs and intents,
  and the reconciliation's status.
- sources_named_file_decides: the market file named after a market decides it; a pre-t change in the event file
  that also holds it changes nothing there, though the same change moves the favourite when the event file is
  alone; the same change in the market file moves the favourite.
- sources_primary_rules: the primary-source rules the scenario world does not reach (an empty named file, two named
  files, the earliest mention and its tie-break by path, the event file of that mention's directory, a refused
  primary), each world built so that the rule it names and the nearest wrong reading give other answers (where two
  members mention the market, path order and pt order choose differently).
- membership_gates: a sealed-month member and a content-gated member are refused at ingest; the kept market is
  decided.
- duplicate_across_archives: the runner's first pass refuses one market in two archives.
- limit_*: a prefix of exactly the limit passes; one more byte is unexercised (prefix_buffer_limit), with the bytes
  and lines kept before the limit, the decision unchanged and the run's own tally (at 64 MiB) as stated; with an
  equal-pt batch at t, a limit inside the batch is unexercised. read_limits_exact and read_limits_unexercised: lines
  of exactly the read limit are read; a line one byte longer before the boundary is unexercised
  (unknown_pt_before_boundary:line_too_long), and the index's view stops there too.
- abstaining_incomplete, unexercised_incomplete: incomplete, never passed.
- Negative controls: a future leak in the run's view, a pre-t change and a wrong decision time (each also through a
  PrefixTally), borrowed rows, a flipped ledger byte, swapped bets, a dropped decision, a short settlement, a
  decision without an input record, a duplicate input record, a row with another variant, a settlement under other
  variants, a member changed after indexing, a markets file changed during the run, a passing result reused for a
  changed decision under the same key, and a rejected tally addition that must leave the tally unchanged and
  retryable.

Standard library only; it writes only under the work root it is given (fresh case directories by exclusive
create), reads nothing else and consumes no trial. It does not validate that root: run_case and run_cases write
case directories and TARs before ingest checks anything, so the caller must pass a root it has already validated
(archive.check_data_root and the runner's path guards: outside every Git worktree, not a symlink, owned and private)
and must refuse before calling them otherwise. The certify-engine wrapper (task c2b3b2b) enforces this.
"""

from dataclasses import replace
import json
from pathlib import Path

from . import runner
from .accounting import AccountingRules, IssuedBet, settle
from .archive import DEFAULT_LIMITS, ingest_archive, safe_target
from .certification_fixtures import (EXPECTED, LIMIT_MARKET, WITNESS_MARKET, abstaining_world, conditions_world,
                                     duplicate_world, event_alone_world, limit_world, membership_world, p0_rules,
                                     scenario_world, second_point_rules, source_rules_worlds, sources_world,
                                     unexercised_world, ustar, witness_outcome)
from .cohort import ANCHOR_NOT_KNOWN, ISSUED, NO_VIEW, SEEDED, decide_market, decision_id, ledger_lines
from .contract import canonical_json
from .engineering import (KAT_CASES, PREFIX_LIMIT_BYTES, PrefixTally, check_prefix,
                          issuance_exposure_reconciliation, known_accounting_answers)
from .market_index import index_archives
from .outcome import market_outcome
from .replay import ReplayProblem, replay_view

CHECK, NEGATIVE, INCOMPLETE = "check", "negative_control", "incomplete_control"
AS_EXPECTED, NOT_AS_EXPECTED, ERROR = "as_expected", "not_as_expected", "error"
# The protocol's case registry: (id, kind, the exact counts the observed projection must report).
CASES = (
    ("kat", CHECK, {"kat_cases": 13}),
    ("scenario_full", CHECK, {"tally_records": 22, "tally_compared": 19, "markets": 22}),
    ("scenario_cut", CHECK, {"compared_markets": 20, "changed_outcomes": 19}),
    ("scenario_deleted", CHECK, {"compared_markets": 20, "changed_outcomes": 17}),
    ("scenario_revised", CHECK, {"compared_markets": 20, "changed_outcomes": 16}),
    ("second_point", CHECK, {"markets": 22}),
    ("settlement_witness", CHECK, {"points": 2}),
    ("market_conditions", CHECK, {"markets": 28}),
    ("sources_named_file_decides", CHECK, {}),
    ("sources_primary_rules", CHECK, {"worlds": 6}),
    ("membership_gates", CHECK, {"refused_members": 2}),
    ("duplicate_across_archives", NEGATIVE, {}),
    ("limit_small_exact", CHECK, {}),
    ("limit_small_over", CHECK, {}),
    ("limit_batch_exact", CHECK, {}),
    ("limit_batch_inside", CHECK, {}),
    ("limit_64mib_exact", CHECK, {}),
    ("limit_64mib_over", CHECK, {}),
    ("read_limits_exact", CHECK, {}),
    ("read_limits_unexercised", CHECK, {}),
    ("abstaining_incomplete", INCOMPLETE, {}),
    ("unexercised_incomplete", INCOMPLETE, {}),
    ("nc_future_leak", NEGATIVE, {}),
    ("nc_pre_t_change", NEGATIVE, {}),
    ("nc_borrowed_rows", NEGATIVE, {}),
    ("nc_ledger_byte", NEGATIVE, {}),
    ("nc_swapped_bets", NEGATIVE, {}),
    ("nc_dropped_decision", NEGATIVE, {}),
    ("nc_short_settlement", NEGATIVE, {}),
    ("nc_decision_without_record", NEGATIVE, {}),
    ("nc_duplicate_record", NEGATIVE, {}),
    ("nc_other_variant_row", NEGATIVE, {}),
    ("nc_settlement_other_variants", NEGATIVE, {}),
    ("nc_wrong_decision_time", NEGATIVE, {}),
    ("nc_member_tampered", NEGATIVE, {}),
    ("nc_markets_changed", NEGATIVE, {}),
    ("nc_same_key_substitution", NEGATIVE, {}),
    ("nc_rejected_tally_addition", NEGATIVE, {}),
)
CASE_IDS = tuple(case_id for case_id, _, _ in CASES)
# Each registry count, read from a case's observed projection.
COUNTERS = {
    "kat_cases": lambda observed: observed["cases"],
    "tally_records": lambda observed: observed["tally"]["records"],
    "tally_compared": lambda observed: observed["tally"]["compared"],
    "markets": lambda observed: len(observed["markets"]),
    "compared_markets": lambda observed: observed["compared_markets"],
    "changed_outcomes": lambda observed: len(observed["distinct"]["changed_outcomes"]),
    "points": lambda observed: len(observed),
    "worlds": lambda observed: len(observed),
    "refused_members": lambda observed: len(observed["refused"]),
}


class CaseProblem(Exception):
    """A case could not run as built (a fixture refused where it should not be): an error, never a pass."""


# --- material -------------------------------------------------------------------------------------------------------

class _Material:
    """One world written under a case directory, ingested and indexed, and evaluated as the runner does."""

    def __init__(self, directory, world, rules, limits=DEFAULT_LIMITS, ingest_status="ingested"):
        self.directory, self.rules, self.limits = directory, rules, limits
        if not directory.exists():                  # a sub-world of a case: its own new directory
            directory.mkdir()
        self.root = directory / "data"
        self.root.mkdir()
        self.ingests = []
        for index, tar in enumerate(world):
            target = directory / f"archive-{index}.tar"
            with target.open("xb") as handle:
                handle.write(ustar(tar))
            result = ingest_archive(target, self.root, limits)
            self.ingests.append(result)
            if result["status"] != ingest_status:
                raise CaseProblem(f"ingest:{result['status']}:{result.get('problems')}")
        indexed = index_archives(self.root, limits=limits)
        if indexed["status"] != "indexed":
            raise CaseProblem(f"index:{indexed['status']}")
        self.archives, self.descriptor, self.input_sha256 = runner._inputs(self.root)
        self.manifests = {archive["archive_sha256"]: archive["manifest"] for archive in self.archives}

    def records(self):
        return [record for archive in self.archives for record in runner._records(archive)]

    def by_market(self):
        return {record["market_id"]: record for record in self.records()}

    def stubs(self):
        keys = sorted({(record["provenance"]["plan"], record["market_id"]) for record in self.records()})
        return [{"market_id": market_id, "provenance": {"plan": plan}} for plan, market_id in keys]

    def first_pass(self):
        return runner._first_pass(self.archives, self.rules[0])

    def evaluate(self):
        """(decisions by market, bets, settlement, tally result, reconciliation) as run_backtest computes them."""
        cohort, accounting_rules, bootstrap = self.rules
        _, _, keys = self.first_pass()
        decisions, bets, settlement, _, tally = runner._evaluate(self.archives, self.root, cohort, accounting_rules,
                                                                 bootstrap, self.limits, keys)
        reconciliation = issuance_exposure_reconciliation(cohort, self.stubs(), decisions,
                                                          b"".join(ledger_lines(decisions)), bets, settlement)
        return {d.market_id: d for d in decisions}, bets, settlement, tally, reconciliation

    def prefix(self, record, decision, prefix_limit=PREFIX_LIMIT_BYTES):
        manifest = self.manifests[record["provenance"]["archive_sha256"]]
        return check_prefix(self.rules[0], record, decision, self.root, manifest, self.limits, prefix_limit)


def _outcomes(row, variants):
    """[outcome, reason, detail, drawn] per variant; the seeded variant must carry drawn (cohort docstring)."""
    return {variant: [row["variants"][variant]["outcome"], row["variants"][variant]["reason"],
                      row["variants"][variant]["detail"],
                      row["variants"][variant]["drawn"] if variant == SEEDED else row["variants"][variant].get("drawn")]
            for variant in variants}


def _market(decision, variants):
    return {"population": decision.population, "reason": decision.reason,
            "rows": [[row["selection_id"], _outcomes(row, variants)] for row in decision.rows]}


def _market_in_full(decision, variants):
    """_market, with the decision's timing and each row's input, decision id, decision block and each variant's
    intent (null unless issued): an intent is checked on its own row, never only in a set of all intents."""
    return {"population": decision.population, "reason": decision.reason,
            "timing": [decision.anchor_pt, decision.anchor_ms, decision.decision_ms],
            "rows": [[row["selection_id"], _outcomes(row, variants), row["input"], row["decision_id"],
                      [row["decision"]["axis"], row["decision"]["decision_ms"], row["decision"]["anchor_pt"],
                       row["decision"]["anchor_ms"]],
                      {variant: row["variants"][variant]["intent"] for variant in variants}]
                     for row in decision.rows]}


def _intents(decisions, variants):
    found = {canonical_json(row["variants"][variant]["intent"]) for decision in decisions.values()
             for row in decision.rows for variant in variants if row["variants"][variant]["outcome"] == ISSUED}
    return [json.loads(item) for item in sorted(found)]


def _ledger_order(decisions):
    """[market id, selection id] of each issuance ledger line, in the ledger's order, read back from its bytes."""
    rows = [json.loads(line) for line in ledger_lines(decisions.values())]
    return [[row["market_id"], row["selection_id"]] for row in rows]


def _settlement_rules(settlement):
    rules = settlement.rules
    return [str(rules.commission_rate), rules.rounding, rules.stake_pence, list(rules.variants)]


def _prefix_projection(result):
    return [result["status"], result["reason"], None if result["prefix"] is None else result["prefix"]["boundary"]]


def _run_tally(tally):
    return {"status": tally["status"], "prefix_limit_bytes": tally["prefix_limit_bytes"],
            "max_line_bytes": tally["read_limits"]["max_line_bytes"], "compared": tally["counts"]["compared"],
            "unexercised_by_reason": tally["unexercised_by_reason"]}


def _anchor_by_t(decisions):
    return {mid for mid, decision in decisions.items() if decision.population not in (NO_VIEW, ANCHOR_NOT_KNOWN)}


# --- cases ----------------------------------------------------------------------------------------------------------

def _kat(directory):
    result = known_accounting_answers()
    return dict(result, case_ids=[case["id"] for case in KAT_CASES])


def _scenario_full(directory):
    material = _Material(directory, scenario_world("full"), p0_rules())
    variants = material.rules[0].variants
    decisions, bets, settlement, tally, reconciliation = material.evaluate()
    records = material.by_market()
    return {
        "markets": {mid: dict(_market_in_full(decisions[mid], variants),
                              prefix=_prefix_projection(material.prefix(records[mid], decisions[mid])))
                    for mid in sorted(decisions)},
        "intents": _intents(decisions, variants), "ledger": _ledger_order(decisions),
        "settlement": sorted([bet.variant, bet.market_id, bet.selection_id, bet.status, list(bet.reasons),
                              bet.settlement_case] for bet in settlement.bets),
        "settlement_rules": _settlement_rules(settlement),
        "tally": {key: tally["counts"][key] for key in ("records", "not_candidate", "candidates", "unexercised",
                                                       "compared", "passed", "failed", "missing")},
        "tally_status": tally["status"], "reconciliation_status": reconciliation["status"]}


_RESULT_STATUSES = ("LOSER", "PLACED", "WINNER")


def _bears_outcome(outcome):
    """Whether an evaluator_only_outcome holds anything outcome-bearing (market_index docstring: CLOSED, a
    settledTime, bspReconciled true, a runner WINNER, LOSER or PLACED, or any runner bsp)."""
    definition, runners = outcome["outcome_definition"], outcome["runners_after_cutoff"]
    return bool(outcome["closed_reached"] or outcome["settled_time"] is not None
                or outcome["winners_observed"] is not None
                or any(value is True for _, value in definition.get("bspReconciled", []))
                or any(value is not None for _, value in definition.get("settledTime", []))
                or any(value is not None for item in runners for _, value in item.get("bsp", []))
                or any(value in _RESULT_STATUSES for item in runners for _, value in item.get("status", []))
                or any(item["bsp"] is not None or item["status"] in _RESULT_STATUSES
                       for item in outcome["final_runners"]))


def _published_after(record, t):
    """The decision_time definition fields with an entry published after t, then "removals" when a removal is."""
    view = record["decision_time"]
    names = sorted(key for key, timeline in view["definition"].items() if any(pt > t for pt, _ in timeline))
    return names + (["removals"] if any(item["pt"] > t for item in view["removals"]) else [])


def _distinct(full, other, full_decisions, population):
    """How the other form's futures differ from the full form's, over the anchor-by-t population: whether the markets
    files differ (they always do between two archives), the records whose outcome changed or vanished, the
    population markets whose primary was read past t (last_pt), those bearing an outcome, and the decision_time
    definition fields and removals published after t."""
    full_records, other_records = full.by_market(), other.by_market()
    t = {mid: full_decisions[mid].decision_ms for mid in population}
    if any(value is None for value in t.values()):
        raise CaseProblem("forms:population_market_without_a_decision_time")
    outcomes = {mid: canonical_json(record["evaluator_only_outcome"]) for mid, record in full_records.items()}
    changed = sorted(mid for mid in outcomes if mid not in other_records
                     or canonical_json(other_records[mid]["evaluator_only_outcome"]) != outcomes[mid])
    flipped = {mid: _published_after(other_records[mid], t[mid]) for mid in sorted(population)
               if mid in other_records and t[mid] is not None}
    return {"markets_files_differ": ([a["markets_sha256"] for a in full.archives]
                                     != [a["markets_sha256"] for a in other.archives]),
            "changed_outcomes": changed,
            "after_t": sorted(mid for mid in population if mid in other_records and t[mid] is not None
                              and other_records[mid]["evaluator_only_outcome"]["last_pt"] > t[mid]),
            "outcome_bearing": sorted(mid for mid in population if mid in other_records
                                      and _bears_outcome(other_records[mid]["evaluator_only_outcome"])),
            "published_after_t": {mid: names for mid, names in flipped.items() if names}}


def _form_case(form):
    def case(directory):
        full = _Material(directory / "full", scenario_world("full"), p0_rules())
        other = _Material(directory / form, scenario_world(form), p0_rules())
        full_decisions, other_decisions = full.evaluate()[0], other.evaluate()[0]
        population = _anchor_by_t(full_decisions)
        same_population = _anchor_by_t(other_decisions) == population
        same_decisions = all(canonical_json(vars(full_decisions[mid])) == canonical_json(vars(other_decisions[mid]))
                             for mid in population if mid in other_decisions)
        same_ledger = (b"".join(ledger_lines(full_decisions[mid] for mid in sorted(population)))
                       == b"".join(ledger_lines(other_decisions[mid] for mid in sorted(population)
                                                if mid in other_decisions)))
        diagnostics = {mid: decision.population for mid, decision in sorted(other_decisions.items())
                       if mid not in population}
        return {"same_population": same_population, "same_decisions": same_decisions, "same_ledger": same_ledger,
                "diagnostics": diagnostics, "compared_markets": len(population) if same_population else 0,
                "distinct": _distinct(full, other, full_decisions, population)}
    return case


def _second_point(directory):
    material = _Material(directory, scenario_world("full"), second_point_rules())
    variants = material.rules[0].variants
    decisions, _, settlement, _, reconciliation = material.evaluate()
    return {"markets": {mid: _market_in_full(decisions[mid], variants) for mid in sorted(decisions)},
            "intents": _intents(decisions, variants), "ledger": _ledger_order(decisions),
            "settlement_rules": _settlement_rules(settlement), "reconciliation_status": reconciliation["status"]}


def _settlement_witness(directory):
    """One winning bet of each point's stake on runner 1 at the witness BSP, settled under that point's rules."""
    observed = {}
    for name, rules in (("p0", p0_rules()[1]), ("second_point", second_point_rules()[1])):
        bet = IssuedBet(decision_id="witness.0", variant=rules.variants[0], market_id=WITNESS_MARKET, selection_id=1,
                        stake_pence=rules.stake_pence, side="BACK", basis="BSP")
        settlement = settle([bet], [witness_outcome()], rules)
        (result,), (market,) = settlement.bets, settlement.markets
        observed[name] = {"rules": _settlement_rules(settlement)[:3], "bet": [result.status, result.gross_pence],
                          "market": [market.gross_pence, market.commission_pence, market.net_pence]}
    return observed


def _market_conditions(directory):
    material = _Material(directory, conditions_world(), p0_rules())
    decisions, _, _, _, reconciliation = material.evaluate()
    variants = material.rules[0].variants
    return {"markets": {mid: _market_in_full(decisions[mid], variants) for mid in sorted(decisions)},
            "intents": _intents(decisions, variants), "reconciliation_status": reconciliation["status"]}


def _favourite(decision):
    return next((row["selection_id"] for row in decision.rows
                 if row["variants"]["ltp_favourite"]["outcome"] == ISSUED), None)


def _sources(directory):
    decided = {}
    for change in ("none", "event_file", "market_file"):
        material = _Material(directory / change, sources_world(change), p0_rules())
        decisions = material.evaluate()[0]
        record = material.records()[0]
        decision = decisions[record["market_id"]]
        decided[change] = (canonical_json(vars(decision)), record["provenance"]["member_path"], _favourite(decision))
    alone = {}
    for change in ("none", "event_file"):
        material = _Material(directory / f"event-alone-{change}", event_alone_world(change), p0_rules())
        (decision,) = material.evaluate()[0].values()
        alone[change] = _favourite(decision)
    return {"primary": decided["none"][1], "event_file_change_same": decided["event_file"] == decided["none"],
            "market_file_favourites": [decided["none"][2], decided["market_file"][2]],
            "event_alone_favourites": [alone["none"], alone["event_file"]]}


def _primary_rules(directory):
    worlds = {}
    for name, world in source_rules_worlds().items():
        material = _Material(directory / name, world, p0_rules())
        decisions = material.evaluate()[0]
        worlds[name] = {
            "records": {mid: [record["provenance"]["member_path"], decisions[mid].population, decisions[mid].reason,
                              _favourite(decisions[mid])] for mid, record in sorted(material.by_market().items())},
            "refused_markets": sorted([item["market_id"], item["member_path"], item["reason"]]
                                      for archive in material.archives for item in archive["head"]["refused_markets"]),
            "refused_members": sorted([item["path"], item["reason"]] for archive in material.archives
                                      for item in archive["manifest"]["refused"])}
    return worlds


def _membership(directory):
    material = _Material(directory, membership_world(), p0_rules())
    refused = sorted([item["path"], item["reason"]] for item in material.manifests[
        material.archives[0]["archive_sha256"]]["refused"])
    decisions = material.evaluate()[0]
    return {"refused": refused, "decided": {mid: decision.population for mid, decision in decisions.items()}}


def _duplicate(directory):
    material = _Material(directory, duplicate_world(), p0_rules())
    try:
        material.first_pass()
    except runner.RunRefused as error:
        return {"refused": str(error)}
    return {"refused": None}


def _limit_case(retained, prefix_limit, batch_at_t=False, filler_line=1 << 20, limits=DEFAULT_LIMITS):
    def case(directory):
        material = _Material(directory, limit_world(retained, filler_line, batch_at_t), p0_rules(), limits)
        decisions, _, _, tally, _ = material.evaluate()
        decision = decisions[LIMIT_MARKET]
        result = material.prefix(material.by_market()[LIMIT_MARKET], decision, prefix_limit)
        return {"prefix": _prefix_projection(result), "prefix_limit_bytes": result["prefix_limit_bytes"],
                "kept": [result["prefix"]["bytes"], result["prefix"]["lines"], result["prefix"]["sha256"]],
                "decision": _market(decision, material.rules[0].variants), "run_tally": _run_tally(tally)}
    return case


def _incomplete_case(world):
    def case(directory):
        material = _Material(directory, world(), p0_rules())
        _, _, _, tally, reconciliation = material.evaluate()
        return {"tally_status": tally["status"], "tally_compared": tally["counts"]["compared"],
                "unissued": tally["unissued_variants"], "unexercised_by_reason": tally["unexercised_by_reason"],
                "reconciliation_status": reconciliation["status"],
                "unexercised_variants": reconciliation["unexercised_variants"]}
    return case


def _scenario(directory, form="full"):
    material = _Material(directory, scenario_world(form), p0_rules())
    decisions, bets, settlement, _, _ = material.evaluate()
    return material, decisions, bets, settlement


def _tallied(material, record, result, decision):
    """One result added to a PrefixTally over its record alone: what the run's tally makes of it."""
    tally = PrefixTally(material.rules[0].variants, [(record["provenance"]["plan"], record["market_id"])],
                        material.limits)
    tally.add(result, decision)
    out = tally.result()
    return {"status": out["status"], "failures": out["failures"], "counts_reconcile": out["counts_reconcile"],
            "counts": {key: out["counts"][key] for key in ("compared", "failed", "failed_against_record",
                                                           "missing")}}


def _nc_future_leak(directory):
    """The run's view of one market read 3 minutes past t (the post-t prices flip its favourite), relabelled t."""
    material, decisions, _, _ = _scenario(directory)
    record = material.by_market()["1.400000001"]
    manifest = material.manifests[record["provenance"]["archive_sha256"]]
    rules = material.rules[0]

    def leaky(item):
        view = replay_view(material.root, manifest, item, rules.offset_s + 180, material.limits)
        return replace(view, decision_ms=view.decision_ms - 180000)

    leaked = decide_market(rules, record, leaky)
    result = material.prefix(record, leaked)
    return {"leak_changes_decision": canonical_json(vars(leaked)) != canonical_json(vars(decisions["1.400000001"])),
            "prefix": _prefix_projection(result)}


def _nc_pre_t_change(directory):
    """A decision made on other pre-t prices (the sources world's market-file change), checked against this record."""
    base = _Material(directory / "base", sources_world("none"), p0_rules())
    changed = _Material(directory / "changed", sources_world("market_file"), p0_rules())
    record = base.records()[0]
    other = changed.evaluate()[0][record["market_id"]]
    result = base.prefix(record, other)
    return {"prefix": _prefix_projection(result), "tally": _tallied(base, record, result, other)}


def _nc_borrowed_rows(directory):
    material, decisions, bets, settlement = _scenario(directory)
    records = material.by_market()
    late, rules = decisions["1.400000012"], material.rules[0]
    borrowed = replace(late, rows=tuple(dict(row, market_id=late.market_id,
                                             decision_id=decision_id(rules, late.market_id, row["selection_id"]))
                                        for row in decisions["1.400000001"].rows))
    changed = [borrowed if d.market_id == late.market_id else d for d in decisions.values()]
    reconciliation = issuance_exposure_reconciliation(rules, material.stubs(), changed,
                                                      b"".join(ledger_lines(changed)), bets, settlement)
    return {"prefix": _prefix_projection(material.prefix(records["1.400000012"], borrowed)),
            "reconciliation_problems": [p for p in reconciliation["problems"] if p.startswith(("decisions:", "rows:"))],
            "reconciliation_status": reconciliation["status"]}


def _reconcile_with(change):
    """A reconciliation control: the scenario's full-form inputs to the reconciliation, with one thing changed."""
    def case(directory):
        material, decisions, bets, settlement = _scenario(directory)
        decided = sorted(decisions.values(), key=lambda d: d.market_id)
        item = {"records": material.stubs(), "decisions": decided, "ledger": b"".join(ledger_lines(decided)),
                "bets": list(bets), "settlement": settlement}
        change(item, material, decisions)
        result = issuance_exposure_reconciliation(material.rules[0], item["records"], item["decisions"],
                                                  item["ledger"], item["bets"], item["settlement"])
        return {"status": result["status"], "problems": result["problems"]}
    return case


def _flip_byte(item, material, decisions):
    ledger = item["ledger"]
    index = ledger.index(b'"ltp_favourite"')
    item["ledger"] = ledger[:index] + b'"ltp_favouritf"' + ledger[index + 15:]


def _swap_bet(item, material, decisions):
    first = item["bets"][0]
    item["bets"][0] = replace(first, selection_id=first.selection_id % 3 + 1)


def _drop_decision(item, material, decisions):
    item["decisions"] = [d for d in item["decisions"] if d.market_id != "1.400000013"]     # excluded: no rows
    item["ledger"] = b"".join(ledger_lines(item["decisions"]))


def _short_settlement(item, material, decisions):
    """Accounting settled every bet but the first (the bets list itself is right)."""
    kept, records = item["bets"][1:], material.by_market()
    outcomes = [market_outcome(records[mid], decisions[mid]) for mid in sorted({bet.market_id for bet in kept})]
    item["settlement"] = settle(kept, outcomes, material.rules[1])


def _drop_record(item, material, decisions):
    item["records"] = [stub for stub in item["records"] if stub["market_id"] != "1.400000001"]


def _duplicate_record(item, material, decisions):
    item["records"] = item["records"] + [dict(item["records"][0])]


def _other_variant_row(item, material, decisions):
    """One row of 1.400000001 with a third variant beside the contract's two; the ledger is that row's too."""
    def changed(decision):
        if decision.market_id != "1.400000001":
            return decision
        row = decision.rows[0]
        extra = dict(row, variants=dict(row["variants"], other_variant={"outcome": "not_selected", "reason": None,
                                                                         "detail": None, "intent": None}))
        return replace(decision, rows=(extra,) + decision.rows[1:])
    item["decisions"] = [changed(d) for d in item["decisions"]]
    item["ledger"] = b"".join(ledger_lines(item["decisions"]))


def _settlement_other_variants(item, material, decisions):
    """The settlement's rules declare one variant (its bets and markets are the run's)."""
    settlement, rules = item["settlement"], item["settlement"].rules
    one = AccountingRules(commission_rate=rules.commission_rate, rounding=rules.rounding,
                          settlement=dict(rules.settlement), stake_pence=rules.stake_pence,
                          variants=rules.variants[:1])
    item["settlement"] = replace(settlement, rules=one)


def _nc_wrong_decision_time(directory):
    material, decisions, _, _ = _scenario(directory)
    record = material.by_market()["1.400000001"]
    wrong = replace(decisions["1.400000001"], decision_ms=decisions["1.400000001"].decision_ms + 1)
    result = material.prefix(record, wrong)
    return {"prefix": _prefix_projection(result), "tally": _tallied(material, record, result, wrong)}


def _nc_member_tampered(directory):
    """A member changed after indexing (an excluded market's: only the prefix check reads it) fails the run."""
    material = _Material(directory, scenario_world("full"), p0_rules())
    record = material.by_market()["1.400000013"]
    raw = safe_target(material.root, record["provenance"]["archive_sha256"], record["provenance"]["member_path"])
    raw.chmod(0o600)
    raw.write_bytes(raw.read_bytes() + b"x")
    try:
        material.evaluate()
    except ReplayProblem as error:
        return {"refused": str(error)}
    return {"refused": None}


def _nc_markets_changed(directory):
    material = _Material(directory, scenario_world("full"), p0_rules())
    markets = material.archives[0]["markets_path"]
    markets.chmod(0o600)
    markets.write_bytes(markets.read_bytes().replace(b'{"', b'{ "', 1))
    try:
        material.first_pass()
    except runner.RunRefused as error:
        return {"refused": str(error).split(":", 2)[2] if str(error).startswith("input:") else str(error)}
    return {"refused": None}


def _nc_same_key_substitution(directory):
    material, decisions, _, _ = _scenario(directory)
    record = material.by_market()["1.400000001"]
    decision = decisions["1.400000001"]
    result = material.prefix(record, decision)
    rows = [dict(row) for row in decision.rows]
    rows[0] = dict(rows[0], input=dict(rows[0]["input"], ltp=999.0))
    tally = PrefixTally(material.rules[0].variants, [("BASIC", "1.400000001")], material.limits)
    try:
        tally.add(result, replace(decision, rows=tuple(rows)))
    except ValueError as error:
        refused = str(error)
    else:
        refused = None
    return {"refused": refused, "missing_after": tally.result()["counts"]["missing"]}


def _nc_rejected_tally_addition(directory):
    material, decisions, _, _ = _scenario(directory)
    records = material.by_market()
    keys = [("BASIC", "1.400000001"), ("BASIC", "1.400000013")]
    tally = PrefixTally(material.rules[0].variants, keys, material.limits)
    good = material.prefix(records["1.400000001"], decisions["1.400000001"])
    tally.add(good, decisions["1.400000001"])
    before = tally.result()
    second = material.prefix(records["1.400000013"], decisions["1.400000013"])
    try:
        tally.add(dict(second, stage="record"), decisions["1.400000013"])
    except ValueError as error:
        refused = str(error)
    else:
        refused = None
    unchanged = tally.result() == before
    tally.add(second, decisions["1.400000013"])
    return {"refused": refused, "unchanged": unchanged, "missing_after_retry": tally.result()["counts"]["missing"]}


RUNNERS = {
    "kat": _kat, "scenario_full": _scenario_full, "scenario_cut": _form_case("cut"),
    "scenario_deleted": _form_case("deleted"), "scenario_revised": _form_case("revised"),
    "second_point": _second_point, "settlement_witness": _settlement_witness,
    "market_conditions": _market_conditions, "sources_named_file_decides": _sources,
    "sources_primary_rules": _primary_rules, "membership_gates": _membership,
    "duplicate_across_archives": _duplicate,
    "limit_small_exact": _limit_case(4096, 4096), "limit_small_over": _limit_case(4097, 4096),
    "limit_batch_exact": _limit_case(4096, 4096, batch_at_t=True),
    "limit_batch_inside": _limit_case(4096, 4095, batch_at_t=True),
    "limit_64mib_exact": _limit_case(PREFIX_LIMIT_BYTES, PREFIX_LIMIT_BYTES, batch_at_t=True),
    "limit_64mib_over": _limit_case(PREFIX_LIMIT_BYTES + 1, PREFIX_LIMIT_BYTES, batch_at_t=True),
    "read_limits_exact": _limit_case(4096, PREFIX_LIMIT_BYTES, filler_line=1024,
                                     limits=DEFAULT_LIMITS.stricter(max_line_bytes=1024)),
    "read_limits_unexercised": _limit_case(4096, PREFIX_LIMIT_BYTES, filler_line=1025,
                                           limits=DEFAULT_LIMITS.stricter(max_line_bytes=1024)),
    "abstaining_incomplete": _incomplete_case(abstaining_world),
    "unexercised_incomplete": _incomplete_case(unexercised_world),
    "nc_future_leak": _nc_future_leak, "nc_pre_t_change": _nc_pre_t_change, "nc_borrowed_rows": _nc_borrowed_rows,
    "nc_ledger_byte": _reconcile_with(_flip_byte), "nc_swapped_bets": _reconcile_with(_swap_bet),
    "nc_dropped_decision": _reconcile_with(_drop_decision),
    "nc_short_settlement": _reconcile_with(_short_settlement),
    "nc_decision_without_record": _reconcile_with(_drop_record),
    "nc_duplicate_record": _reconcile_with(_duplicate_record),
    "nc_other_variant_row": _reconcile_with(_other_variant_row),
    "nc_settlement_other_variants": _reconcile_with(_settlement_other_variants),
    "nc_wrong_decision_time": _nc_wrong_decision_time, "nc_member_tampered": _nc_member_tampered,
    "nc_markets_changed": _nc_markets_changed, "nc_same_key_substitution": _nc_same_key_substitution,
    "nc_rejected_tally_addition": _nc_rejected_tally_addition,
}


def _shows_failure(observed):
    """A negative control's projection shows a refusal, a failed status or a failed prefix result."""
    return isinstance(observed, dict) and (
        observed.get("refused") is not None
        or "failed" in (observed.get("status"), observed.get("tally_status"), observed.get("reconciliation_status"))
        or (observed.get("prefix") or [None])[0] == "failed")


def _shows_incomplete(observed):
    return (isinstance(observed, dict)
            and (observed.get("tally_status"), observed.get("reconciliation_status")) == ("incomplete", "incomplete"))


def _counts_are(counts, observed):
    try:
        return all(COUNTERS[name](observed) == value for name, value in counts.items())
    except (KeyError, TypeError):
        return False


def run_case(case_id, directory):
    """One case's result: {id, kind, result, observed, expected} (module docstring); directory must not exist and
    must lie under a work root the caller has validated (module docstring)."""
    kind, counts = next((k, c) for i, k, c in CASES if i == case_id)
    directory = Path(directory)
    directory.mkdir()
    expected = EXPECTED.get(case_id)
    try:
        observed = RUNNERS[case_id](directory)
    except Exception as error:                      # a case never passes on an exception it did not ask for
        return {"id": case_id, "kind": kind, "result": ERROR, "observed": f"{type(error).__name__}:{error}"[:300],
                "expected": expected}
    same = expected is not None and canonical_json(observed) == canonical_json(expected)
    holds = {NEGATIVE: _shows_failure, INCOMPLETE: _shows_incomplete}.get(kind, lambda _: True)(observed)
    return {"id": case_id, "kind": kind,
            "result": AS_EXPECTED if same and holds and _counts_are(counts, observed) else NOT_AS_EXPECTED,
            "observed": observed, "expected": expected}


def run_cases(work_root):
    """Every registered case, in registry order, each in its own new directory under work_root, which the caller
    must have validated (module docstring)."""
    if (set(RUNNERS) != set(CASE_IDS) or set(EXPECTED) != set(CASE_IDS)
            or len(set(CASE_IDS)) != len(CASE_IDS)):
        raise CaseProblem("certify:registry_mismatch")
    work_root = Path(work_root)
    return [run_case(case_id, work_root / case_id) for case_id in CASE_IDS]
