"""python3 -m betangel_research.betfair.backtest.early_study: run registered measurement early-price-v1.

--study early-price-v1 --pass blind|registered --registration-sha256 HEX --archive-root DIR --out DIR
--code-commit HEX --ledger DIR --ledger-head none|SEQ:SHA256 [--blind-receipt PATH] [--registration PATH] runs
research/studies/early-price-v1.json (or --registration) over every archive ingested and indexed under the data root
DIR and writes summary.json, summary.md (totals only) and receipt.json into the new directory --out, through
"<out>.partial" (mode 0700, files 0600) renamed only when all three are whole. It prints one JSON line of counts,
codes and hashes and exits 0 (completed) or 2 (refused or failed: --out is not created then). A fault in the code
itself prints the failure internal:<exception class name> and nothing else of the error (main): a traceback or a
message can hold a key, and a key can be an id. Nothing here is ordered and nothing can reach an exchange. It is a
module of its own, not a subcommand of the backtest CLI, because __main__.py is one of the certified engine's
sources and stays byte-identical; this module and early.py are in the certificate's source descriptor, so adding
them changed the certificate key again.

Refusals, before any record is read, in this order (resting_study's): --study is not early-price-v1 (nothing is read
then); the pass name; the registration file's raw bytes must hash to --registration-sha256, the file must lie in a
folder named studies, and the split file beside that folder, a regular file of at most 1 MiB named by the
registration, must be the bytes it names (early.parse_registration); --code-commit, the data root and --out as
movement_study checks them; the blind pass takes no --blind-receipt and the registered pass needs the blind pass's
canonical, completed receipt (kind betangel_early_receipt) of this study, registration, split file, --code-commit,
backtest-source sha256 and input sha256; every manifest's read limits must be the replay's. Then the study ledger
(study_ledger.py, through resting_study._ledger): --out apart from the ledger directory under any spelling of it;
the ledger opened with the head the operator typed, and for a registered pass with the entry the blind receipt names
as its own as the anchor; then the budgets. A pass whose identity the ledger already holds is a repeat.

The read is resting_study's: every record's months pass the current month gate (sealed months are never read), a
market id seen twice in the BASIC tier fails the run (duplicate_market), and the member's path is guarded and its
bytes hashed against the manifest. What the index record alone says is read first (its month, its race date, label
and half-year, and each time's code: early.record_code); only those calls report malformed_record. A record with a
code at every time is never opened: so no file of 2025, of 2015-16 or of another plan is opened. Any other record
is opened once and folded to the latest time that reached the view, at most 10 minutes before the off
(early.assess); the rest of the file is hashed, not parsed. The tally keeps counts, the ages of prices, the sum of
1 / BSP per market with a scored value, and the priced value rows. A row holds no market or selection id, name or
time of a market, stays in memory and never reaches disk.

After the read the summary's bytes are built in memory; then the ledger gets its one entry (a look for a blind
pass, the registered entry for a registered pass) before any file or stdout write; then the outputs are written. A
repeat appends nothing, and its summary must hash to the stored tables hash (ledger:repeat_differs otherwise). A
pass that fails or is stopped during the read has appended and written nothing. If an output cannot be written
after the append, or the code faults there, the entry stands, the result names the new head (ledger_head) beside
the failure, and the re-run is a repeat. If only the ledger's lock file could not be removed, the outputs are still
written and the result says ledger_lock_left. The summary has no clock, so the same registration, input and code
give the same bytes. The receipt has resting_study's keys.

Tables (the registration's blind_pass.releases and registered_pass.releases). Both passes: B1 records by registered
month and label, records with no off time and with no registered label, files opened; B2 the market codes per time
(each time adds up to the record count); B3 per time and label the markets viewed, the markets with a priced runner,
the runner codes (no_ltp by kind) and the priced runners per band; B4 the classes of priced runners per time, band
and label; B5 the age of the price in seconds, as quantiles, per time and band, over the runners coded
ltp_before_last_removal, ltp_stale or priced; B6 per band, start and label the undefined moves by reason, the
members flagged suspension_in_window and per move cell its members, scored values, race dates and members by k;
B7 the SD and symmetric ranges of x at the BSP as written over each move cell's scored values, both labels pooled;
B8 the detectable effect on the test label from a clustered SE over both years (early.blind_spreads); B9 the
counts of whole-hundredth BSPs and the switch, the times with no bspMarket value, and the bytes the replay read and
drained. In the blind pass the two moved cells of a start and band are named moved_larger and moved_smaller
(early.blind_names), and the blind summary holds no location of x: no average, middle value, raw quantile, sign,
interval, test, label, selection or half-year figure, no spread by label or of a level cell
(tests/test_backtest_early.py walks every key and the Markdown). The registered pass names the cells and adds: R1
the level cells per label at both BSP ends (early.figures); R2 the move cells on the explore label, and on the test
label counts only; R3 the selection (early.select); R4 per side the label and everything printed with it
(early.verdicts, early.shown); R5 the primaries on the test label by half-year, mean above 0 yes or no; R6 the
quantiles of the sum of 1 / BSP per market with a scored value, per label. The selection is decided first, then the
labels on the full values, then part and rest (early.given_move_cells, early.kill_given): a move cell's explore
figures, and its standing in R3 (its SE, mean / SE and detectable effect), are given only when given_move_cells
gives it; a primary that the test label does not give keeps its label, p and Holm result and loses its mean.
Markdown tables print 100 x x as points of stake; summary.json keeps fractions of stake, floats rounded to 6 places.

Standard library only; it imports the backtest modules named above and nothing outside the package but what they
import (tests/test_betfair_backtest_boundaries.py). It never logs in, reads an account or sends anything.
"""

import argparse
import bz2
from collections import Counter
from dataclasses import asdict, replace
import hashlib
import json
import sys

from .archive import DEFAULT_LIMITS, REPOSITORY_ROOT, ArchiveProblem, HashingReader, check_data_root, month_gate, \
    safe_target, utc_month
from .certificate import CertificateProblem, source_descriptor
from .contract import canonical_json
from .early import (BEFORE_REMOVAL, CHANGES_BETWEEN, MOVED_LARGER, MOVED_SMALLER, MOVES, NO_LTP, NO_LTP_KINDS,
                    PRICED, RUNNER_CODES, STALE, STUDY_ID, UNDEFINED, VIEWED, Assessed, LevelCell, assess,
                    blind_names, blind_spreads,
                    bsp_switch, by_cell, changes_between, figures, given_move_cells, kill_given, parse_registration,
                    race_label, record_code, select, shown, split_named, test_counts, verdicts)
from .lp import bsp_book
from .market_index import loads_strict
from .movement import MAX_REGISTRATION_BYTES, RegistrationError, registration_sha256
from .movement_study import (COMMIT_RE, MAX_RECEIPT_BYTES, SHA256_RE, StudyRefused, _cell_text, _failure, _guarded,
                             _out_path, _read_regular, _refuse, _table, _write)
from .partition import MAX_SPLIT_BYTES
from .replay import ReplayProblem, _member
from .resting import AS_WRITTEN, BSP_ENDS, G, NET_HELD, SCORED, quantile_table, rounded, scored
from .resting_study import (BLIND, EXPLORATORY, NO_ANCHOR, NO_LABEL, NOT_TESTED, OUTSIDE, PASSES, RECEIPT_JSON,
                            RECORD_ERRORS, REGISTERED, STUDIES_FOLDER, SUMMARY_JSON, SUMMARY_MD, _exploratory,
                            _ledger)
from .runner import RunRefused, _inputs, _record_months, _records, utc_now_text
from .study_ledger import COMPLETED, LOOK, PassRequest, head_text, parse_head
from .study_ledger import REGISTERED as REGISTERED_KIND
from .trial_ledger import LedgerError, UnlockError

SUMMARY_KIND, RECEIPT_KIND, SCHEMA_VERSION = "betangel_early_summary", "betangel_early_receipt", 1
STUDIES = (STUDY_ID,)                               # the studies this module runs
REGISTRATIONS = REPOSITORY_ROOT / "research" / "studies"
WITHHELD = ("every location statistic of x (an average, a middle value, a raw quantile or a sign), every interval, "
            "test, label and selection, any figure by half-year, any SD of a net, the names of the two moved cells, "
            "any SD, range or SE by label, every spread of a level cell, and any spread over a union of move cells "
            "or over the values with an undefined move: the registration's blind_pass.withholds")
CLUSTERED = "from a clustered SE over both years"
NOT_COMPARABLE = "not comparable across times: each time needs its own fresh price, so the cells hold other runners"
COUNTS_ONLY_TEST = "counts only: of 2024's move cells only the primaries print a mean, in R4"
# The runner's own tallies of runner codes: no_ltp by kind, then the other runner codes, then priced.
RUNNER_TALLY = NO_LTP_KINDS + RUNNER_CODES[1:] + (PRICED,)
AGED = (BEFORE_REMOVAL, STALE, PRICED)              # the runner codes whose price has a band and a change (B5)
SHOWN_ORDER = MOVES + (MOVED_LARGER, MOVED_SMALLER)


def _plan(study, path, expected):
    """(Plan, the split file's raw bytes, the registered alpha as its decimal text) of the registration and the
    split file it names, after the checks in the module docstring; StudyRefused otherwise."""
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
        split_name, _ = split_named(raw)
        split_raw = _read_regular(folder.parent / split_name, "split", MAX_SPLIT_BYTES)
        plan = parse_registration(raw, split_raw)
    except RegistrationError as error:
        raise StudyRefused(str(error)) from None
    return plan, split_raw, loads_strict(raw)["family"]["alpha"]


def _blind_receipt(path, plan):
    """(receipt, sha256 of its bytes, its own ledger entry as (seq, sha256)) of a completed blind-pass receipt of
    this study, registration and split file; StudyRefused otherwise."""
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
        anchor = parse_head(receipt.get("ledger_entry"))
    except LedgerError:
        anchor = None
    if anchor is None:
        _refuse("blind_receipt:ledger_entry_malformed")
    return receipt, hashlib.sha256(data).hexdigest(), anchor


class Tally:
    """Everything the summary needs: counts, the ages of prices, the sum of 1 / BSP per market with a scored
    value, and rows, the priced Value rows of every time, class and label. No market or selection id enters it."""

    def __init__(self, plan):
        self.records = self.replayed = self.bytes_walked = self.bytes_drained = 0
        self.months = {month: dict.fromkeys(plan.labels + (NO_LABEL,), 0)
                       for month in plan.months + (OUTSIDE, NO_ANCHOR)}
        self.codes = {at_s: dict.fromkeys(plan.codes + (VIEWED,), 0) for at_s in plan.times_s}
        self.bsp_unknown = dict.fromkeys(plan.times_s, 0)
        self.viewed = {at_s: dict.fromkeys(plan.labels, 0) for at_s in plan.times_s}
        self.with_priced = {at_s: dict.fromkeys(plan.labels, 0) for at_s in plan.times_s}
        self.runners = {at_s: {label: dict.fromkeys(RUNNER_TALLY, 0) for label in plan.labels}
                        for at_s in plan.times_s}
        self.ages = {(at_s, band): [] for at_s in plan.times_s for band, _, _ in plan.bands}
        self.rows = []
        self.books = {label: [] for label in plan.labels}


def _walk(root, manifest, record, plan, limits):
    """(early.assess's answer over the record's primary member, the compressed bytes the fold consumed, the bytes
    drained after it), after replay_view's checks of the member; the member's bytes are hashed against the
    manifest (resting_study._walk's checks)."""
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
                assessed = assess(stream, record, plan, limits)
            consumed = reader.size
            digest = reader.drain()
    except OSError:
        raise ReplayProblem(f"replay:{market_id}:member_unreadable_file") from None
    if digest != (member["sha256"], member["size_bytes"]):
        raise ReplayProblem(f"replay:{market_id}:member_hash_mismatch")
    return assessed, consumed, digest[1] - consumed


def record_facts(plan, record):
    """What the index record alone says: (its coverage row, or None for another plan; early.race_label's answer;
    {time: early.record_code's answer}). These are the only calls whose errors are reported as a malformed
    record."""
    month = None
    if record["provenance"]["plan"] in plan.tiers:
        first = record["decision_time"]["first_published_market_time"]
        month = None if first is None else utc_month(first["value"])
        if month is None:
            month = NO_ANCHOR
        elif not plan.start_month <= month <= plan.end_month:
            month = OUTSIDE
    return month, race_label(plan, record), {at_s: record_code(plan, record, at_s) for at_s in plan.times_s}


def decide_record(plan, record, facts, root, manifest, tally, limits=DEFAULT_LIMITS):
    """Add one index record to the tally (module docstring). A record with a code at every time is not opened."""
    month, dated, coded = facts
    tally.records += 1
    label = None if dated is None else dated[1]
    if month is not None:
        tally.months[month][label if label in plan.labels else NO_LABEL] += 1
    if any(code is None for code, _ in coded.values()):
        assessed, consumed, drained = _walk(root, manifest, record, plan, limits)
        tally.replayed += 1
        tally.bytes_walked += consumed
        tally.bytes_drained += drained
    else:
        assessed = Assessed(codes={at_s: code for at_s, (code, _) in coded.items()},
                            bsp_unknown={at_s: unknown for at_s, (_, unknown) in coded.items()}, values=(),
                            replayed=False)
    for at_s in plan.times_s:
        tally.codes[at_s][assessed.codes[at_s]] += 1
        tally.bsp_unknown[at_s] += int(assessed.bsp_unknown[at_s])
        if assessed.codes[at_s] == VIEWED:
            tally.viewed[at_s][label] += 1
    priced, valued = set(), False
    for row in assessed.values:
        tally.runners[row.at_s][row.label][row.no_ltp if row.code == NO_LTP else row.code] += 1
        if row.code in AGED:
            tally.ages[(row.at_s, row.band)].append(row.age_ms / 1000)
        if row.code == PRICED:
            tally.rows.append(row)
            priced.add(row.at_s)
            valued = valued or row.klass == SCORED
    for at_s in priced:
        tally.with_priced[at_s][label] += 1
    book = bsp_book(record) if valued else None
    if book is not None:
        tally.books[label].append(float(book))


# ---------------------------------------------------------------------------------------------------------------
# Tables

def _level_key(cell):
    """The text a table names a level cell by: its band and its time."""
    return f"{cell.band}@{cell.at_s}"


def _group_key(band, start_s):
    """The text a table names one band's move cells from one start by."""
    return f"{band} from {start_s}"


def _move_key(cell, name):
    """The text a table names a move cell by, under the name the pass shows it by."""
    return f"{_group_key(cell.band, cell.start_s)}: {name}"


def _dates(rows):
    return len({row.race_date for row in rows})


def _of(rows, label):
    return [row for row in rows if row.label == label]


def _move(row, start_s):
    return next(move for move in row.moves if move.start_s == start_s)


def _counts_tables(plan, tally):
    """B2 to B5 (module docstring): counts, and the ages of prices."""
    labels, bands = plan.labels, [band for band, _, _ in plan.bands]
    found = Counter((row.at_s, row.band, row.label, row.klass) for row in tally.rows)
    runners = {}
    for at_s in plan.times_s:
        runners[str(at_s)] = {}
        for label in labels:
            counted = tally.runners[at_s][label]
            runners[str(at_s)][label] = {
                "markets_viewed": tally.viewed[at_s][label],
                "markets_with_a_priced_runner": tally.with_priced[at_s][label],
                "no_ltp": {kind: counted[kind] for kind in NO_LTP_KINDS},
                **{code: counted[code] for code in RUNNER_CODES[1:] + (PRICED,)},
                "priced_by_band": {band: sum(found[(at_s, band, label, klass)] for klass in plan.classes)
                                   for band in bands}}
    classes = {str(at_s): {band: {label: {klass: found[(at_s, band, label, klass)] for klass in plan.classes}
                                  for label in labels} for band in bands} for at_s in plan.times_s}
    ages = {str(at_s): {band: {"runners": len(tally.ages[(at_s, band)]),
                               "seconds_quantiles": quantile_table(plan, tally.ages[(at_s, band)])}
                        for band, _, _ in plan.bands} for at_s in plan.times_s}
    return {"codes": {str(at_s): dict(tally.codes[at_s]) for at_s in plan.times_s}, "runners": runners,
            "classes": classes, "ages": ages}


def _listed(plan, names):
    """The move cells in the order the tables list them: band by band, start by start, and in a start DRIFT, FLAT,
    STEAM when named, or FLAT, moved_larger, moved_smaller in the blind pass, so a row's place shows no name."""
    return [cell for band, _, _ in plan.bands for start in plan.starts_s
            for cell in sorted((cell for cell in plan.move_cells if (cell.band, cell.start_s) == (band, start)),
                               key=lambda cell: SHOWN_ORDER.index(names[cell]))]


def _move_tables(plan, cells, names):
    """B6 to B8 (module docstring), each move cell under the name the pass shows it by. names: {MoveCell: name}."""
    groups, listed = {}, _listed(plan, names)
    for band, _, _ in plan.bands:
        entry = cells[LevelCell(plan.entry_s, band)]
        for start in plan.starts_s:
            mine = [cell for cell in listed if (cell.band, cell.start_s) == (band, start)]
            by_label = {}
            for label in plan.labels:
                members = _of(entry, label)
                found = [_move(row, start) for row in members]
                by_label[label] = {
                    "members": len(members),
                    "undefined": {reason: sum(1 for move in found if move.reason == reason) for reason in UNDEFINED},
                    "flagged": sum(1 for move in found if move.flagged),
                    "cells": {names[cell]: _cell_counts(_of(cells[cell], label), start) for cell in mine}}
            groups[_group_key(band, start)] = by_label
    spreads = blind_spreads(plan, cells)
    return {"move_groups": groups,
            "spreads": {"bsp_end": AS_WRITTEN, "detectable_from": CLUSTERED,
                        "by_cell": {_move_key(cell, names[cell]): spreads[cell] for cell in listed}}}


def _cell_counts(rows, start_s):
    """B6 for one move cell on one label: members, scored values, their race dates, members by k."""
    held = scored(rows)
    ks = [changes_between(_move(row, start_s).k) for row in rows]
    return {"members": len(rows), "scored": len(held), "race_dates": _dates(held),
            "k": {name: ks.count(name) for name in CHANGES_BETWEEN}}


def _counts_only(rows):
    """R2 on the test label: counts only."""
    held = scored(rows)
    return {"members": len(rows), "scored": len(held), "race_dates": _dates(held)}


def _verdict_json(verdict):
    """A shown verdict with its cell as table text."""
    cell = verdict["cell"]
    return dict(verdict, cell=None if cell is None else _move_key(cell, cell.move),
                printed_with=list(verdict["printed_with"]))


def _registered(plan, cells, switch, books):
    """R1 to R6 of the registered pass (module docstring). The selection is given the explore label's rows, two
    counts per move cell from the test label and the switch, nothing else; then the labels are decided on the full
    values; then part and rest."""
    explore, test = plan.labels
    named = {cell: _move_key(cell, cell.move) for cell in plan.move_cells}
    selection = select(plan, {cell: _of(cells[cell], explore) for cell in plan.move_cells},
                       test_counts(plan, cells), switch.apart)
    found = verdicts(plan, selection.primaries, {cell: cells[cell] for cell in plan.move_cells}, switch.apart)
    given = {label: given_move_cells(plan, cells, label, selection.primaries) for label in plan.labels}
    standing = {}
    for cell in plan.move_cells:
        item = selection.cells[cell]
        if cell not in given[explore]:      # its SE with its mean / SE would give the mean R2 withholds
            item = dict(item, se=None, detectable=None, mean_over_se=dict.fromkeys(item["mean_over_se"]))
        standing[named[cell]] = dict(item, figures_given=cell in given[explore])
    primaries = {}
    for side, verdict in found.items():
        cell = verdict["cell"]
        kill = cell is not None and kill_given(plan, cells[cell], {label: cell in given[label]
                                                                    for label in plan.labels})
        primaries[side] = _verdict_json(shown(verdict, cell is not None and cell in given[test], kill))
    return {
        "level_figures": dict(_exploratory(len(plan.level_cells)), headings={explore: EXPLORATORY, test: NOT_TESTED},
                              not_comparable=NOT_COMPARABLE, removals=plan.removal_note, by_cell={
                                  _level_key(cell): {label: figures(plan, _of(cells[cell], label))
                                                     for label in plan.labels} for cell in plan.level_cells}),
        "move_figures": dict(_exploratory(len(plan.move_cells)), headings={explore: EXPLORATORY,
                                                                           test: COUNTS_ONLY_TEST}, by_cell={
            named[cell]: {explore: figures(plan, _of(cells[cell], explore), given=cell in given[explore]),
                          test: _counts_only(_of(cells[cell], test))} for cell in plan.move_cells}),
        "selection": {"eligible": [named[cell] for cell in selection.eligible],
                      "primaries": {side: cell and named[cell] for side, cell in selection.primaries.items()},
                      "cells": standing},
        "primaries": primaries,
        "bsp_book": {label: {"markets": len(values), "quantiles": quantile_table(plan, values)}
                     for label, values in books.items()},
    }


def summary(plan, tally, pass_name, input_sha256):
    """The summary of one pass (module docstring), every float rounded; the blind pass's holds no location of x."""
    cells = by_cell(plan, tally.rows)
    switch = bsp_switch(plan, cells)
    names = blind_names(plan, cells) if pass_name == BLIND else {cell: cell.move for cell in plan.move_cells}
    months = tally.months
    value = {"kind": SUMMARY_KIND, "schema_version": SCHEMA_VERSION, "study": plan.study, "pass": pass_name,
             "registration_sha256": plan.sha256, "partitions_sha256": plan.split.sha256,
             "input_sha256": input_sha256,
             "partitions": {"explore_label": plan.explore_label, "test_label": plan.test_label},
             "records": tally.records, "replayed_markets": tally.replayed,
             "coverage": {"by_month": {month: dict(row) for month, row in months.items()},
                          "records_without_an_anchor": sum(months[NO_ANCHOR].values()),
                          "records_without_a_registered_label": sum(row[NO_LABEL] for month, row in months.items()
                                                                    if month != NO_ANCHOR)},
             **_counts_tables(plan, tally), **_move_tables(plan, cells, names),
             "bsp_precision": {"scored_bsps_whole_hundredths": switch.whole,
                               "scored_bsps_not_whole_hundredths": switch.of - switch.whole,
                               "bsp_ends_decide_apart": switch.apart,
                               "bsp_market_with_no_value": {str(at_s): tally.bsp_unknown[at_s]
                                                            for at_s in plan.times_s},
                               "bytes_walked": tally.bytes_walked, "bytes_drained": tally.bytes_drained}}
    if pass_name == REGISTERED:
        value.update(_registered(plan, cells, switch, tally.books))
    else:
        value["withheld"] = WITHHELD
    return rounded(value)


# ---------------------------------------------------------------------------------------------------------------
# Markdown

def _points(value):
    """A fraction of stake as points of stake (100 x), to 4 places."""
    return None if value is None else round(100 * value, 4)


def _figure(item):
    return [_points(item.get(key)) for key in ("mean", "sd", "ci_low", "ci_high")]


def _figure_rows(rows):
    """[(cell, label, heading, figures)] as table rows at both BSP ends."""
    return [[cell, label, heading, found["members"], found["x"][AS_WRITTEN]["values"],
             found["x"][AS_WRITTEN]["race_dates"], *(part for end in BSP_ENDS for part in _figure(found["x"][end])),
             found["x"][AS_WRITTEN]["reason"]] for cell, label, heading, found in rows]


FIGURE_COLUMNS = ["cell", "label", "heading", "members", "values", "race dates",
                  *(f"{name} ({end})" for end in BSP_ENDS for name in ("mean", "SD", "CI low", "CI high")), "note"]


def _primary_lines(side, item):
    """R4 for one side: the label with everything it is printed with."""
    if item["cell"] is None:
        lines = [f"### {side}: no primary cell, label {item['label']}", "",
                 "No move cell passed the selection's gates, so nothing is tested for this side. It still counts in "
                 "the family.", ""]
    else:
        lines = [f"### {side}: primary cell {item['cell']} (band {item['band']}), label {item['label']}", ""]
    lines += [*(f"- {text}" for text in item["printed_with"]), f"- {item['text']}"]
    if item["cell"] is None:
        return lines + [""]
    lines += [f"- band allowance: {_points(item['allowance'])} points; members on the test label: {item['members']}; "
              f"not scored: {_share(item['not_scored_share'])}; at k = 1: {_share(item['k_1_share'])}; detectable "
              f"effect (points): {_cell_text(_points(item['detectable']))}; figures given: "
              f"{_cell_text(item['figures_given'])}",
              "- checks: " + ", ".join(f"{name} {'yes' if held else 'no'}" for name, held in item["checks"].items()),
              ""]
    test, kill, net = item["test"], item["kill"], item["net_lower"]
    lines += _table(["test (test label, gross x for the side: LAY reads -x)", "BSP end", "n", "race dates", "mean", "CI low", "CI high",
                     "p one-sided", "Holm p", "rejected", "note"],
                    [["claim", test["bsp_end"], test["values"], test["race_dates"], _points(test["mean"]),
                      _points(test["ci_low"]), _points(test["ci_high"]), test["p_one_sided"],
                      test["holm_p_adjusted"], test["holm_rejected"], test["reason"]]])
    lines += _table(["bound (net_held)", "labels", "BSP end", "n", "race dates", "mean", "bound", "figures given",
                     "note"],
                    [["kill: upper", "both", kill["bsp_end"], kill["values"], kill["race_dates"], _points(kill["mean"]),
                      _points(kill["bound"]), item["kill_figures_given"], kill["reason"]],
                     ["lower", "test", net["bsp_end"], net["values"], net["race_dates"], _points(net["mean"]),
                      _points(net["bound"]), item["figures_given"], net["reason"]]])
    return lines + _table(["gross x and net at both BSP ends (test label)", "BSP end", "n", G, NET_HELD],
                          [["mean", end, found[G]["values"], _points(found[G]["mean"]), _points(found[NET_HELD]["mean"])]
                           for end, found in item["both"].items()])


def _share(value):
    return "-" if value is None else f"{round(100 * value, 2)}%"


def _registered_lines(plan, value):
    """The registered pass's tables R1 to R6."""
    labels = value["partitions"]
    explore, test = labels["explore_label"], labels["test_label"]
    levels, moves = value["level_figures"], value["move_figures"]
    lines = [f"## R1 Level cells per label (points of stake; descriptive, no p; {levels['not_comparable']})", "",
             f"Cells in this table: c = {levels['cells']}; sqrt(2 ln c) = {levels['sqrt_2_ln_c']}. "
             f"{levels['removals']}", ""]
    lines += _table(FIGURE_COLUMNS, _figure_rows([(cell, label, levels["headings"][label], found)
                                                  for cell, by_label in levels["by_cell"].items()
                                                  for label, found in by_label.items()]))
    lines += ["Classes of the level cells' members", ""]
    lines += _table(["cell", "label", *plan.classes],
                    [[cell, label, *found["classes"].values()] for cell, by_label in levels["by_cell"].items()
                     for label, found in by_label.items()])
    lines += ["## R2 Move cells (points of stake; descriptive, no p)", "",
              f"Cells in this table: c = {moves['cells']}; sqrt(2 ln c) = {moves['sqrt_2_ln_c']}. On {test}: "
              f"{moves['headings'][test]}.", ""]
    lines += _table(FIGURE_COLUMNS, _figure_rows([(cell, explore, moves["headings"][explore], by_label[explore])
                                                  for cell, by_label in moves["by_cell"].items()]))
    lines += _table(["cell", "label", "members", "scored values", "race dates"],
                    [[cell, test, *by_label[test].values()] for cell, by_label in moves["by_cell"].items()])
    selection = value["selection"]
    lines += [f"## R3 Selection (on {explore}'s values and {test}'s counts)", "",
              f"- eligible cells: {', '.join(selection['eligible']) or 'none'}",
              *(f"- {side} primary: {cell or 'none'}" for side, cell in selection["primaries"].items()), ""]
    sides = list(selection["primaries"])
    lines += _table(["cell", "eligible", "sizes", "SE", "detectable within the ceiling", f"values {explore}",
                     f"race dates {explore}", f"values {test}", f"race dates {test}", "SE of x (points)",
                     "detectable effect (points)", *(f"mean / SE, {side}" for side in sides), "figures given"],
                    [[cell, item["eligible"], item["checks"]["sizes"], item["checks"]["se"],
                      item["checks"]["detectable_within_ceiling"], item["explore"]["values"],
                      item["explore"]["race_dates"], item["test"]["values"], item["test"]["race_dates"],
                      _points(item["se"]), _points(item["detectable"]),
                      *(item["mean_over_se"][side] for side in sides), item["figures_given"]]
                     for cell, item in selection["cells"].items()])
    lines += ["## R4 Primaries (the only tests, p-values and labels of this pass)", ""]
    for side, item in value["primaries"].items():
        lines += _primary_lines(side, item)
    lines += [f"## R5 The primaries on {test} by half-year (no mean is printed)", ""]
    lines += _table(["side", "cell", "half-year", "values", "race dates", "mean above 0"],
                    [[side, item["cell"], half, part["values"], part["race_dates"], part["mean_above_0"]]
                     for side, item in value["primaries"].items() if item["half_years"]
                     for half, part in item["half_years"].items()])
    books = value["bsp_book"]
    keys = next((list(item["quantiles"]) for item in books.values() if item["quantiles"]), [])
    lines += ["## R6 The sum of 1 / BSP per market with a scored value (nearest-rank quantiles)", ""]
    return lines + _table(["label", "markets", *keys], [[label, item["markets"], *((item["quantiles"] or {}).get(q)
                                                                                  for q in keys)]
                                                        for label, item in books.items()])


def markdown(plan, value):
    """summary.md: the summary's tables as Markdown. x is printed as points of stake (100 x x)."""
    registered = value["pass"] == REGISTERED
    labels = list(plan.labels)
    times = [str(at_s) for at_s in plan.times_s]
    bands = [band for band, _, _ in plan.bands]
    lines = [f"# Study {value['study']}: {value['pass']} pass", "",
             f"- registration sha256: `{value['registration_sha256']}`",
             f"- split file sha256: `{value['partitions_sha256']}`", f"- input sha256: `{value['input_sha256']}`",
             f"- index records: {value['records']}; files opened: {value['replayed_markets']}",
             f"- partition labels: explore {labels[0]}, test {labels[1]}",
             "- x = LTP / BSP - 1 per runner, a fraction of the stake; these tables print 100 x x as points of stake",
             f"- times are seconds before the off; a level cell is a band at a time (A@-600), a move cell a band's "
             f"runners at {plan.entry_s} by their move from a start", ""]
    if not registered:
        lines += [f"Withheld in this pass: {value['withheld']}.", ""]
    lines += [f"A group with fewer than {plan.min_values} values gives its counts only.", ""]
    coverage = value["coverage"]
    lines += ["## B1 Records by month and partition label (the BASIC tier)", ""]
    lines += _table(["month", *labels, "no registered label"],
                    [[month, *(row[label] for label in labels), row[NO_LABEL]]
                     for month, row in coverage["by_month"].items()])
    lines += [f"Records with no off time: {coverage['records_without_an_anchor']}. Records with an off time and no "
              f"registered label: {coverage['records_without_a_registered_label']}. Files opened: "
              f"{value['replayed_markets']}.", ""]
    lines += ["## B2 Market codes per time (each column adds up to the record count)", ""]
    codes = value["codes"]
    lines += _table(["code", *times], [[code, *(codes[at_s][code] for at_s in times)]
                                       for code in plan.codes + (VIEWED,) if any(codes[at_s][code] for at_s in times)])
    lines += ["## B3 Runners per time and label (runners ACTIVE and listed at a viewed time)", ""]
    runner_columns = [*(f"no_ltp: {kind}" for kind in NO_LTP_KINDS), *RUNNER_CODES[1:], PRICED]
    lines += _table(["time", "label", "markets viewed", "markets with a priced runner", *runner_columns,
                     *(f"priced in band {band}" for band in bands)],
                    [[at_s, label, item["markets_viewed"], item["markets_with_a_priced_runner"],
                      *item["no_ltp"].values(), *(item[code] for code in RUNNER_CODES[1:] + (PRICED,)),
                      *item["priced_by_band"].values()]
                     for at_s in times for label, item in value["runners"][at_s].items()])
    lines += ["## B4 Classes of priced runners per time, band and label", ""]
    lines += _table(["time", "band", "label", *plan.classes],
                    [[at_s, band, label, *classes.values()] for at_s in times
                     for band, by_label in value["classes"][at_s].items() for label, classes in by_label.items()])
    ages = [(at_s, band, item) for at_s in times for band, item in value["ages"][at_s].items()]
    keys = next((list(item["seconds_quantiles"]) for _, _, item in ages if item["seconds_quantiles"]), [])
    lines += ["## B5 Age of the price in seconds (since it last changed to a new value; nearest-rank quantiles over "
              "the runners coded ltp_before_last_removal, ltp_stale or priced; an age is not an outcome)", ""]
    lines += _table(["time", "band", "runners", *keys],
                    [[at_s, band, item["runners"], *((item["seconds_quantiles"] or {}).get(q) for q in keys)]
                     for at_s, band, item in ages])
    groups = value["move_groups"]
    lines += [f"## B6 Moves of the runners priced at {plan.entry_s}, per band, start and label (counts)", "",
              "Undefined moves by reason and the members of the cells add up to the level cell's members. k is the "
              "number of price changes after the end price, up to the entry price."
              + ("" if registered else " The two moved cells of a start are shown as moved_larger and moved_smaller,"
                                       " by scored values of both labels together, not by their direction."), ""]
    lines += _table(["group", "label", "members", *UNDEFINED, "flagged suspension_in_window"],
                    [[group, label, item["members"], *item["undefined"].values(), item["flagged"]]
                     for group, by_label in groups.items() for label, item in by_label.items()])
    lines += _table(["group", "label", "cell", "members", "scored values", "race dates",
                     *(f"k = {name}" for name in CHANGES_BETWEEN)],
                    [[group, label, name, item["members"], item["scored"], item["race_dates"], *item["k"].values()]
                     for group, by_label in groups.items() for label, found in by_label.items()
                     for name, item in found["cells"].items()])
    spreads = value["spreads"]["by_cell"]
    ranges = next((list(item["ranges"]) for item in spreads.values() if item["ranges"]), [])
    lines += ["## B7 Spread of x at the BSP as written, both labels pooled (points of stake)", "",
              "SD and nearest-rank ranges q(1 - a) - q(a) over each move cell's scored values. A cell that shows "
              "rest_not_given is the earlier start of a band whose two starts hold nearly the same values.", ""]
    lines += _table(["cell", "values", "race dates", "SD", *ranges, "note"],
                    [[cell, item["values"], item["race_dates"], _points(item["sd"]),
                      *(_points((item["ranges"] or {}).get(name)) for name in ranges), item["reason"]]
                     for cell, item in spreads.items()])
    lines += [f"## B8 Detectable effect on {labels[1]} (points of stake; {CLUSTERED})", "",
              "(t(0.99; G - 1) + t(0.8; G - 1)) x SE x the square root of n / n_V, with G and n_V the race dates and "
              f"values on {labels[1]} and n the values of B7; empty whenever B7's SD is.", ""]
    lines += _table(["cell", f"values {labels[1]}", f"race dates {labels[1]}", "detectable effect"],
                    [[cell, item["detectable"]["test_values"], item["detectable"]["test_race_dates"],
                      _points(item["detectable"]["effect"])] for cell, item in spreads.items()])
    precision = value["bsp_precision"]
    lines += ["## B9 Diagnostics (counts)", "",
              f"- scored BSPs of the level cells at {plan.entry_s}, both labels, that are whole hundredths: "
              f"{precision['scored_bsps_whole_hundredths']}; that are not: "
              f"{precision['scored_bsps_not_whole_hundredths']}; the two BSP ends decide apart: "
              f"{'yes' if precision['bsp_ends_decide_apart'] else 'no'}",
              "- times that reached the view with no bspMarket value: "
              + ", ".join(f"{at_s} {count}" for at_s, count in precision["bsp_market_with_no_value"].items()),
              f"- compressed bytes the replay read: {precision['bytes_walked']}; drained, hashed and not parsed: "
              f"{precision['bytes_drained']}", ""]
    if registered:
        lines += _registered_lines(plan, value)
    return "\n".join(lines).rstrip("\n") + "\n"


# ---------------------------------------------------------------------------------------------------------------
# The run

def _counts(plan, tally):
    priced = Counter(row.at_s for row in tally.rows)
    held = Counter(row.at_s for row in tally.rows if row.klass == SCORED)
    return {"records": tally.records, "replayed_markets": tally.replayed,
            "viewed": {str(at_s): tally.codes[at_s][VIEWED] for at_s in plan.times_s},
            "priced": {str(at_s): priced[at_s] for at_s in plan.times_s},
            "scored": {str(at_s): held[at_s] for at_s in plan.times_s}}


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
            # Registration data.duplicates: a market of the BASIC tier is keyed by its id alone.
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
            handle, stored = _ledger(ledger, ledger_head, root, target, request, split_raw, blind and blind[2])
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
    lock_left = False
    try:                                            # the one ledger entry, before any file or stdout write
        head = handle.record(request, split_raw, outputs[SUMMARY_JSON]["sha256"], COMPLETED, clock)
    except UnlockError as error:                    # the entry was written and confirmed; the lock file is left
        head, lock_left = error.head, True
    except LedgerError as error:
        return {"status": "failed", "failure": str(error)}
    # From here the entry stands: whatever happens, the result names the head, and the re-run is a repeat from it.
    head, repeat = head_text(head), stored is not None
    try:
        own = head if stored is None else head_text((stored["seq"], stored["entry_sha256"]))
        passes = handle.counts(plan.lineage)
        receipt = {"kind": RECEIPT_KIND, "schema_version": SCHEMA_VERSION, "study": plan.study, "pass": pass_name,
                   "status": "completed", "registration_sha256": plan.sha256,
                   "partitions_sha256": plan.split.sha256, "input_sha256": input_sha256, "input": descriptor,
                   "code_commit": code_commit, "source_descriptor_sha256": sources_sha256,
                   "interpreter": "{0} {1}.{2}.{3}".format(sys.implementation.name, *sys.version_info[:3]),
                   "blind_receipt_sha256": None if blind is None else blind[1], "ledger_head": head,
                   "ledger_entry": own, "ledger_repeat": repeat, "ledger_counts": passes,
                   "bytes_walked": tally.bytes_walked, "bytes_drained": tally.bytes_drained, "counts": counts,
                   "outputs": outputs, "started_at": started_at, "recorded_at": clock()}
        receipt_bytes = canonical_json(receipt) + b"\n"
        target = _out_path(out, root)
        _write(target, [(SUMMARY_JSON, summary_bytes), (SUMMARY_MD, markdown_bytes), (RECEIPT_JSON, receipt_bytes)])
    except (LedgerError, StudyRefused) as error:
        return {"status": "failed", "failure": str(error), "ledger_head": head, "ledger_lock_left": lock_left}
    except Exception as error:      # a fault of the code: its class name only (main), with the head that stands
        return {"status": "failed", "failure": f"internal:{type(error).__name__}", "ledger_head": head,
                "ledger_lock_left": lock_left}
    return {"status": "completed", "study": plan.study, "pass": pass_name, "registration_sha256": plan.sha256,
            "input_sha256": input_sha256, "summary_sha256": outputs[SUMMARY_JSON]["sha256"],
            "receipt_sha256": hashlib.sha256(receipt_bytes).hexdigest(), "ledger_head": head,
            "ledger_repeat": repeat, "ledger_lock_left": lock_left, "ledger_counts": passes, **counts}


def build_parser():
    parser = argparse.ArgumentParser(prog="python3 -m betangel_research.betfair.backtest.early_study",
                                     description="Registered measurement early-price-v1 on local historical files; "
                                                 "nothing is ordered and nothing can reach an exchange.")
    parser.add_argument("--study", required=True, help="the study to run: early-price-v1")
    parser.add_argument("--pass", dest="pass_name", required=True, choices=PASSES, help="blind first, then registered")
    parser.add_argument("--registration-sha256", required=True, help="sha256 of the registration file's raw bytes")
    parser.add_argument("--archive-root", required=True, help="the BASIC data root with manifests/, raw/ and index/")
    parser.add_argument("--out", required=True, help="a new directory outside every Git worktree and data tree")
    parser.add_argument("--code-commit", required=True, help="the 40-character commit the code was checked out at")
    parser.add_argument("--ledger", required=True, help="the study ledger's directory; it must exist")
    parser.add_argument("--ledger-head", required=True, help="the ledger's last head: none, or SEQ:SHA256")
    parser.add_argument("--blind-receipt", help="registered pass: the blind pass's receipt.json")
    parser.add_argument("--registration", help="registration file (default: research/studies/<study>.json); the "
                                               "split file is read beside its studies/ folder")
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
