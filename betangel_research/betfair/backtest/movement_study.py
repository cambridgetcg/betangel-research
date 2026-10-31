"""python3 -m betangel_research.betfair.backtest.movement_study: run registered measurement movement-v1.

--pass blind|registered --registration-sha256 HEX --archive-root DIR --out DIR --code-commit HEX
[--blind-receipt PATH] [--registration PATH] runs research/studies/movement-v1.json (or --registration) over every
archive ingested and indexed under the data root DIR (archive-ingest and archive-index; the index is the root's own
index/ directory) and writes three files into the new directory --out: summary.json and summary.md, aggregate
tables only, and receipt.json. They are written into "<out>.partial" (mode 0700, files 0600) and renamed to --out
only when all three are whole. It prints one JSON line of counts, codes and hashes and exits 0 (completed) or 2
(refused, or failed: --out is not created then; out:partial_left alone says a "<out>.partial" directory may be left).
It is a subcommand of its own, not of the backtest CLI, because __main__.py is one of the certified engine's sources
(certificate.py's descriptor) and stays byte-identical; this module and movement.py are in that descriptor too, so
adding them changed the certificate key.

Refusals, before any record is read: the registration file must be a regular file whose raw bytes hash to
--registration-sha256 (both passes: one registration names both) and that movement.parse_registration accepts;
--code-commit is 40 lowercase hex characters (nothing here runs git: the operator names the commit, and the receipt
also records the sha256 of the backtest package's sources as certificate.source_descriptor hashes them); the data
root passes path_guard and archive.check_data_root (outside the repository and every Git worktree); --out must not
exist yet, its parent must, it passes path_guard as a new path, neither it nor its parent may lie in the repository,
in a Git worktree or inside the data root (nor hold it), and no component of it, as given or resolved, may be a
licensed-data or log tree's name (DATA_TREES, compared ignoring case: the lab keeps those trees under the home
directory, which no backtest module may look up, so any component with such a name refuses), and "<out>.partial"
must not exist. The blind pass takes no --blind-receipt. The registered pass needs the blind pass's receipt:
canonical, completed, of this registration, of the same --code-commit and backtest-source sha256
(blind_receipt:another_code: code changed after people read the blind tables needs a new blind pass) and, once the
input is read, of the same input sha256; the registered receipt names its sha256. That enforces the order of the
passes on one machine, not that the registered pass runs once: running it once is the operator's rule
(docs/movement-study.md).

Input: runner._inputs (every manifest, index head and markets file under the root, checked and hashed as the P0 runner
checks them; input_sha256 is the sha256 of that descriptor; every manifest's read limits must be the ones the replay
uses, input:<sha>:limits_not_the_runs, as the P0 runner requires) and runner._records (each markets file rehashed
while read). Every record's shape and months are checked as the P0 runner's first pass checks them (a month the current
month gate refuses fails the run, so sealed months are never read). A record of a registered tier is keyed by its
market id alone (registration data.duplicates): the same id under two registered tiers fails the run
(market_in_two_tiers), as does one id seen twice in one plan (duplicate_market); a record of another plan is keyed
by plan and id.
Each record is decided at every registered offset (movement.market_code); a record that reaches the book predicates
at any offset has its primary member replayed once (movement.replay_market up to the first-published off, after the
member's path is guarded and its manifest entry checked as replay_view checks it), and the bytes read must hash to
the manifest's. Failures (ReplayProblem, a changed markets file, a malformed record) fail the run; the failure code
drops any market id.

Tables (summary): registered-tier records, issued and eligible decisions by registered month (every month listed, so
a gap shows as 0); per decision time, every market code and measurement class count (the codes add up to the record
count), the diagnostics (issued decisions whose market status at t is not OPEN or that are in play; decisions whose
unique favourite's best back size is below diagnostics.min_stake_p), issued and eligible counts by price band x
field-size band, and over the eligible bets the NCLV SD and, in the registered pass only, the mean with its CR1
interval, by all, price band and field-size band; tick quantiles of P - BSP by all and price band, in the blind pass
each minus its group's median (no location), in the registered pass as they are. Late move: exclusions, undefined
reasons, the cells and their counts by price band, the controls (movement.py and the registration), and in the
registered pass the contrasts with Holm, each control's contrasts beside the main contrasts over the same bets, the
kill rule and the flags (movement.kill_and_flags). A group below statistics.min_bets_for_statistics bets gives its
counts only. The blind summary holds no location statistic of NCLV or of ticks (tests/test_backtest_movement.py
checks every key and the Markdown text). Neither summary holds a market id, a selection id or a per-market row; the
summary has no clock, so the same registration, input and code give the same bytes.

Standard library only; it imports the backtest modules named above and nothing outside the package but what they
import (tests/test_betfair_backtest_boundaries.py). It never places an order, logs in or reads an account.
"""

import argparse
from collections import namedtuple
import bz2
from dataclasses import asdict
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import re
import sys

from .archive import (DEFAULT_LIMITS, REPOSITORY_ROOT, ArchiveProblem, HashingReader, check_data_root, month_gate,
                      safe_target, utc_month)
from .certificate import CertificateProblem, source_descriptor
from .contract import canonical_json
from .market_index import loads_strict, market_sort_key
from .movement import (CELLS, CLASSES, CONTRASTS, CONTROLS, ELIGIBLE, ISSUED, LATE_EXCLUSIONS, MARKET_CODES,
                       MAX_REGISTRATION_BYTES, RANDOM_RUNNER_LABEL, STUDY_ID, UNDEFINED_REASONS, RegistrationError,
                       active_ids, band, cr1_contrast, cr1_mean, decision_time, drawn, favourite, field_share, holm,
                       hundredths, kill_and_flags, late_move_cell, log_move, market_code, measure, nclv, nearest_rank,
                       parse_registration, permuted_contrast, race_date, registration_sha256, replay_market, sample_sd,
                       select, tick_position, valid_bsp, window_problem)
from .path_guard import PathDenied, guard_path
from .replay import ReplayProblem, _member
from .runner import RunRefused, _inputs, _record_months, _records, utc_now_text

SUMMARY_KIND, RECEIPT_KIND, SCHEMA_VERSION = "betangel_movement_summary", "betangel_movement_receipt", 1
BLIND, REGISTERED = "blind", "registered"
PASSES = (BLIND, REGISTERED)
DEFAULT_REGISTRATION = REPOSITORY_ROOT / "research" / "studies" / "movement-v1.json"
# Names of the lab's licensed-data and log trees (module docstring); an --out with such a component refuses.
DATA_TREES = ("betangel-data", "betangel-logs", "soccer-data")
SUMMARY_JSON, SUMMARY_MD, RECEIPT_JSON = "summary.json", "summary.md", "receipt.json"
PARTIAL_SUFFIX = ".partial"
SHA256_RE = re.compile(r"[0-9a-f]{64}")
COMMIT_RE = re.compile(r"[0-9a-f]{40}")
MAX_RECEIPT_BYTES = 1 << 20
WITHHELD = ("location statistics of NCLV and of ticks P - BSP, contrasts, control effects and flags: the "
            "registration's blind_pass.withholds")

Bet = namedtuple("Bet", "race_date price_band field_band nclv ticks")
LateBet = namedtuple("LateBet", "key race_date price_band nclv move cell placebo random")
# Diagnostic counts per decision time (registration diagnostics.rule).
DIAGNOSTICS = ("issued_status_not_open", "issued_in_play", "favourite_best_back_below_min_stake")
NO_ANCHOR, OUTSIDE = "no_anchor", "outside_registered_months"
MEDIAN = Fraction(1, 2)


class StudyRefused(Exception):
    """A refusal before anything is written; str() is "<where>:<reason>"."""


def _refuse(code):
    raise StudyRefused(code)


def _guarded(path, where, prospective=False):
    try:
        return guard_path(path, where, prospective)
    except PathDenied as error:
        raise StudyRefused(str(error)) from None


def _read_regular(path, where, cap):
    """The bytes of an existing regular file (not a symlink, FIFO or directory), at most cap of them."""
    resolved = _guarded(path, where)
    try:
        mode = Path(path).lstat().st_mode
        if mode & 0o170000 != 0o100000:
            _refuse(f"{where}:not_a_regular_file")
        with resolved.open("rb") as handle:
            data = handle.read(cap + 1)
    except OSError:
        _refuse(f"{where}:unreadable")
    if len(data) > cap:
        _refuse(f"{where}:too_large")
    return data


def _in_data_tree(path):
    return any(part.casefold() in DATA_TREES for part in Path(path).parts)


def _out_path(out, root):
    """The resolved --out after the checks in the module docstring; StudyRefused otherwise."""
    if out is None or not str(out).strip():
        _refuse("out:missing_path")
    if _in_data_tree(Path(out).absolute()):
        _refuse("out:under_a_data_tree")
    resolved = _guarded(out, "out", prospective=True)
    if _in_data_tree(resolved):
        _refuse("out:under_a_data_tree")
    _, problems = check_data_root(resolved.parent)
    if problems:
        _refuse(problems[0].replace("data_root:", "out:", 1))
    if root is not None and (resolved == root or root in resolved.parents or resolved in root.parents):
        _refuse("roots:not_distinct")
    partial = resolved.with_name(resolved.name + PARTIAL_SUFFIX)
    if partial.exists() or partial.is_symlink():
        _refuse("out:partial_present")
    return resolved


def _registration(path, expected):
    if not (isinstance(expected, str) and SHA256_RE.fullmatch(expected)):
        _refuse("registration_sha256:malformed")
    raw = _read_regular(DEFAULT_REGISTRATION if path is None else path, "registration", MAX_REGISTRATION_BYTES)
    if registration_sha256(raw) != expected:
        _refuse("registration:sha256_mismatch")
    try:
        return parse_registration(raw)
    except RegistrationError as error:
        raise StudyRefused(str(error)) from None


def _blind_receipt(path, registration):
    """(receipt, sha256 of its bytes) of a completed blind-pass receipt of this registration; StudyRefused
    otherwise."""
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
         receipt.get("status")) != (RECEIPT_KIND, SCHEMA_VERSION, STUDY_ID, BLIND, "completed")):
        _refuse("blind_receipt:not_a_completed_blind_receipt")
    if receipt.get("registration_sha256") != registration.sha256:
        _refuse("blind_receipt:another_registration")
    return receipt, hashlib.sha256(data).hexdigest()


class Tally:
    """Everything the summary needs, kept per decision time and for the late move; no market id leaves it."""

    def __init__(self, registration):
        self.reg = registration
        self.records = self.replayed = self.null_ladder_markets = 0
        offsets = registration.decision_offsets_s
        self.months = {month: {"records": 0, "issued": dict.fromkeys(offsets, 0), "eligible": dict.fromkeys(offsets, 0)}
                       for month in registration.months + (OUTSIDE, NO_ANCHOR)}
        self.codes = {offset: dict.fromkeys(MARKET_CODES + (ISSUED,), 0) for offset in offsets}
        self.classes = {offset: dict.fromkeys(CLASSES, 0) for offset in offsets}
        self.diagnostics = {offset: dict.fromkeys(DIAGNOSTICS, 0) for offset in offsets}
        self.strata = {offset: {(p, f): [0, 0] for p, _, _ in registration.price_bands
                                for f, _, _ in registration.field_bands} for offset in offsets}
        self.bets = {offset: [] for offset in offsets}
        self.late_exclusions = dict.fromkeys(LATE_EXCLUSIONS, 0)
        self.late_undefined = dict.fromkeys(UNDEFINED_REASONS, 0)
        self.placebo_undefined = dict.fromkeys(LATE_EXCLUSIONS[:-1] + UNDEFINED_REASONS, 0)
        self.random_undefined = {"no_candidate": 0}
        self.late = []


def _window_move(reg, replay, selection_id, start_offset, end_offset, window_end):
    """(M, None, None) or (None, exclusion, undefined reason) for one window: the window's checks over
    (off + start_offset, window_end], then the runner's shares at both offsets."""
    start = replay.moments[start_offset]
    problem = window_problem(replay, start.t, window_end)
    if problem is not None:
        return None, problem, None
    later, reason = field_share(reg, replay.moments[end_offset], selection_id)
    if reason is None:
        earlier, reason = field_share(reg, start, selection_id)
    if reason is not None:
        return None, "cell_undefined", reason
    return log_move(later, earlier), None, None


def _random_runner(reg, record, replay, selection_id):
    """(NCLV, cell) of the seeded random runner (registration registered_pass.controls.random_runner), or None."""
    moment = replay.moments[reg.late_offset_s]
    candidates = []
    for sid in moment.active():
        if sid == selection_id:
            continue
        later, reason = field_share(reg, moment, sid)
        earlier, other = field_share(reg, replay.moments[reg.late_from_offset_s], sid) if reason is None \
            else (None, reason)
        bsp = valid_bsp(record, sid) if other is None else None
        if bsp is not None:
            candidates.append((sid, later, earlier, bsp))
    if not candidates:
        return None
    sid, later, earlier, bsp = candidates[drawn(reg.random_runner_seed,
                                                RANDOM_RUNNER_LABEL.format(market_id=record["market_id"]),
                                                len(candidates))]
    return float(nclv(moment.quote(sid)[0][0], bsp, reg.commission)), late_move_cell(reg, log_move(later, earlier))


def _late_move(reg, record, replay, selection, value, day, price_band, tally):
    off = decision_time(record, 0)
    move, exclusion, reason = _window_move(reg, replay, selection.selection_id, reg.late_from_offset_s,
                                           reg.late_offset_s, off)
    if exclusion is not None:
        tally.late_exclusions[exclusion] += 1
        if reason is not None:
            tally.late_undefined[reason] += 1
        return
    placebo_move, exclusion, reason = _window_move(reg, replay, selection.selection_id, reg.placebo_from_offset_s,
                                                   reg.placebo_to_offset_s,
                                                   replay.moments[reg.placebo_to_offset_s].t)
    if placebo_move is None:
        tally.placebo_undefined[reason or exclusion] += 1
    random = _random_runner(reg, record, replay, selection.selection_id)
    if random is None:
        tally.random_undefined["no_candidate"] += 1
    tally.late.append(LateBet(key=market_sort_key(record["market_id"]), race_date=day, price_band=price_band,
                              nclv=value, move=move, cell=late_move_cell(reg, move), random=random,
                              placebo=None if placebo_move is None else late_move_cell(reg, placebo_move)))


def _replay(root, manifest, record, reg, limits):
    """movement.replay_market over the record's primary member, after replay_view's checks of the member, and the
    member's bytes hashed against the manifest."""
    market_id = record["market_id"]
    member = _member(manifest, record)
    try:
        raw = safe_target(root, manifest["archive"]["sha256"], member["path"])
    except ArchiveProblem as error:
        raise ReplayProblem(f"replay:{market_id}:{error}") from None
    raw = _guarded(raw, "raw")
    off = decision_time(record, 0)
    try:
        if not raw.is_file():
            raise ReplayProblem(f"replay:{market_id}:member_missing")
        with open(raw, "rb") as handle:
            reader = HashingReader(handle)
            with bz2.BZ2File(reader) as stream:
                replayed = replay_market(stream, record, reg.ladders[record["provenance"]["plan"]],
                                         reg.replay_offsets_s, off, limits)
            digest = reader.drain()
    except OSError:
        raise ReplayProblem(f"replay:{market_id}:member_unreadable_file") from None
    if digest != (member["sha256"], member["size_bytes"]):
        raise ReplayProblem(f"replay:{market_id}:member_hash_mismatch")
    return replayed


def _month(reg, record):
    """The coverage row of a registered-tier record: its first-published month when registered, else OUTSIDE, or
    NO_ANCHOR without one; None for a record of another plan."""
    if record["provenance"]["plan"] not in reg.tiers:
        return None
    first = record["decision_time"]["first_published_market_time"]
    month = None if first is None else utc_month(first["value"])
    if month is None:
        return NO_ANCHOR
    return month if reg.start_month <= month <= reg.end_month else OUTSIDE


def _diagnose(reg, tally, offset, active, moment, issued):
    """Count the registration's diagnostics for one decision that reached the book predicates."""
    if issued and moment.view.field("status") != "OPEN":
        tally.diagnostics[offset]["issued_status_not_open"] += 1
    if issued and moment.view.field("inPlay") is True:
        tally.diagnostics[offset]["issued_in_play"] += 1
    _, found = favourite(active, moment)
    if found is not None and found[1][1] * 100 < reg.min_stake_p:
        tally.diagnostics[offset]["favourite_best_back_below_min_stake"] += 1


def decide_record(reg, record, root, manifest, tally, limits=DEFAULT_LIMITS):
    """Decide one index record at every registered offset and add it to the tally (module docstring)."""
    tally.records += 1
    month = _month(reg, record)
    if month is not None:
        tally.months[month]["records"] += 1
    codes = {offset: market_code(reg, record, offset) for offset in reg.decision_offsets_s}
    replay = None
    if any(code is None for code in codes.values()):
        replay = _replay(root, manifest, record, reg, limits)
        tally.replayed += 1
        tally.null_ladder_markets += bool(replay.null_ladder_updates)
    for offset in reg.decision_offsets_s:
        code, selection, t = codes[offset], None, decision_time(record, offset)
        if code is None:
            active = active_ids(record, t)
            code, selection = select(reg, active, replay.moments[offset])
            _diagnose(reg, tally, offset, active, replay.moments[offset], code is None)
        if code is not None:
            tally.codes[offset][code] += 1
            continue
        tally.codes[offset][ISSUED] += 1
        tally.months[month]["issued"][offset] += 1
        klass, bsp = measure(record, t, selection.selection_id, active)
        tally.classes[offset][klass] += 1
        price_band = band(reg.price_bands, hundredths(selection.back[0]))
        field_band = band(reg.field_bands, len(active))
        tally.strata[offset][price_band, field_band][0] += 1
        if klass != ELIGIBLE:
            continue
        tally.months[month]["eligible"][offset] += 1
        tally.strata[offset][price_band, field_band][1] += 1
        value = float(nclv(selection.back[0], bsp, reg.commission))
        low, high = tick_position(selection.back[0]), tick_position(bsp)
        day = race_date(record)
        tally.bets[offset].append(Bet(race_date=day, price_band=price_band, field_band=field_band, nclv=value,
                                      ticks=None if high is None else float(low - high)))
        if offset == reg.late_offset_s:
            _late_move(reg, record, replay, selection, value, day, price_band, tally)


# ---------------------------------------------------------------------------------------------------------------
# Tables

def _round(value, places=6):
    return None if value is None else round(value, places)


def _q(value):
    return format(float(value), "g")


def _dates(rows):
    return len({day for day, _ in rows})


def _group(reg, rows, registered):
    """{bets, race_dates, nclv_sd[, nclv_mean]} of [(race date, NCLV)] rows; statistics only from
    min_bets_for_statistics bets."""
    result = {"bets": len(rows), "race_dates": _dates(rows), "nclv_sd": None}
    enough = len(rows) >= reg.min_bets_for_statistics
    if enough:
        result["nclv_sd"] = _round(sample_sd([x for _, x in rows]))
    if registered:
        result["nclv_mean"] = None
        if enough:
            result["nclv_mean"] = {key: _round(value) if isinstance(value, float) else value
                                   for key, value in cr1_mean(rows, reg.confidence_level, reg.min_clusters).items()}
    return result


def _ticks(reg, values, registered):
    """{bets, quantiles} in the registered pass; in the blind pass {bets, quantiles_minus_median}, each registered
    quantile less the group's nearest-rank median, which keeps the spread and shape and drops the location."""
    values = [value for value in values if value is not None]
    key = "quantiles" if registered else "quantiles_minus_median"
    if len(values) < reg.min_bets_for_statistics:
        return {"bets": len(values), key: None}
    shift = 0.0 if registered else nearest_rank(values, [MEDIAN])[MEDIAN]
    return {"bets": len(values),
            key: {_q(q): _round(value - shift, 3) for q, value in nearest_rank(values, reg.quantiles).items()}}


def _by(rows, key, names):
    return {name: [row for row in rows if key(row) == name] for name in names}


def _decision_tables(reg, tally, offset, registered):
    bets = tally.bets[offset]
    prices = [name for name, _, _ in reg.price_bands]
    fields = [name for name, _, _ in reg.field_bands]
    rows = [(bet.race_date, bet.nclv) for bet in bets]
    by_price = _by(bets, lambda bet: bet.price_band, prices)
    by_field = _by(bets, lambda bet: bet.field_band, fields)
    return {
        "codes": dict(tally.codes[offset]),
        "classes": dict(tally.classes[offset]),
        "diagnostics": dict(tally.diagnostics[offset]),
        "strata": {p: {f: {"issued": tally.strata[offset][p, f][0], "eligible": tally.strata[offset][p, f][1]}
                       for f in fields} for p in prices},
        "nclv": {"all": _group(reg, rows, registered),
                 "price_band": {name: _group(reg, [(b.race_date, b.nclv) for b in by_price[name]], registered)
                                for name in prices},
                 "field_size": {name: _group(reg, [(b.race_date, b.nclv) for b in by_field[name]], registered)
                                for name in fields}},
        "ticks_p_minus_bsp": {"all": _ticks(reg, [bet.ticks for bet in bets], registered),
                              "price_band": {name: _ticks(reg, [b.ticks for b in by_price[name]], registered)
                                             for name in prices}},
    }


def _cells(bets, cell_of, value_of):
    """{cell: [(race date, value)]} of the bets whose cell_of is that cell."""
    return {cell: [(bet.race_date, value_of(bet)) for bet in bets if cell_of(bet) == cell] for cell in CELLS}


def _contrasts(reg, rows):
    return {name: cr1_contrast(rows[first], rows[second], reg.min_cell_bets, reg.min_cell_race_dates)
            for name, first, second in CONTRASTS}


def _rounded(result):
    return {key: _round(value) if isinstance(value, float) else value for key, value in result.items()}


def _control_bets(late):
    """{control: the late-move bets it is computed on}: those with an earlier-window cell, all of them for shuffled_m
    (a permutation keeps each race date's cell counts), and those with a random runner."""
    return {"earlier_window": [bet for bet in late if bet.placebo is not None], "shuffled_m": list(late),
            "random_runner": [bet for bet in late if bet.random is not None]}


def _control_contrasts(reg, late, bets):
    """{control: {contrast: result}} (registration registered_pass.controls)."""
    shuffled = [(bet.race_date, bet.cell, bet.nclv) for bet in late]
    return {
        "earlier_window": _contrasts(reg, _cells(bets["earlier_window"], lambda bet: bet.placebo,
                                                 lambda bet: bet.nclv)),
        "shuffled_m": {name: permuted_contrast(shuffled, first, second, reg.min_cell_bets, reg.min_cell_race_dates)
                       for name, first, second in CONTRASTS},
        "random_runner": _contrasts(reg, _cells(bets["random_runner"], lambda bet: bet.random[1],
                                                lambda bet: bet.random[0])),
    }


def _late_tables(reg, tally, registered):
    late = tally.late
    main = _cells(late, lambda bet: bet.cell, lambda bet: bet.nclv)
    bets = _control_bets(late)
    cells_of = {"earlier_window": _cells(bets["earlier_window"], lambda bet: bet.placebo, lambda bet: None),
                "shuffled_m": main,
                "random_runner": _cells(bets["random_runner"], lambda bet: bet.random[1], lambda bet: None)}
    prices = [name for name, _, _ in reg.price_bands]
    tables = {
        "decision_offset_s": reg.late_offset_s, "from_offset_s": reg.late_from_offset_s,
        "eligible_at_decision": len(tally.bets[reg.late_offset_s]), "population": len(late),
        "exclusions": dict(tally.late_exclusions), "undefined_reasons": dict(tally.late_undefined),
        "cells": {cell: _group(reg, main[cell], registered) for cell in CELLS},
        "cells_by_price_band": {cell: {p: sum(1 for bet in late if bet.cell == cell and bet.price_band == p)
                                       for p in prices} for cell in CELLS},
    }
    control_tables = {name: {"bets": len(bets[name]),
                             "cells": {cell: {"bets": len(rows[cell]), "race_dates": _dates(rows[cell])}
                                       for cell in CELLS}} for name, rows in cells_of.items()}
    control_tables["earlier_window"]["undefined"] = dict(tally.placebo_undefined)
    control_tables["random_runner"]["undefined"] = dict(tally.random_undefined)
    if not registered:
        return tables, control_tables, None
    contrasts = _contrasts(reg, main)
    decisions = holm({name: result["p_two_sided"] for name, result in contrasts.items()}, reg.holm_alpha,
                     len(CONTRASTS))
    tables["contrasts"] = {name: dict(_rounded(result), holm_p_adjusted=_round(decisions[name][0]),
                                      holm_rejected=decisions[name][1]) for name, result in contrasts.items()}
    computed = _control_contrasts(reg, late, bets)
    same = {name: _contrasts(reg, _cells(bets[name], lambda bet: bet.cell, lambda bet: bet.nclv)) for name in CONTROLS}
    for name in CONTROLS:
        control_tables[name]["contrasts"] = {key: _rounded(value) for key, value in computed[name].items()}
        control_tables[name]["main_contrasts_same_bets"] = {key: _rounded(value) for key, value in same[name].items()}
    levels = {cell: cr1_mean(main[cell], reg.confidence_level, reg.min_clusters)["mean"]
              if len(main[cell]) >= reg.min_bets_for_statistics else None for cell in CELLS}
    kill, flags = kill_and_flags(reg, contrasts, decisions, {name: (computed[name], same[name]) for name in CONTROLS},
                                 levels)
    kill = {name: {control: _rounded(item) for control, item in controls.items()} for name, controls in kill.items()}
    return tables, control_tables, {"kill_rule": kill, "flags": flags}


def _coverage(reg, tally):
    """Records of a registered tier, and their issued and eligible decisions, by registered month (every one listed,
    so a missing month shows as 0), outside those months and without an anchor."""
    def by_offset(counts):
        return {str(offset): count for offset, count in counts.items()}

    return {month: {"records": row["records"], "issued": by_offset(row["issued"]),
                    "eligible": by_offset(row["eligible"])} for month, row in tally.months.items()}


def summary(reg, tally, pass_name, input_sha256):
    """The summary of one pass (module docstring); the blind pass's holds no location statistic of NCLV."""
    registered = pass_name == REGISTERED
    late, controls, decided = _late_tables(reg, tally, registered)
    value = {"kind": SUMMARY_KIND, "schema_version": SCHEMA_VERSION, "study": STUDY_ID, "pass": pass_name,
             "registration_sha256": reg.sha256, "input_sha256": input_sha256, "records": tally.records,
             "replayed_markets": tally.replayed, "replayed_markets_with_null_ladder_update": tally.null_ladder_markets,
             "coverage_by_month": _coverage(reg, tally),
             "decision_times": {str(offset): _decision_tables(reg, tally, offset, registered)
                                for offset in reg.decision_offsets_s},
             "late_move": late, "controls": controls}
    if registered:
        value.update(decided)
    else:
        value["withheld"] = WITHHELD
    return value


def _cell_text(value):
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def _table(header, rows):
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(_cell_text(item) for item in row) + " |" for row in rows]
    return lines + [""]


def _stat_rows(groups, registered):
    rows = []
    for name, group in groups:
        row = [name, group["bets"], group["race_dates"], group["nclv_sd"]]
        if registered:
            mean = group["nclv_mean"] or {}
            row += [mean.get("mean"), mean.get("ci_low"), mean.get("ci_high"), mean.get("reason")]
        rows.append(row)
    return rows


def markdown(value):
    """summary.md: the summary's tables as Markdown, nothing else."""
    registered = value["pass"] == REGISTERED
    stat_header = ["group", "bets", "race dates", "NCLV SD"] + (
        ["mean NCLV", "CI low", "CI high", "note"] if registered else [])
    lines = [f"# Movement study {value['study']}: {value['pass']} pass", "",
             f"- registration sha256: `{value['registration_sha256']}`", f"- input sha256: `{value['input_sha256']}`",
             f"- index records: {value['records']}; markets replayed: {value['replayed_markets']}; replayed markets "
             f"with a null ladder update: {value['replayed_markets_with_null_ladder_update']}", ""]
    if not registered:
        lines += [f"Withheld in this pass: {value['withheld']}.", ""]
    offsets = list(value["decision_times"])
    lines += ["## Coverage by month (records of a registered tier; issued / eligible by decision time)", ""]
    lines += _table(["month", "records", *(f"off {offset} s" for offset in offsets)],
                    [[month, row["records"], *(f"{row['issued'][offset]} / {row['eligible'][offset]}"
                                               for offset in offsets)]
                     for month, row in value["coverage_by_month"].items()])
    for offset, tables in value["decision_times"].items():
        lines += [f"## Decision at off {offset} s", "", "### Codes", ""]
        lines += _table(["code", "records"], [[code, count] for code, count in tables["codes"].items() if count])
        lines += ["### Measurement classes of issued decisions", ""]
        lines += _table(["class", "bets"], list(tables["classes"].items()))
        lines += ["### Diagnostics", ""]
        lines += _table(["diagnostic", "decisions"], list(tables["diagnostics"].items()))
        lines += ["### Issued / eligible by price band x field size", ""]
        fields = list(next(iter(tables["strata"].values())))
        lines += _table(["band", *fields], [[band_name, *(f"{cell['issued']} / {cell['eligible']}"
                                                          for cell in row.values())]
                                            for band_name, row in tables["strata"].items()])
        lines += ["### NCLV over eligible bets", ""]
        groups = [("all", tables["nclv"]["all"])] + [(f"price {name}", group) for name, group in
                                                      tables["nclv"]["price_band"].items()]
        groups += [(f"field {name}", group) for name, group in tables["nclv"]["field_size"].items()]
        lines += _table(stat_header, _stat_rows(groups, registered))
        key = "quantiles" if registered else "quantiles_minus_median"
        lines += [f"### Ticks P - BSP (nearest-rank quantiles{'' if registered else ', each minus the median'})", ""]
        ticks = [("all", tables["ticks_p_minus_bsp"]["all"])] + list(tables["ticks_p_minus_bsp"]["price_band"].items())
        keys = next((list(item[key]) for _, item in ticks if item[key]), [])
        lines += _table(["group", "bets", *keys], [[name, item["bets"], *((item[key] or {}).get(q) for q in keys)]
                                                   for name, item in ticks])
    late = value["late_move"]
    lines += [f"## Late move, off {late['from_offset_s']} s to off {late['decision_offset_s']} s", "",
              f"Eligible bets at off {late['decision_offset_s']} s: {late['eligible_at_decision']}; "
              f"late-move population: {late['population']}.", ""]
    lines += _table(["exclusion", "bets"], list(late["exclusions"].items()))
    lines += _table(["undefined reason", "bets"], list(late["undefined_reasons"].items()))
    lines += _table(stat_header, _stat_rows(list(late["cells"].items()), registered))
    bands = list(next(iter(late["cells_by_price_band"].values())))
    lines += _table(["cell", *bands], [[cell, *row.values()] for cell, row in late["cells_by_price_band"].items()])
    if registered:
        lines += ["### Contrasts (CR1 by race date, t(G - 1), Holm)", ""]
        lines += _table(["contrast", "estimate", "se", "df", "p", "Holm p", "rejected", "note"],
                        [[name, item["estimate"], item["se"], item["df"], item["p_two_sided"], item["holm_p_adjusted"],
                          item["holm_rejected"], item["reason"]] for name, item in late["contrasts"].items()])
    lines += ["## Controls", ""]
    for name, control in value["controls"].items():
        lines += [f"### {name}", "", f"Late-move bets it is computed on: {control['bets']}.", ""]
        lines += _table(["cell", "bets", "race dates"], [[cell, item["bets"], item["race_dates"]]
                                                          for cell, item in control["cells"].items()])
        if "undefined" in control:
            lines += _table(["not defined", "bets"], list(control["undefined"].items()))
        if registered:
            lines += _table(["contrast", "control estimate", "se", "p", "note", "main over the same bets", "main note"],
                            [[key, item["estimate"], item["se"], item["p_two_sided"], item["reason"],
                              control["main_contrasts_same_bets"][key]["estimate"],
                              control["main_contrasts_same_bets"][key]["reason"]]
                             for key, item in control["contrasts"].items()])
    if registered:
        lines += ["## Kill rule and flags (for people to read, never a decision)", ""]
        lines += _table(["contrast", "control", "control estimate", "control p", "main over the same bets", "blocks",
                         "why"],
                        [[name, control, item["control_estimate"], item["control_p_two_sided"],
                          item["main_estimate_same_bets"], item["blocks"], item["why"]]
                         for name, controls in value["kill_rule"].items() for control, item in controls.items()])
        lines += _table(["cell", "cell NCLV at least min", "contrast positive", "Holm", "no control blocks",
                         "tradable_flag"],
                        [[cell, item["cell_nclv_at_least_min"], item["contrast_positive"],
                          item["contrast_rejected_under_holm"], item["no_control_blocks"], item["tradable_flag"]]
                         for cell, item in value["flags"].items()])
    return "\n".join(lines).rstrip("\n") + "\n"


# ---------------------------------------------------------------------------------------------------------------
# The run

def _counts(reg, tally):
    return {"records": tally.records, "replayed_markets": tally.replayed,
            "issued": {str(offset): tally.codes[offset][ISSUED] for offset in reg.decision_offsets_s},
            "eligible": {str(offset): tally.classes[offset][ELIGIBLE] for offset in reg.decision_offsets_s},
            "late_move_population": len(tally.late)}


def _failure(error):
    """A failure code without a market id: "replay:<reason>" for a ReplayProblem, else the error's own code."""
    text = str(error)
    if isinstance(error, ReplayProblem):
        return f"replay:{text.rpartition(':')[2]}"
    return text


def _tally(reg, root, archives, limits):
    """Decide every record of every archive (module docstring)."""
    tally, seen = Tally(reg), {}
    for archive in archives:
        sha = archive["archive_sha256"]
        for record in _records(archive):
            months = _record_months(record, sha)
            if months is None:
                raise RunRefused(f"input:{sha}:malformed_record")
            if any(month_gate(month) for month in months):
                raise RunRefused(f"input:{sha}:stale_gate")
            # Registration data.duplicates: a market of a registered tier is keyed by its id alone, so the same market
            # held as PRO and as ADVANCED refuses rather than being decided and counted twice.
            plan, market_id = record["provenance"]["plan"], record["market_id"]
            key = market_id if plan in reg.tiers else (plan, market_id)
            if key in seen:
                raise RunRefused(f"input:{sha}:{'duplicate_market' if seen[key] == plan else 'market_in_two_tiers'}")
            seen[key] = plan
            try:
                decide_record(reg, record, root, archive["manifest"], tally, limits)
            except (KeyError, TypeError, AttributeError, IndexError, ValueError):
                raise RunRefused(f"input:{sha}:malformed_record") from None
    return tally


def _write(out, files):
    """Write each (name, bytes) into "<out>.partial", a new mode-0700 sibling (files 0600, exclusive create), and
    rename it to out only when every file is whole, so out holds all three files or does not exist. A failure removes
    the partial directory and refuses (out:unwritable); a leftover partial directory refuses before anything is
    written (out:partial_present: remove it by hand); a failure that cannot remove it is out:partial_left, with the
    partial directory named only by the out path the operator gave."""
    partial = out.with_name(out.name + PARTIAL_SUFFIX)
    if partial.exists() or partial.is_symlink():
        _refuse("out:partial_present")
    try:
        partial.mkdir(mode=0o700)
    except OSError:
        _refuse("out:unwritable")
    try:
        partial.chmod(0o700)
        for name, data in files:
            with open(partial / name, "xb") as handle:
                (partial / name).chmod(0o600)
                handle.write(data)
        if out.exists() or out.is_symlink():
            raise FileExistsError(str(out))
        partial.rename(out)
    except OSError:
        try:
            for name, _ in files:
                (partial / name).unlink(missing_ok=True)
            partial.rmdir()
        except OSError:
            _refuse("out:partial_left")
        _refuse("out:unwritable")


def run_study(pass_name, registration_sha256_hex, archive_root, out, code_commit, blind_receipt=None,
              registration_path=None, limits=DEFAULT_LIMITS, clock=utc_now_text):
    """One pass (module docstring); a JSON-ready result: completed with counts and hashes, refused with problems,
    or failed with a failure code."""
    try:
        if pass_name not in PASSES:
            _refuse("pass:not_supported")
        reg = _registration(registration_path, registration_sha256_hex)
        if not (isinstance(code_commit, str) and COMMIT_RE.fullmatch(code_commit)):
            _refuse("code_commit:malformed")
        guarded = _guarded(archive_root, "archive_root")
        root, problems = check_data_root(guarded)
        if problems:
            _refuse(problems[0].replace("data_root:", "archive_root:", 1))
        target = _out_path(out, root)
        if pass_name == BLIND and blind_receipt is not None:
            _refuse("blind_receipt:not_for_the_blind_pass")
        blind = _blind_receipt(blind_receipt, reg) if pass_name == REGISTERED else None
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
    except StudyRefused as error:
        return {"status": "refused", "problems": [str(error)]}
    try:
        tally = _tally(reg, root, archives, limits)
    except (RunRefused, ReplayProblem, StudyRefused) as error:
        return {"status": "failed", "failure": _failure(error)}
    value = summary(reg, tally, pass_name, input_sha256)
    summary_bytes = canonical_json(value) + b"\n"
    markdown_bytes = markdown(value).encode("utf-8")
    counts = _counts(reg, tally)
    outputs = {SUMMARY_JSON: {"sha256": hashlib.sha256(summary_bytes).hexdigest(), "bytes": len(summary_bytes)},
               SUMMARY_MD: {"sha256": hashlib.sha256(markdown_bytes).hexdigest(), "bytes": len(markdown_bytes)}}
    receipt = {"kind": RECEIPT_KIND, "schema_version": SCHEMA_VERSION, "study": STUDY_ID, "pass": pass_name,
               "status": "completed", "registration_sha256": reg.sha256, "input_sha256": input_sha256,
               "input": descriptor, "code_commit": code_commit, "source_descriptor_sha256": sources_sha256,
               "interpreter": "{0} {1}.{2}.{3}".format(sys.implementation.name, *sys.version_info[:3]),
               "blind_receipt_sha256": None if blind is None else blind[1], "counts": counts, "outputs": outputs,
               "recorded_at": clock()}
    receipt_bytes = canonical_json(receipt) + b"\n"
    try:
        target = _out_path(out, root)
        _write(target, [(SUMMARY_JSON, summary_bytes), (SUMMARY_MD, markdown_bytes), (RECEIPT_JSON, receipt_bytes)])
    except StudyRefused as error:
        return {"status": "failed", "failure": str(error)}
    return {"status": "completed", "pass": pass_name, "registration_sha256": reg.sha256,
            "input_sha256": input_sha256, "summary_sha256": outputs[SUMMARY_JSON]["sha256"],
            "receipt_sha256": hashlib.sha256(receipt_bytes).hexdigest(), **counts}


def build_parser():
    parser = argparse.ArgumentParser(prog="python3 -m betangel_research.betfair.backtest.movement_study",
                                     description="Registered measurement movement-v1 on local historical files; "
                                                 "nothing here places an order.")
    parser.add_argument("--pass", dest="pass_name", required=True, choices=PASSES, help="blind first, then registered")
    parser.add_argument("--registration-sha256", required=True, help="sha256 of the registration file's raw bytes")
    parser.add_argument("--archive-root", required=True, help="data root with manifests/, raw/ and index/")
    parser.add_argument("--out", required=True, help="a new directory outside every Git worktree and data tree")
    parser.add_argument("--code-commit", required=True, help="the 40-character commit the code was checked out at")
    parser.add_argument("--blind-receipt", help="registered pass: the blind pass's receipt.json")
    parser.add_argument("--registration", help="registration file (default: research/studies/movement-v1.json)")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    result = run_study(args.pass_name, args.registration_sha256, args.archive_root, args.out, args.code_commit,
                       blind_receipt=args.blind_receipt, registration_path=args.registration)
    sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
    return 0 if result["status"] == "completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
