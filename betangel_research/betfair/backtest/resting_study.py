"""python3 -m betangel_research.betfair.backtest.resting_study: run registered measurement close-v1.

--study close-v1 --pass blind|registered --registration-sha256 HEX --archive-root DIR --out DIR --code-commit HEX
--ledger DIR --ledger-head none|SEQ:SHA256 [--blind-receipt PATH] [--registration PATH] runs
research/studies/close-v1.json (or --registration) over every archive ingested and indexed under the data root DIR
and writes summary.json, summary.md (totals only) and receipt.json into the new directory --out, through
"<out>.partial" (mode 0700, files 0600) renamed only when all three are whole. It prints one JSON line of counts,
codes and hashes and exits 0 (completed) or 2 (refused or failed: --out is not created then). A fault in the code
itself prints the failure internal:<exception class name> and nothing else of the error (main): a traceback or a
message can hold a key, and a key can be an id. An "order" here is a simulated intent inside the backtest package;
nothing here can reach an exchange. It is a module of its own, not a subcommand of the backtest CLI, because
__main__.py is one of the certified engine's sources and stays byte-identical; this module, resting.py and fills.py
are in the certificate's source descriptor, so adding them changed the certificate key again.

Refusals, before any record is read, in this order: --study is not close-v1 (nothing is read then); the pass name;
the registration file's raw bytes must hash to --registration-sha256, the file must lie in a folder named studies,
and the split file beside that folder and the rule files in rules/ beside it, each a regular file of at most 1 MiB
named by the registration, must be the bytes it names (resting.parse_registration); --code-commit, the data root
and --out as movement_study checks them; the blind pass takes no --blind-receipt and the registered pass needs the
blind pass's canonical, completed receipt of this study, registration, split file, --code-commit, backtest-source
sha256 and input sha256; every manifest's read limits must be the replay's. Then the study ledger
(study_ledger.py): --out may not lie inside the ledger directory nor hold it; the ledger is opened with the head the
operator typed, and for a registered pass with the blind receipt's ledger head as the anchor, where the entry at
that seq must be a look of this study, registration, input, backtest sources and commit; then the budgets are
checked. A pass whose identity the ledger already holds is a repeat.

The read is lp_study's: every record's months pass the current month gate (sealed months are never read), a market
id seen twice in the registered tier fails the run (duplicate_market), and the member's path is guarded and its
bytes hashed against the manifest. What the index record alone says of a record is read first (its month, its race
date and partition label, each cell's code: resting.record_code); only those calls report malformed_record. A
record whose cells all have a code, one with an unregistered label included, is never opened. Any other record is
walked once (resting.walk) to its last pull time. Value rows hold no market or selection id, are kept in memory by
cell and partition label, and never reach disk.

After the read the summary's bytes are built in memory; then the ledger gets its one entry (a look for a blind
pass, the registered entry for a registered pass) before any file or stdout write; then the outputs are written. A
repeat appends nothing, and its summary must hash to the stored tables hash (ledger:repeat_differs otherwise). A
pass that fails or is stopped during the read has appended and written nothing. If an output cannot be written
after the append, the entry stands, the result names the new head, and the re-run is a repeat. The summary has no
clock, so the same registration, input and code give the same bytes. The receipt names the hashes, the commit, the
ledger head after the pass, the lineage's counts of looks and registered passes, started_at and recorded_at, and
two totals: bytes_walked, the compressed bytes the walks consumed, and bytes_drained, the rest of those members,
hashed and not parsed.

Tables. Both passes (registration blind_pass.releases): B1 coverage by registered month and partition label,
records with no anchor and with no registered label; per cell B2 the codes, B3 the classes by label, B4 the fill
counts by label and model and the fill counts of the classes that are not scored, B5 the sample SD and symmetric
ranges of gross g at the BSP as written over each model's filled part (both labels pooled, part and rest), the SD
over all scored orders, quantiles of the size shown at t ahead of the order and of the seconds to a fill, B6 the
detectable effect on the test label, B7 the diagnostics' counts. The blind summary holds no location of an
outcome: no average, middle value, raw quantile, sign, interval, test, label or selection
(tests/test_backtest_resting.py walks every key and the Markdown). The registered pass adds: T5 the
intent-to-treat figures per cell and label at both BSP ends (resting.figures); T7 the same by half-year, every cell
on the explore label and the primaries on the test label; T8 the selection; T9 per primary the test, Holm, both
bounds, the gap, the null and the label; T10 the filled figures without the fills in the first registered second,
and the quantiles of the sum of 1 / BSP per market with a scored order. A p-value, test or label exists only under
a primary; every other figure is descriptive. Part and rest is also applied across a split: a half-year's figure is
given only when the other half-year gives it too, and a kill's pooled figures only when each label holds
statistics.min_values of the orders they are over (the label a kill decided is printed either way). Markdown
tables print 100 x g as points of stake; summary.json keeps fractions of stake, floats rounded to 6 places.

Standard library only; it imports the backtest modules named above and nothing outside the package but what they
import (tests/test_betfair_backtest_boundaries.py). It never logs in, reads an account or sends anything.
"""

import argparse
import bz2
from dataclasses import asdict, replace
import hashlib
import json
import math
import sys

from . import fills
from .archive import DEFAULT_LIMITS, REPOSITORY_ROOT, ArchiveProblem, HashingReader, check_data_root, month_gate, \
    safe_target, utc_month
from .certificate import CertificateProblem, source_descriptor
from .contract import canonical_json
from .lp import FILL_KINDS, bsp_book
from .market_index import loads_strict
from .movement import ISSUED, MAX_REGISTRATION_BYTES, RegistrationError, registration_sha256
from .movement_study import (COMMIT_RE, MAX_RECEIPT_BYTES, SHA256_RE, StudyRefused, _failure, _guarded, _out_path,
                             _read_regular, _refuse, _table, _write)
from .partition import HALVES, MAX_SPLIT_BYTES
from .replay import ReplayProblem, _member
from .resting import (AS_WRITTEN, BSP_ENDS, CLASSES, ENDS, FILLED_IS_ALL_PLACED, FLAG_GATES, G, MEASURES,
                      NET_CLOSED, NET_HELD, NULL_UNDEFINED, SCORED, STUDY_ID, TRD_COUNTS, OrderRow, blind_outcome,
                      described, ends_apart, figures, filled_part, gap, named_files, parse_registration,
                      quantile_table, race_label, record_code, rounded, scored, select_primaries, series, spread,
                      verdicts, walk, whole_hundredths, without_first_fills)
from .rulefile import MAX_RULE_BYTES
from .runner import RunRefused, _inputs, _record_months, _records, utc_now_text
from .study_ledger import COMPLETED, LOOK, PassRequest, StudyLedger, head_text, identity, ledger_dir, parse_head
from .study_ledger import REGISTERED as REGISTERED_KIND
from .trial_ledger import LedgerError, UnlockError

SUMMARY_KIND, RECEIPT_KIND, SCHEMA_VERSION = "betangel_resting_summary", "betangel_resting_receipt", 1
BLIND, REGISTERED = "blind", "registered"
PASSES = (BLIND, REGISTERED)
STUDIES = (STUDY_ID,)                               # the studies this module runs
REGISTRATIONS = REPOSITORY_ROOT / "research" / "studies"
STUDIES_FOLDER, RULES_FOLDER = "studies", "rules"
SUMMARY_JSON, SUMMARY_MD, RECEIPT_JSON = "summary.json", "summary.md", "receipt.json"
WITHHELD = ("every location statistic of an outcome (an average, a middle value, a raw quantile or a sign), every "
            "interval, test, label and selection, any SD of a figure after commission, the SD over unfilled orders, "
            "and any statistic by price band or half-year: the registration's blind_pass.withholds")
INDEPENDENCE = "under independence; clustering makes it larger"
EXPLORATORY, NOT_TESTED = "exploratory", "not tested, unadjusted"
NO_ANCHOR, OUTSIDE, NO_LABEL = "no_anchor", "outside_registered_months", "no_registered_label"
REST_NOT_GIVEN = "rest_not_given"                   # a figure withheld because another part of its split is
# What part and rest withholds of a figure: every statistic, never a count.
STATISTICS = ("mean", "estimate", "se", "df", "t_critical", "ci_low", "ci_high", "sd", "bound")
RECORD_ERRORS = (KeyError, TypeError, AttributeError, IndexError, ValueError)


def _plan(study, path, expected):
    """(Plan, the split file's raw bytes, the registered alpha as its decimal text) of the registration and the
    files it names, after the checks in the module docstring; StudyRefused otherwise."""
    if not (isinstance(expected, str) and SHA256_RE.fullmatch(expected)):
        _refuse("registration_sha256:malformed")
    path = REGISTRATIONS / f"{study}.json" if path is None else path
    raw = _read_regular(path, "registration", MAX_REGISTRATION_BYTES)
    if registration_sha256(raw) != expected:
        _refuse("registration:sha256_mismatch")
    folder = _guarded(path, "registration").parent
    if folder.name != STUDIES_FOLDER:
        _refuse("registration:not_in_a_studies_folder")
    try:
        split_name, rule_names = named_files(raw)
        split_raw = _read_regular(folder.parent / split_name, "split", MAX_SPLIT_BYTES)
        rules_raw = {name: _read_regular(folder.parent / RULES_FOLDER / name, "rule_file", MAX_RULE_BYTES)
                     for name in rule_names}
        plan = parse_registration(raw, split_raw, rules_raw)
    except RegistrationError as error:
        raise StudyRefused(str(error)) from None
    return plan, split_raw, loads_strict(raw)["family"]["alpha"]


def _blind_receipt(path, plan):
    """(receipt, sha256 of its bytes, its ledger head as (seq, sha256)) of a completed blind-pass receipt of this
    registration and split file; StudyRefused otherwise."""
    if path is None:
        _refuse("blind_receipt:missing")
    data = _read_regular(path, "blind_receipt", MAX_RECEIPT_BYTES)
    try:
        receipt = loads_strict(data)
    except (ValueError, UnicodeDecodeError, RecursionError):
        _refuse("blind_receipt:not_json")
    if not isinstance(receipt, dict) or canonical_json(receipt) + b"\n" != data:
        _refuse("blind_receipt:not_canonical")
    if ((receipt.get("kind"), receipt.get("schema_version"), receipt.get("study"), receipt.get("pass"),
         receipt.get("status")) != (RECEIPT_KIND, SCHEMA_VERSION, plan.study, BLIND, "completed")):
        _refuse("blind_receipt:not_a_completed_blind_receipt")
    if receipt.get("registration_sha256") != plan.sha256:
        _refuse("blind_receipt:another_registration")
    if receipt.get("partitions_sha256") != plan.split.sha256:
        _refuse("blind_receipt:another_split")
    try:
        anchor = parse_head(receipt.get("ledger_head"))
    except LedgerError:
        anchor = None
    if anchor is None:
        _refuse("blind_receipt:ledger_head_malformed")
    return receipt, hashlib.sha256(data).hexdigest(), anchor


def _ledger(directory, expected, root, target, request, split_raw, anchor):
    """(the opened StudyLedger, whether this pass is a repeat) after the ledger's checks in the module docstring;
    LedgerError or StudyRefused otherwise. anchor: the blind receipt's ledger head, for a registered pass."""
    resolved = ledger_dir(directory, root)
    if resolved in target.parents or target == resolved or target in resolved.parents:
        _refuse("out:not_apart_from_the_ledger")
    ledger = StudyLedger.open(directory, expected, root=root, anchor=anchor)
    if anchor is not None and identity(ledger.entries()[anchor[0]]) != identity(replace(request, kind=LOOK)):
        _refuse("blind_receipt:not_this_ledgers_look")
    return ledger, ledger.check(request, split_raw) is not None


class Tally:
    """Everything the summary needs. rows: {Cell: {partition label: [OrderRow]}}, every placed order of every
    class; no market or selection id enters it."""

    def __init__(self, plan):
        self.records = self.replayed = self.bytes_walked = self.bytes_drained = 0
        self.months = {month: {"records": dict.fromkeys(plan.labels + (NO_LABEL,), 0),
                               "orders": dict.fromkeys(plan.labels, 0)}
                       for month in plan.months + (OUTSIDE, NO_ANCHOR)}
        self.codes = {cell: dict.fromkeys(plan.codes + (ISSUED,), 0) for cell in plan.cells}
        self.rows = {cell: {label: [] for label in plan.labels} for cell in plan.cells}
        self.flags_missing = {cell: {name: 0 for name, _, _ in FLAG_GATES
                                     if getattr(plan.rule(cell.rule_id).market, name) is True}
                              for cell in plan.cells}
        self.books = {label: [] for label in plan.labels}       # the sum of 1 / BSP of a market with a scored order


def _walk(root, manifest, record, plan, limits):
    """(resting.walk's answer over the record's primary member, the compressed bytes the walk consumed, the bytes
    drained after it), after replay_view's checks of the member; the member's bytes are hashed against the
    manifest (lp_study._walk's checks)."""
    market_id = record["market_id"]
    member = _member(manifest, record)
    try:
        raw = safe_target(root, manifest["archive"]["sha256"], member["path"])
    except ArchiveProblem as error:
        raise ReplayProblem(f"replay:{market_id}:{error}") from None
    raw = _guarded(raw, "raw")
    try:
        if not raw.is_file():
            raise ReplayProblem(f"replay:{market_id}:member_missing")
        with open(raw, "rb") as handle:
            reader = HashingReader(handle)
            with bz2.BZ2File(reader) as stream:
                walked = walk(stream, record, plan, limits)
            consumed = reader.size
            digest = reader.drain()
    except OSError:
        raise ReplayProblem(f"replay:{market_id}:member_unreadable_file") from None
    if digest != (member["sha256"], member["size_bytes"]):
        raise ReplayProblem(f"replay:{market_id}:member_hash_mismatch")
    return walked, consumed, digest[1] - consumed


def record_facts(plan, record):
    """What the index record alone says: (its coverage row, or None for another plan; resting.race_label's
    answer; {Cell: its code, or None when the cell reaches the book}). These are the only calls whose errors
    are reported as a malformed record."""
    month = None
    if record["provenance"]["plan"] in plan.tiers:
        first = record["decision_time"]["first_published_market_time"]
        month = None if first is None else utc_month(first["value"])
        if month is None:
            month = NO_ANCHOR
        elif not plan.start_month <= month <= plan.end_month:
            month = OUTSIDE
    return month, race_label(plan, record), {cell: record_code(plan, record, cell)[0] for cell in plan.cells}


def decide_record(plan, record, facts, root, manifest, tally, limits=DEFAULT_LIMITS):
    """Add one index record to the tally: its codes, and one row per placed order (module docstring)."""
    month, dated, outcomes = facts
    tally.records += 1
    label = None if dated is None else dated[1]
    if month is not None:
        tally.months[month]["records"][label if label in plan.labels else NO_LABEL] += 1
    if any(code is None for code in outcomes.values()):
        walked, consumed, drained = _walk(root, manifest, record, plan, limits)
        tally.replayed += 1
        tally.bytes_walked += consumed
        tally.bytes_drained += drained
        outcomes = walked.outcomes
        for cell, names in walked.flags_missing.items():
            for name in names:
                tally.flags_missing[cell][name] += 1
    valued = False
    for cell in plan.cells:
        row = outcomes[cell]
        if not isinstance(row, OrderRow):
            tally.codes[cell][row] += 1
            continue
        tally.codes[cell][ISSUED] += 1
        tally.months[month]["orders"][row.label] += 1
        tally.rows[cell][row.label].append(row)
        valued = valued or row.klass == SCORED
    book = bsp_book(record) if valued else None
    if book is not None:
        tally.books[label].append(float(book))


# ---------------------------------------------------------------------------------------------------------------
# Tables

def _key(cell):
    """The text a table names a cell by: its rule file and its time."""
    return f"{cell.rule_id}@{cell.at_s}"


def _both(plan, tally, cell):
    """A cell's rows of both partition labels, the explore label's first."""
    return [row for label in plan.labels for row in tally.rows[cell][label]]


def _fill_counts(plan, rows, model):
    """B4 for the scored rows of one cell and label under one model: counts and the fill rate."""
    found = [row.fills[model] for row in rows]
    filled = [fill for fill in found if fill.kind is not None]
    return {"scored": len(rows), "fills": len(filled), "fill_rate": len(filled) / len(rows) if rows else None,
            "by_kind": {kind: sum(1 for fill in filled if fill.kind == kind) for kind in FILL_KINDS},
            "within_s_of_t": {str(seconds): sum(1 for fill in filled if fill.within[index])
                              for index, seconds in enumerate(plan.within_s)},
            "after_the_hour": {kind: sum(1 for fill in filled if fill.kind == kind and fill.after_the_hour)
                               for kind in FILL_KINDS},
            "left_the_market_not_open": sum(1 for fill in filled if fill.transition),
            "ended_by": {end: sum(1 for fill in found if fill.ended == end) for end in ENDS}}


def _cell_tables(plan, tally, cell):
    """Tables B2 to B7 of one cell (module docstring): counts, and of an outcome its spread only."""
    by_label, pooled = tally.rows[cell], _both(plan, tally, cell)
    good, behind = scored(pooled), plan.models[-1]
    return {
        "rule": cell.rule_id, "side": plan.rule(cell.rule_id).intents[0].side, "at_s": cell.at_s,
        "pull_s": cell.pull_s, "codes": dict(tally.codes[cell]),
        "classes": {label: {klass: sum(1 for row in rows if row.klass == klass) for klass in CLASSES}
                    for label, rows in by_label.items()},
        "fills": {label: {model: _fill_counts(plan, scored(rows), model) for model in plan.models}
                  for label, rows in by_label.items()},
        "fills_not_scored": {label: {klass: {model: sum(1 for row in rows if row.klass == klass
                                                         and row.fills[model].kind is not None)
                                             for model in plan.models} for klass in CLASSES if klass != SCORED}
                             for label, rows in by_label.items()},
        "spread_of_g": {
            "bsp_end": AS_WRITTEN,
            "all_scored": {key: item for key, item in spread(plan, series(plan, good, G, AS_WRITTEN)).items()
                           if key != "ranges"},
            "filled": {model: blind_outcome(plan, pooled, model, AS_WRITTEN) for model in plan.models}},
        "queue_at_t": {"model": behind, "orders": len(pooled),
                       "queue_quantiles": quantile_table(plan, [row.fills[behind].queue for row in pooled])},
        "seconds_to_fill": {model: _seconds(plan, [row.fills[model].seconds for row in good
                                                   if row.fills[model].kind is not None])
                            for model in plan.models},
        "trd_changes": {name: sum(row.trd[name] for row in pooled) for name in TRD_COUNTS},
        "null_values": {"defined": sum(1 for row in good if row.null_mid is not None),
                        "undefined": {reason: sum(1 for row in good if row.null_reason == reason)
                                      for reason in NULL_UNDEFINED}},
        "flags_missing_at_t": dict(tally.flags_missing[cell]),
    }


def _seconds(plan, values):
    return {"fills": len(values), "seconds_quantiles": quantile_table(plan, values)}


def _together(parts):
    """Part and rest across the parts of one split. parts: {part: a tree of figures}, every tree of one shape. A
    figure (a dict with a mean or an estimate) is given for a part only when every part gives it; else each part
    keeps its counts and loses its statistics, so that a small part's figure cannot be worked out from the
    whole's and the other part's."""
    first = next(iter(parts.values()))
    if not isinstance(first, dict):
        return parts
    value = next((key for key in ("mean", "estimate") if key in first), None)
    if value is None:
        merged = {key: _together({name: item[key] for name, item in parts.items()}) for key in first}
        return {name: {key: merged[key][name] for key in first} for name in parts}
    if all(item[value] is not None for item in parts.values()):
        return parts
    return {name: {key: None if key in STATISTICS else REST_NOT_GIVEN if key == "reason" and item[value] is not None
                   else entry for key, entry in item.items()} for name, item in parts.items()}


def _kill(plan, kill, rows):
    """A primary's kill block as it is printed. Its figures are over both labels pooled, so they are given only
    when each label holds min_values of those orders (part and rest); figures_given says which."""
    part = filled_part(plan, scored(rows), fills.OPTIMISTIC)
    sizes = {label: sum(1 for row in part.rows if row.label == label) for label in plan.labels}
    if min(sizes.values()) >= plan.min_values:
        return dict(kill, orders_by_label=sizes, figures_given=True)
    return dict({key: None if key in STATISTICS else item for key, item in kill.items()}, orders_by_label=sizes,
                both={end: dict.fromkeys(nets) for end, nets in kill["both"].items()}, figures_given=False)


def _exploratory(cells):
    """What every exploratory table prints: its number of cells c and sqrt(2 ln c), about the largest of c
    independent standard scores."""
    return {"cells": cells, "sqrt_2_ln_c": math.sqrt(2 * math.log(cells))}


def _registered(plan, tally, apart):
    """Tables T5 and T7 to T10 of the registered pass (module docstring). The selection is given the explore
    label's rows and two counts per cell from the test label, nothing else."""
    def ends(rows):
        return {end: figures(plan, rows, end) for end in BSP_ENDS}

    def later(rows, model):
        found, stands = without_first_fills(plan, rows, model)
        return {"filled_figure_stands": stands, "ends": {end: {what: described(plan, series(plan, found, what, end))
                                                               for what in (G, NET_HELD)} for end in BSP_ENDS}}

    explore, test = plan.labels
    counts = {}
    for cell in plan.cells:
        part = filled_part(plan, scored(tally.rows[cell][test]), plan.select_test_model)
        counts[cell] = (part.fills, part.fill_dates)
    selection = select_primaries(plan, {cell: tally.rows[cell][explore] for cell in plan.cells}, counts, apart)
    chosen = [cell for cell in selection.primaries.values() if cell is not None]
    found = verdicts(plan, selection.primaries, {cell: _both(plan, tally, cell) for cell in plan.cells}, apart)
    primaries = {}
    for rule_id, verdict in found.items():
        primaries[rule_id] = verdict and dict(
            verdict, cell=_key(verdict["cell"]), half_years=_together(verdict["half_years"]),
            kill=_kill(plan, verdict["kill"], _both(plan, tally, verdict["cell"])),
            gap=gap(plan, scored(tally.rows[verdict["cell"]][test]), fills.PESSIMISTIC, verdict["test"]["bsp_end"]))
    headings = {explore: EXPLORATORY, test: NOT_TESTED}
    return {
        "figures": dict(_exploratory(len(plan.cells)), headings=headings, by_cell={
            _key(cell): {label: ends(scored(tally.rows[cell][label])) for label in plan.labels}
            for cell in plan.cells}),
        "half_years": dict(_exploratory(len(plan.cells) * len(HALVES)), headings=headings, by_cell={
            _key(cell): {label: _together({half: ends(scored(tally.rows[cell][label], half=half))
                                           for half in HALVES})
                         for label in plan.labels if label == explore or cell in chosen}
            for cell in plan.cells}),
        "selection": {"eligible": [_key(cell) for cell in selection.eligible],
                      "primaries": {rule_id: cell and _key(cell) for rule_id, cell in selection.primaries.items()}},
        "primaries": primaries,
        "sensitivity": dict(_exploratory(len(plan.cells)), headings=headings, without_first_fills={
            _key(cell): {label: {model: later(scored(tally.rows[cell][label]), model) for model in plan.models}
                         for label in plan.labels} for cell in plan.cells},
            bsp_book={label: {"markets": len(values), "quantiles": quantile_table(plan, values)}
                      for label, values in tally.books.items()}),
    }


def summary(plan, tally, pass_name, input_sha256):
    """The summary of one pass (module docstring), every float rounded; the blind pass's holds no location of an
    outcome."""
    months = tally.months
    whole, of = whole_hundredths(plan, [row for cell in plan.cells for row in _both(plan, tally, cell)])
    apart = ends_apart(plan, whole, of)
    value = {"kind": SUMMARY_KIND, "schema_version": SCHEMA_VERSION, "study": plan.study, "pass": pass_name,
             "registration_sha256": plan.sha256, "partitions_sha256": plan.split.sha256,
             "input_sha256": input_sha256,
             "partitions": {"explore_label": plan.explore_label, "test_label": plan.test_label},
             "records": tally.records, "replayed_markets": tally.replayed,
             "coverage": {"by_month": {month: {"records": dict(row["records"]), "orders": dict(row["orders"])}
                                       for month, row in months.items()},
                          "records_without_an_anchor": sum(months[NO_ANCHOR]["records"].values()),
                          "records_without_a_registered_label": sum(row["records"][NO_LABEL]
                                                                    for month, row in months.items()
                                                                    if month != NO_ANCHOR)},
             "cells": {_key(cell): _cell_tables(plan, tally, cell) for cell in plan.cells},
             "bsp_precision": {"scored_bsps_whole_hundredths": whole, "scored_bsps_not_whole_hundredths": of - whole,
                               "bsp_ends_decide_apart": apart}}
    if pass_name == REGISTERED:
        value.update(_registered(plan, tally, apart))
    else:
        value["withheld"] = WITHHELD
    return rounded(value)


# ---------------------------------------------------------------------------------------------------------------
# Markdown

def _points(value):
    """A fraction of stake as points of stake (100 x), to 4 places."""
    return None if value is None else round(100 * value, 4)


def _figure_cells(item, value="mean"):
    return [_points(item.get(key)) for key in (value, "sd", "ci_low", "ci_high")]


def _figure_rows(found):
    """[(name, figure)] of one group's resting.figures at one BSP end, without the gaps."""
    rows = [("all placed: g", found["placed"]), ("the mid at t: g", found["mid"])]
    for model, item in found["models"].items():
        flag = " (all placed)" if item[FILLED_IS_ALL_PLACED] else ""
        rows += [(f"{model} filled{flag}: {what}", item["filled"][what]) for what in MEASURES]
        rows.append((f"{model}: value per placed order", item["value_per_placed"]))
    return rows


def _figure_lines(ends):
    """One group's figures at both BSP ends as two Markdown tables, in points of stake."""
    listed = [_figure_rows(ends[end]) for end in BSP_ENDS]
    columns = [f"{name} ({end})" for end in BSP_ENDS for name in ("mean", "SD", "CI low", "CI high")]
    lines = _table(["figure", "n", "race dates", *columns, "note"],
                   [[name, item["bets"], item["race_dates"], *_figure_cells(item), *_figure_cells(other),
                     item["reason"]] for (name, item), (_, other) in zip(*listed)])
    gaps = [[model, *(part for end in BSP_ENDS for part in _gap_cells(ends[end]["models"][model]["gap"]))]
            for model in ends[AS_WRITTEN]["models"]]
    columns = [f"{name} ({end})" for end in BSP_ENDS for name in ("filled", "unfilled", "gap", "CI low", "CI high",
                                                                   "note")]
    return lines + ["Gap: filled less unfilled, gross g (descriptive, no p)", ""] + _table(["model", *columns], gaps)


def _gap_cells(item):
    return [*item["bets"], _points(item["estimate"]), _points(item["ci_low"]), _points(item["ci_high"]),
            item["reason"]]


def _primary_lines(rule_id, item):
    """T9 for one rule file: the label with everything it is printed with."""
    if item is None:
        return [f"### {rule_id}: no primary cell", "", "No cell passed the selection's gates, so nothing is tested "
                "for this rule file. It still counts in the family.", ""]
    test, kill, null = item["test"], item["kill"], item["null"]
    lines = [f"### {rule_id}: primary cell {item['cell']}, label {item['label']}", "",
             *(f"- {text}" for text in item["printed_with"]),
             f"- blocked: {item['blocked'] or 'no'}; pessimistic fill rate on the test label: "
             f"{item['pessimistic_fill_rate']}",
             "- checks: " + ", ".join(f"{name} {'yes' if held else 'no'}" for name, held in item["checks"].items()),
             ""]
    lines += _table(["test (pessimistic, test label, net_held)", "BSP end", "n", "fills", "race dates", "mean",
                     "CI low", "CI high", "p one-sided", "Holm p", "rejected", "lower bound", FILLED_IS_ALL_PLACED,
                     "note"],
                    [["claim", test["bsp_end"], test["bets"], test["fills"], test["race_dates"],
                      _points(test["mean"]), _points(test["ci_low"]), _points(test["ci_high"]), test["p_one_sided"],
                      test["holm_p_adjusted"], test["holm_rejected"], _points(test["lower_bound"]),
                      test[FILLED_IS_ALL_PLACED], test["reason"]]])
    lines += _table(["kill (optimistic, both labels, net_closed)", "BSP end", "n", "fills", "race dates", "mean",
                     "kill bound", FILLED_IS_ALL_PLACED, "figures given", "note"],
                    [["kill", kill["bsp_end"], kill["bets"], kill["fills"], kill["race_dates"], _points(kill["mean"]),
                      _points(kill["bound"]), kill[FILLED_IS_ALL_PLACED], kill["figures_given"], kill["reason"]]])
    lines += _table(["both nets at both BSP ends", "BSP end", NET_CLOSED, NET_HELD],
                    [[name, end, _points(nets[NET_CLOSED]), _points(nets[NET_HELD])]
                     for name, block in (("claim", test), ("kill", kill)) for end, nets in block["both"].items()])
    lines += _table(["mean net_held at the claim end", "fills", FILLED_IS_ALL_PLACED, "mean"],
                    [[name, part["fills"], part[FILLED_IS_ALL_PLACED], _points(part["mean"])]
                     for group in (item["models"], item["half_years"]) for name, part in group.items()])
    if null is not None:
        lines += _table(["null", "n", "null mean", "null p two-sided", "main over the same orders", "undefined",
                         "blocks", "why"],
                        [["random time", null["null"]["bets"], _points(null["null"]["mean"]),
                          null["null"]["p_two_sided"], _points(null["main_same_orders"]["mean"]), null["undefined"],
                          null["blocks"], null["why"]]])
    return lines + _table(["gap (pessimistic, test label, gross g)", "filled", "unfilled", "gap", "CI low", "CI high",
                           "note"], [["filled less unfilled", *_gap_cells(item["gap"])]])


def _registered_lines(value):
    """The registered pass's tables T8, T9, T5, T7 and T10."""
    labels = value["partitions"]
    lines = ["## T8 Selection (on the explore label's figures and the test label's counts)", "",
             f"- eligible cells: {', '.join(value['selection']['eligible']) or 'none'}",
             *(f"- {rule_id}: {cell or 'no primary'}" for rule_id, cell in value["selection"]["primaries"].items()),
             "", "## T9 Primaries (the only tests, p-values and labels of this pass)", ""]
    for rule_id, item in value["primaries"].items():
        lines += _primary_lines(rule_id, item)
    for title, key in (("T5 Intent-to-treat figures per cell and label", "figures"),
                       ("T7 The same by half-year", "half_years")):
        table = value[key]
        lines += [f"## {title} (points of stake; descriptive, no p)", "",
                  f"Cells in this table: c = {table['cells']}; sqrt(2 ln c) = {table['sqrt_2_ln_c']}.", ""]
        for cell, by_label in table["by_cell"].items():
            for label, group in by_label.items():
                heading = f"### {cell}, {label}: {table['headings'][label]}"
                if key == "figures":
                    classes = value["cells"][cell]["classes"][label]
                    lines += [heading, "", "Classes: " + ", ".join(f"{name} {count}"
                                                                    for name, count in classes.items()) + ".", ""]
                    lines += _figure_lines(group)
                else:
                    for half, ends in group.items():
                        lines += [f"{heading}, {half}", ""] + _figure_lines(ends)
    table = value["sensitivity"]
    lines += ["## T10 Sensitivity (points of stake; descriptive, no p)", "",
              f"Cells in this table: c = {table['cells']}; sqrt(2 ln c) = {table['sqrt_2_ln_c']}. Filled figures "
              "without the fills in the first registered second after t; where too few fall outside it, the filled "
              "figure stands.", ""]
    columns = [f"{name} ({end})" for end in BSP_ENDS for name in ("mean", "SD", "CI low", "CI high")]
    lines += _table(["cell", "label", "heading", "model", "figure", "stands", "n", "race dates", *columns, "note"],
                    [[cell, label, table["headings"][label], model, what, item["filled_figure_stands"],
                      item["ends"][AS_WRITTEN][what]["bets"], item["ends"][AS_WRITTEN][what]["race_dates"],
                      *(part for end in BSP_ENDS for part in _figure_cells(item["ends"][end][what])),
                      item["ends"][AS_WRITTEN][what]["reason"]]
                     for cell, by_label in table["without_first_fills"].items() for label, models in by_label.items()
                     for model, item in models.items() for what in (G, NET_HELD)])
    books = table["bsp_book"]
    keys = next((list(item["quantiles"]) for item in books.values() if item["quantiles"]), [])
    lines += ["The sum of 1 / BSP per market with a scored order (nearest-rank quantiles)", ""]
    lines += _table(["label", "markets", *keys], [[label, item["markets"], *((item["quantiles"] or {}).get(q)
                                                                              for q in keys)]
                                                  for label, item in books.items()])
    return lines + [f"Partition labels: explore {labels['explore_label']}, test {labels['test_label']}.", ""]


def markdown(plan, value):
    """summary.md: the summary's tables as Markdown. Outcomes are printed as points of stake (100 x g)."""
    registered = value["pass"] == REGISTERED
    cells, labels = value["cells"], list(plan.labels)
    lines = [f"# Study {value['study']}: {value['pass']} pass", "",
             f"- registration sha256: `{value['registration_sha256']}`",
             f"- split file sha256: `{value['partitions_sha256']}`", f"- input sha256: `{value['input_sha256']}`",
             f"- index records: {value['records']}; markets replayed: {value['replayed_markets']}",
             f"- partition labels: explore {labels[0]}, test {labels[1]}",
             "- unit: g is a fraction of the backer's stake; these tables print 100 x g as points of stake", ""]
    if not registered:
        lines += [f"Withheld in this pass: {value['withheld']}.", ""]
    lines += [f"A group with fewer than {plan.min_values} values gives its counts only. A cell is one rule file at "
              "one of its times, named rule@seconds before the off.", ""]
    coverage = value["coverage"]
    lines += ["## B1 Coverage by month (records of the registered tier and orders placed, by partition label)", ""]
    lines += _table(["month", *(f"records {label}" for label in labels), "records, no registered label",
                     *(f"orders {label}" for label in labels)],
                    [[month, *(row["records"][label] for label in labels), row["records"][NO_LABEL],
                      *(row["orders"][label] for label in labels)] for month, row in coverage["by_month"].items()])
    lines += [f"Records with no anchor: {coverage['records_without_an_anchor']}. Records with an anchor and no "
              f"registered label: {coverage['records_without_a_registered_label']}.", ""]
    lines += ["## B2 Codes per cell (each column adds up to the record count)", ""]
    lines += _table(["code", *cells], [[code, *(tables["codes"][code] for tables in cells.values())]
                                       for code in plan.codes + (ISSUED,)
                                       if any(tables["codes"][code] for tables in cells.values())])
    lines += ["## B3 Classes of placed orders per cell and label", ""]
    lines += _table(["cell", "label", *CLASSES], [[cell, label, *classes.values()] for cell, tables in cells.items()
                                                  for label, classes in tables["classes"].items()])
    lines += ["## B4 Fills of scored orders per cell, label and model (counts)", ""]
    within = [f"within {seconds} s of t" for seconds in plan.within_s]
    lines += _table(["cell", "label", "model", "scored", "fills", "fill rate", *FILL_KINDS, *within,
                     *(f"after the hour: {kind}" for kind in FILL_KINDS), "left the market not OPEN",
                     *(f"ended by {end}" for end in ENDS)],
                    [[cell, label, model, item["scored"], item["fills"], item["fill_rate"], *item["by_kind"].values(),
                      *item["within_s_of_t"].values(), *item["after_the_hour"].values(),
                      item["left_the_market_not_open"], *item["ended_by"].values()]
                     for cell, tables in cells.items() for label, models in tables["fills"].items()
                     for model, item in models.items()])
    lines += ["Fills of the orders that are not scored, by class (a class with none is left out here, not in "
              "summary.json)", ""]
    lines += _table(["cell", "label", "class", *plan.models],
                    [[cell, label, klass, *counts.values()] for cell, tables in cells.items()
                     for label, classes in tables["fills_not_scored"].items() for klass, counts in classes.items()
                     if any(counts.values())])
    lines += ["## B5 Spread of gross g at the BSP as written, both labels pooled (points of stake)", "",
              "SD and nearest-rank ranges q(1 - a) - q(a) over each model's filled part; where fewer than "
              f"{plan.min_values} scored orders are unfilled, every scored order stands in ({FILLED_IS_ALL_PLACED}).",
              ""]
    filled = [(cell, model, item) for cell, tables in cells.items()
              for model, item in tables["spread_of_g"]["filled"].items()]
    ranges = next((list(item["ranges"]) for _, _, item in filled if item["ranges"]), [])
    rows = []
    for cell, tables in cells.items():
        everything = tables["spread_of_g"]["all_scored"]
        rows.append([cell, "all scored orders", everything["values"], everything["race_dates"],
                     _points(everything["sd"]), *[None] * len(ranges), None])
        rows += [[cell, f"{model} filled", item["values"], item["race_dates"], _points(item["sd"]),
                  *(_points((item["ranges"] or {}).get(name)) for name in ranges), item[FILLED_IS_ALL_PLACED]]
                 for model, item in tables["spread_of_g"]["filled"].items()]
    lines += _table(["cell", "group", "values", "race dates", "SD", *ranges, FILLED_IS_ALL_PLACED], rows)
    queues = [(cell, tables["queue_at_t"]) for cell, tables in cells.items()]
    keys = next((list(item["queue_quantiles"]) for _, item in queues if item["queue_quantiles"]), [])
    lines += ["The size shown at t ahead of the order, at its price on its own side (GBP of backer's stake; the "
              f"{plan.models[-1]} model's queue; nearest-rank quantiles over every placed order)", ""]
    lines += _table(["cell", "orders", *keys], [[cell, item["orders"], *((item["queue_quantiles"] or {}).get(q)
                                                                         for q in keys)] for cell, item in queues])
    seconds = [(cell, model, item) for cell, tables in cells.items()
               for model, item in tables["seconds_to_fill"].items()]
    keys = next((list(item["seconds_quantiles"]) for _, _, item in seconds if item["seconds_quantiles"]), [])
    lines += ["Seconds from t to each fill of a scored order (nearest-rank quantiles)", ""]
    lines += _table(["cell", "model", "fills", *keys],
                    [[cell, model, item["fills"], *((item["seconds_quantiles"] or {}).get(q) for q in keys)]
                     for cell, model, item in seconds])
    lines += [f"## B6 Detectable effect on the test label (points of stake; {INDEPENDENCE})", ""]
    lines += _table(["cell", "model", "values", "race dates", FILLED_IS_ALL_PLACED, "detectable effect"],
                    [[cell, model, item["detectable"]["values"], item["detectable"]["race_dates"],
                      item["detectable"][FILLED_IS_ALL_PLACED], _points(item["detectable"]["effect"])]
                     for cell, model, item in filled])
    precision = value["bsp_precision"]
    lines += ["## B7 Diagnostics (counts)", "",
              f"- scored orders' BSPs that are whole hundredths: {precision['scored_bsps_whole_hundredths']}; that "
              f"are not: {precision['scored_bsps_not_whole_hundredths']}; the two BSP ends decide apart: "
              f"{'yes' if precision['bsp_ends_decide_apart'] else 'no'}", ""]
    flags = sorted({name for tables in cells.values() for name in tables["flags_missing_at_t"]})
    lines += _table(["cell", *TRD_COUNTS, "null values", *(f"no null: {reason}" for reason in NULL_UNDEFINED),
                     *(f"no value at t: {name}" for name in flags)],
                    [[cell, *tables["trd_changes"].values(), tables["null_values"]["defined"],
                      *tables["null_values"]["undefined"].values(),
                      *(tables["flags_missing_at_t"].get(name) for name in flags)]
                     for cell, tables in cells.items()])
    if registered:
        lines += _registered_lines(value)
    return "\n".join(lines).rstrip("\n") + "\n"


# ---------------------------------------------------------------------------------------------------------------
# The run

def _counts(plan, tally):
    return {"records": tally.records, "replayed_markets": tally.replayed,
            "issued": {_key(cell): tally.codes[cell][ISSUED] for cell in plan.cells},
            "scored": {_key(cell): len(scored(_both(plan, tally, cell))) for cell in plan.cells}}


def _tally(plan, root, archives, limits):
    """Decide every record of every archive (module docstring)."""
    tally, seen = Tally(plan), set()
    for archive in archives:
        sha = archive["archive_sha256"]
        for record in _records(archive):
            months = _record_months(record, sha)
            if months is None:
                raise RunRefused(f"input:{sha}:malformed_record")
            if any(month_gate(month) for month in months):
                raise RunRefused(f"input:{sha}:stale_gate")
            # Registration data.duplicates: a market of the registered tier is keyed by its id alone.
            tier, market_id = record["provenance"]["plan"], record["market_id"]
            key = market_id if tier in plan.tiers else (tier, market_id)
            if key in seen:
                raise RunRefused(f"input:{sha}:duplicate_market")
            seen.add(key)
            try:
                facts = record_facts(plan, record)
            except RECORD_ERRORS:
                raise RunRefused(f"input:{sha}:malformed_record") from None
            decide_record(plan, record, facts, root, archive["manifest"], tally, limits)
    return tally


def run_study(study, pass_name, registration_sha256_hex, archive_root, out, code_commit, ledger, ledger_head,
              blind_receipt=None, registration_path=None, limits=DEFAULT_LIMITS, clock=utc_now_text):
    """One pass (module docstring); a JSON-ready result: completed with counts and hashes, refused with problems,
    or failed with a failure code."""
    started_at = clock()
    try:
        if study not in STUDIES:
            _refuse("study:not_supported")
        if pass_name not in PASSES:
            _refuse("pass:not_supported")
        plan, split_raw, alpha = _plan(study, registration_path, registration_sha256_hex)
        if not (isinstance(code_commit, str) and COMMIT_RE.fullmatch(code_commit)):
            _refuse("code_commit:malformed")
        guarded = _guarded(archive_root, "archive_root")
        root, problems = check_data_root(guarded)
        if problems:
            _refuse(problems[0].replace("data_root:", "archive_root:", 1))
        target = _out_path(out, root)
        if pass_name == BLIND and blind_receipt is not None:
            _refuse("blind_receipt:not_for_the_blind_pass")
        blind = _blind_receipt(blind_receipt, plan) if pass_name == REGISTERED else None
        try:
            _, sources_sha256 = source_descriptor()
        except CertificateProblem as error:
            raise StudyRefused(str(error)) from None
        if blind is not None and (blind[0].get("code_commit"), blind[0].get("source_descriptor_sha256")) \
                != (code_commit, sources_sha256):
            _refuse("blind_receipt:another_code")
        try:
            archives, descriptor, input_sha256 = _inputs(root)
        except RunRefused as error:
            raise StudyRefused(str(error)) from None
        for archive in archives:                    # ingested under the read limits the replay uses
            if archive["manifest"].get("limits") != asdict(limits):
                _refuse(f"input:{archive['archive_sha256']}:limits_not_the_runs")
        if blind is not None and blind[0].get("input_sha256") != input_sha256:
            _refuse("blind_receipt:another_input")
        request = PassRequest(
            study=plan.study, lineage=plan.lineage, kind=LOOK, partitions=plan.labels,
            partitions_sha256=plan.split.sha256, registration_sha256=plan.sha256, input_sha256=input_sha256,
            source_descriptor_sha256=sources_sha256, code_commit=code_commit)
        if pass_name == REGISTERED:
            request = replace(request, kind=REGISTERED_KIND, m=plan.family_m, alpha=alpha)
        try:
            handle, repeat = _ledger(ledger, ledger_head, root, target, request, split_raw, blind and blind[2])
        except LedgerError as error:
            raise StudyRefused(str(error)) from None
    except StudyRefused as error:
        return {"status": "refused", "problems": [str(error)]}
    try:
        tally = _tally(plan, root, archives, limits)
    except (RunRefused, ReplayProblem, StudyRefused) as error:
        return {"status": "failed", "failure": _failure(error)}
    value = summary(plan, tally, pass_name, input_sha256)
    summary_bytes = canonical_json(value) + b"\n"
    markdown_bytes = markdown(plan, value).encode("utf-8")
    counts = _counts(plan, tally)
    outputs = {SUMMARY_JSON: {"sha256": hashlib.sha256(summary_bytes).hexdigest(), "bytes": len(summary_bytes)},
               SUMMARY_MD: {"sha256": hashlib.sha256(markdown_bytes).hexdigest(), "bytes": len(markdown_bytes)}}
    try:                                            # the one ledger entry, before any file or stdout write
        head = head_text(handle.record(request, split_raw, outputs[SUMMARY_JSON]["sha256"], COMPLETED, clock))
    except UnlockError as error:                    # the entry was written; the lock file is left
        return {"status": "failed", "failure": str(error), "ledger_head": head_text(error.head)}
    except LedgerError as error:
        return {"status": "failed", "failure": str(error)}
    try:
        passes = handle.counts(plan.lineage)
        receipt = {"kind": RECEIPT_KIND, "schema_version": SCHEMA_VERSION, "study": plan.study, "pass": pass_name,
                   "status": "completed", "registration_sha256": plan.sha256,
                   "partitions_sha256": plan.split.sha256, "input_sha256": input_sha256, "input": descriptor,
                   "code_commit": code_commit, "source_descriptor_sha256": sources_sha256,
                   "interpreter": "{0} {1}.{2}.{3}".format(sys.implementation.name, *sys.version_info[:3]),
                   "blind_receipt_sha256": None if blind is None else blind[1], "ledger_head": head,
                   "ledger_repeat": repeat, "ledger_counts": passes, "bytes_walked": tally.bytes_walked,
                   "bytes_drained": tally.bytes_drained, "counts": counts, "outputs": outputs,
                   "started_at": started_at, "recorded_at": clock()}
        receipt_bytes = canonical_json(receipt) + b"\n"
        target = _out_path(out, root)
        _write(target, [(SUMMARY_JSON, summary_bytes), (SUMMARY_MD, markdown_bytes), (RECEIPT_JSON, receipt_bytes)])
    except (LedgerError, StudyRefused) as error:    # the entry stands: the re-run is a repeat from this head
        return {"status": "failed", "failure": str(error), "ledger_head": head}
    return {"status": "completed", "study": plan.study, "pass": pass_name, "registration_sha256": plan.sha256,
            "input_sha256": input_sha256, "summary_sha256": outputs[SUMMARY_JSON]["sha256"],
            "receipt_sha256": hashlib.sha256(receipt_bytes).hexdigest(), "ledger_head": head,
            "ledger_repeat": repeat, "ledger_counts": passes, **counts}


def build_parser():
    parser = argparse.ArgumentParser(prog="python3 -m betangel_research.betfair.backtest.resting_study",
                                     description="Registered measurement close-v1 on local historical files; an "
                                                 "order here is simulated and nothing can reach an exchange.")
    parser.add_argument("--study", required=True, help="the study to run: close-v1")
    parser.add_argument("--pass", dest="pass_name", required=True, choices=PASSES, help="blind first, then registered")
    parser.add_argument("--registration-sha256", required=True, help="sha256 of the registration file's raw bytes")
    parser.add_argument("--archive-root", required=True, help="data root with manifests/, raw/ and index/")
    parser.add_argument("--out", required=True, help="a new directory outside every Git worktree and data tree")
    parser.add_argument("--code-commit", required=True, help="the 40-character commit the code was checked out at")
    parser.add_argument("--ledger", required=True, help="the study ledger's directory; it must exist")
    parser.add_argument("--ledger-head", required=True, help="the ledger's last head: none, or SEQ:SHA256")
    parser.add_argument("--blind-receipt", help="registered pass: the blind pass's receipt.json")
    parser.add_argument("--registration", help="registration file (default: research/studies/<study>.json); the "
                                               "split file and rules/ are read beside its studies/ folder")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        result = run_study(args.study, args.pass_name, args.registration_sha256, args.archive_root, args.out,
                           args.code_commit, args.ledger, args.ledger_head, blind_receipt=args.blind_receipt,
                           registration_path=args.registration)
    except Exception as error:      # only the class name: a traceback or a message can print a key, and so an id
        result = {"status": "failed", "failure": f"internal:{type(error).__name__}"}
    sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
    return 0 if result["status"] == "completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
