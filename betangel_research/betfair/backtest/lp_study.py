"""python3 -m betangel_research.betfair.backtest.lp_study: run registered measurement lp-v1.

--pass blind|registered --registration-sha256 HEX --archive-root DIR --out DIR --code-commit HEX
[--blind-receipt PATH] [--registration PATH] runs research/studies/lp-v1.json (or --registration) over every archive
ingested and indexed under the data root DIR and writes summary.json, summary.md (aggregate tables only) and
receipt.json into the new directory --out, through "<out>.partial" (mode 0700, files 0600) renamed only when all
three are whole. It prints one JSON line of counts, codes and hashes and exits 0 (completed) or 2 (refused, or
failed: --out is not created then; out:partial_left alone says a "<out>.partial" directory may be left). It is a
module of its own, not a subcommand of the backtest CLI, because __main__.py is one of the certified engine's sources
and stays byte-identical; this module and lp.py are in the certificate's source descriptor, so adding them changed the
certificate key.

The refusals, the input and the run are movement_study.py's, with its helpers (_out_path, _write, _read_regular,
_guarded, _failure, the Markdown table) and the same rules: the registration file's raw bytes must hash to
--registration-sha256 (both passes) and lp.parse_registration must accept it; --code-commit is 40 lowercase hex
characters; the data root passes path_guard and archive.check_data_root; --out must be new, outside the repository,
every Git worktree, the data root and any licensed-data or log tree's name; the blind pass takes no --blind-receipt
and the registered pass needs the blind pass's canonical, completed receipt of this registration, the same
--code-commit and backtest-source sha256 (blind_receipt:another_code) and the same input sha256. That enforces the
order of the passes on one machine, not that the registered pass runs once: running it once is the operator's rule
(docs/lp-study.md). Every manifest's read limits must be the ones the replay uses, every record's months pass the
current month gate (sealed months are never read), and a market id seen twice in a registered tier fails the run
(duplicate_market).

Each record is decided at every registered offset (movement.market_code); a record that reaches the book predicates
at any offset has its primary member walked once (lp.walk_market, after the member's path is guarded and its manifest
entry checked as replay_view checks it), and the bytes read must hash to the manifest's. An issued decision is
classed by lp.classify; an eligible one is valued (lp.valued, lp.valued_normalised) and its legs' fills, mark-outs,
edges and null moves are kept without any market or selection id; an issued decision of another class keeps only its
legs' fill kinds, counted by class (the exclusions condition on what happened after t, so the blind pass shows what
they remove).

Tables (summary): coverage by registered month as movement-v1's; per decision time, every market code and class
count (the codes add up to the record count), eligible decisions by spread bucket x price band x matched-volume
tercile with the cut points (from statistics.min_bets_for_statistics decisions, to 2 significant figures), per role
(main, join) the fills by side and kind, fill rates, both legs filled, inventory states (main), legs touched before
their fill and legs touched but never filled, fills within each registered latency of t, fills in the first seconds
after a weekday hour beside the count expected if fill times were uniform over their windows, and the seconds to each
fill; the fills of the excluded classes by kind; the selected runners' trd counts over the eligible windows
(lp.TRD_COUNTS), with the rises expected in the after-the-hour seconds if their times were uniform; the commission
rounding effects; net per decision over decisions with a fill, and by inventory state; every mark-out series and
horizon over the main legs (all, by side, by fill kind, touched before their fill) and the join and null legs; the
edge at fill over the main legs' groups only; the realised spread at the secondary's horizon over the main legs; and
the blind minimum detectable effects. Every statistic group gives its count, race dates and undefined reasons, and
from statistics.min_bets_for_statistics values (below them, counts only) its SD with, in the blind pass, the
symmetric nearest-rank ranges q(1 - a) - q(a) (key ranges) and, in the registered pass, the quantiles as they are with
the mean and its CR1 interval; quantiles and ranges are rounded to 4 decimal places. A group over exactly the
decisions or legs of a test (_populations: net over every eligible decision, the mid mark-outs at the secondary's
horizon over all the main or join legs, the realised spread, or any group that happens to hold the same ones) needs
the tests' gates as well, in either pass, and below them reports counts only, reason underpowered as the test's
(_enough). Net by inventory state is counts only in the blind pass, and so are the points of the by-side groups (LAY,
BACK) of mark-outs and edges (_ticks_only). The registered pass adds net per decision over every eligible decision
(the primary), with unrounded commission and with p normalised by the market's book; the tests with Holm and the
realised spread's test (lp.decide); the mark-out at BSP beside the secondary; the controls, with the null control at
BSP; and the flags. A test below its gates, like a group below min_bets_for_statistics, reports counts only. The blind
summary holds no location of net, of a mark-out, of an edge or of the realised spread, no quantile of them but the
symmetric ranges, no statistic of net over every eligible decision or by inventory state but counts, and no points
statistic of a by-side group (tests/test_backtest_lp.py checks every key and the Markdown text). Neither summary holds
a market id, a selection id or a per-market row; the summary has no clock, so the same registration, input and code
give the same bytes.

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
import math
import sys

from .archive import DEFAULT_LIMITS, REPOSITORY_ROOT, ArchiveProblem, HashingReader, check_data_root, month_gate, \
    safe_target, utc_month
from .certificate import CertificateProblem, source_descriptor
from .contract import canonical_json
from .lp import (BACK, BSP_HORIZON, CLASSES, CODES, FILL_KINDS, INVENTORY, JOIN, LAY, MAIN, MAX_REGISTRATION_BYTES,
                 MID_SERIES, ROUNDING, SECONDARY, SERIES, SIDES, STUDY_ID, TRD_COUNTS, UNDEFINED, VOLUME_TERCILES, WIDE,
                 RegistrationError, after_the_hour, after_the_hour_ms, bsp_book, classify, cr1_test, decide, edge,
                 inventory, leg_markouts, null_markouts, parse_registration, registration_sha256, valued,
                 valued_normalised, walk_market)
from .market_index import loads_strict
from .movement import (ELIGIBLE, ISSUED, active_ids, band, cr1_mean, decision_time, hundredths, market_code,
                       nearest_rank, race_date, sample_sd, student_t_quantile)
from .movement_study import (COMMIT_RE, MAX_RECEIPT_BYTES, SHA256_RE, StudyRefused, _failure, _guarded, _out_path,
                             _read_regular, _refuse, _round, _q, _table, _write)
from .replay import ReplayProblem, _member
from .runner import RunRefused, _inputs, _record_months, _records, utc_now_text

SUMMARY_KIND, RECEIPT_KIND, SCHEMA_VERSION = "betangel_lp_summary", "betangel_lp_receipt", 1
BLIND, REGISTERED = "blind", "registered"
PASSES = (BLIND, REGISTERED)
DEFAULT_REGISTRATION = REPOSITORY_ROOT / "research" / "studies" / "lp-v1.json"
SUMMARY_JSON, SUMMARY_MD, RECEIPT_JSON = "summary.json", "summary.md", "receipt.json"
WITHHELD = ("location statistics of net per decision, of every mark-out, of the edge at fill and of the realised "
            "spread, any quantile of them but the symmetric ranges, any statistic of net over every eligible decision "
            "or by inventory state but counts, any points statistic of a by-side group (LAY, BACK: ticks only), tests, "
            "control checks and flags: the registration's blind_pass.withholds")
NO_ANCHOR, OUTSIDE = "no_anchor", "outside_registered_months"
COUNTS_ONLY, UNDERPOWERED = "counts_only", "underpowered"      # the second is lp.cr1_test's, below the tests' gates
TICKS, POINTS = "ticks", "points"
MEDIAN = Fraction(1, 2)
TERCILE_CUTS = (Fraction(1, 3), Fraction(2, 3))
# Per-pass counts of replayed markets (registration diagnostics.rule).
MARKET_DIAGNOSTICS = ("replayed_markets_with_null_ladder_update", "replayed_markets_with_trd_settlement_zeroing",
                      "replayed_markets_with_trd_value_below_last_published")
NOT_COUNTED = ("rises_not_counted_at_touch", "rises_not_counted_displayed", "rises_not_counted_not_displayed")
ROLES = (MAIN, JOIN)
EXCLUDED = tuple(klass for klass in CLASSES if klass != ELIGIBLE)
QUANTILE_PLACES = 4

Decision = namedtuple("Decision", "race_date spread_bucket price_band matched net net_unrounded net_normalised effect "
                                  "state legs trd hour_share")
LegRow = namedtuple("LegRow", "filled kind seconds touched_before_fill touched_never_filled after_the_hour marks edge "
                              "null")


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
    """Everything the summary needs, kept per decision time; no market or selection id leaves it."""

    def __init__(self, registration):
        self.reg = registration
        self.records = self.replayed = 0
        self.diagnostics = dict.fromkeys(MARKET_DIAGNOSTICS, 0)
        offsets = registration.decision_offsets_s
        self.months = {month: {"records": 0, "issued": dict.fromkeys(offsets, 0), "eligible": dict.fromkeys(offsets, 0)}
                       for month in registration.months + (OUTSIDE, NO_ANCHOR)}
        self.codes = {offset: dict.fromkeys(CODES + (ISSUED,), 0) for offset in offsets}
        self.classes = {offset: dict.fromkeys(CLASSES, 0) for offset in offsets}
        self.decisions = {offset: [] for offset in offsets}
        self.excluded = {offset: {klass: {role: dict.fromkeys(FILL_KINDS, 0) for role in ROLES} for klass in EXCLUDED}
                         for offset in offsets}


def _walk(root, manifest, record, reg, offsets, limits):
    """lp.walk_market over the record's primary member, after replay_view's checks of the member, and the member's
    bytes hashed against the manifest (movement_study._replay's checks)."""
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
                walked = walk_market(stream, record, reg, offsets, limits)
            digest = reader.drain()
    except OSError:
        raise ReplayProblem(f"replay:{market_id}:member_unreadable_file") from None
    if digest != (member["sha256"], member["size_bytes"]):
        raise ReplayProblem(f"replay:{market_id}:member_hash_mismatch")
    return walked


def _month(reg, record):
    """The coverage row of a registered-tier record (movement_study._month's rule), or None for another plan."""
    if record["provenance"]["plan"] not in reg.tiers:
        return None
    first = record["decision_time"]["first_published_market_time"]
    month = None if first is None else utc_month(first["value"])
    if month is None:
        return NO_ANCHOR
    return month if reg.start_month <= month <= reg.end_month else OUTSIDE


def _leg_row(reg, walk, leg, bsp):
    touched = leg.touched_pt is not None
    if leg.fill_pt is None:
        return LegRow(filled=False, kind=None, seconds=None, touched_before_fill=False, touched_never_filled=touched,
                      after_the_hour=False, marks=None, edge=None, null=None)
    return LegRow(filled=True, kind=leg.fill_kind, seconds=(leg.fill_pt - leg.placed) / 1000,
                  touched_before_fill=touched and leg.touched_pt < leg.fill_pt, touched_never_filled=False,
                  after_the_hour=after_the_hour(leg.fill_pt, reg.after_the_hour_s),
                  marks=leg_markouts(reg, walk, leg, bsp), edge=edge(reg, leg),
                  null=null_markouts(reg, walk, leg, bsp) if leg.role == MAIN else None)


def decide_record(reg, record, root, manifest, tally, limits=DEFAULT_LIMITS):
    """Decide one index record at every registered offset and add it to the tally (module docstring)."""
    tally.records += 1
    month = _month(reg, record)
    if month is not None:
        tally.months[month]["records"] += 1
    codes = {offset: market_code(reg, record, offset) for offset in reg.decision_offsets_s}
    walked = [offset for offset in reg.decision_offsets_s if codes[offset] is None]
    walk = cancel = None
    if walked:                                       # only a record with an anchor reaches the book predicates
        walk = _walk(root, manifest, record, reg, walked, limits)
        tally.replayed += 1
        for key, count in zip(MARKET_DIAGNOSTICS, (walk.null_ladder_updates, walk.trd_zeroing, walk.trd_decreases)):
            tally.diagnostics[key] += bool(count)
        cancel = decision_time(record, 0) + reg.cancel_offset_s * 1000
    for offset in reg.decision_offsets_s:
        code, selection, t = codes[offset], None, decision_time(record, offset)
        if code is None:
            code, selection = walk.selections[offset]
        if code is not None:
            tally.codes[offset][code] += 1
            continue
        tally.codes[offset][ISSUED] += 1
        tally.months[month]["issued"][offset] += 1
        klass, bsp = classify(record, walk, t, cancel, selection.selection_id, active_ids(record, t))
        tally.classes[offset][klass] += 1
        legs = {(leg.role, leg.side): leg for leg in walk.legs if leg.offset_s == offset}
        if klass != ELIGIBLE:
            for leg in legs.values():
                if leg.fill_pt is not None:
                    tally.excluded[offset][klass][leg.role][leg.fill_kind] += 1
            continue
        tally.months[month]["eligible"][offset] += 1
        lay, back = legs[MAIN, LAY], legs[MAIN, BACK]
        prices = (lay.price if lay.fill_pt is not None else None, back.price if back.fill_pt is not None else None)
        net, unrounded, effect = valued(reg, *prices, bsp)
        tally.decisions[offset].append(Decision(
            race_date=race_date(record), spread_bucket=band(reg.spread_buckets, selection.spread),
            price_band=band(reg.price_bands, hundredths(selection.back[0])),
            matched=float(walk.matched[offset] / reg.divisor), net=float(net), net_unrounded=float(unrounded),
            net_normalised=float(valued_normalised(reg, *prices, bsp, bsp_book(record))),
            effect=effect, state=inventory(lay.fill_pt is not None, back.fill_pt is not None),
            legs={key: _leg_row(reg, walk, leg, bsp) for key, leg in legs.items()}, trd=dict(walk.trd[offset]),
            hour_share=after_the_hour_ms(t, cancel, reg.after_the_hour_s) / (cancel - t)))


# ---------------------------------------------------------------------------------------------------------------
# Tables

def _rounded(result):
    return {key: _round(value) if isinstance(value, float) else value for key, value in result.items()}


def _significant(value, digits):
    """value rounded to `digits` significant figures."""
    return float(format(value, f".{digits}g"))


def _dates(rows):
    return len({day for day, _ in rows})


def _counted(rows):
    """{values, race_dates}: a group that releases its counts only."""
    return {"values": len(rows), "race_dates": _dates(rows)}


def _enough(reg, rows, tested):
    """Whether a group of [(race date, x)] rows gives any statistic: from min_bets_for_statistics values and, for a
    group over exactly the decisions or legs of a test (tested, _populations), from the tests' gates as well, so that
    no descriptive copy of a test gives what the test does not (registration statistics.rule)."""
    if len(rows) < reg.min_bets_for_statistics:
        return False
    return not tested or (len(rows) >= reg.min_tested_bets and _dates(rows) >= reg.min_tested_race_dates)


def _identities(items):
    """The decisions or legs a group is computed over, by identity: two groups hold the very same ones exactly when
    these are equal."""
    return frozenset(map(id, items))


def _populations(reg, decisions):
    """The decisions and legs each test of the registered pass is computed over (_registered), by identity: every
    eligible decision (the primary), the main legs with a mid mark-out at the secondary's horizon (the secondary),
    those with an edge as well (the realised spread: the same legs, since a mark-out needs the start mid the edge
    needs), the join legs with that mark-out (the positive control) and the main legs with it and a null move there
    (the null control)."""
    key = (SECONDARY, reg.secondary_horizon_s)
    main = [row for _, row in _filled_legs(decisions, MAIN) if isinstance(row.marks[key], float)]
    join = [row for _, row in _filled_legs(decisions, JOIN) if isinstance(row.marks[key], float)]
    return {_identities(decisions), _identities(main), _identities(row for row in main if isinstance(row.edge, dict)),
            _identities(join), _identities(row for row in main if isinstance(row.null[key], float))}


def _ticks_only(registered, group):
    """Whether a group gives its points counts only: a by-side group (LAY, BACK) in the blind pass. Points per tick
    fall as the price lengthens, and a shortening hurts a LAY while a lengthening hurts a BACK, so the LAY group's
    points SD over its ticks SD, beside the BACK group's, tracks the sign of the mark-out (registration
    blind_pass.rule); a group pooling both sides keeps both units."""
    return not registered and group in SIDES


def _described(reg, rows, tested=False):
    """cr1_mean of [(race date, float)] rows, every float rounded, from min_bets_for_statistics values and, over
    exactly a test's decisions or legs (tested), the tests' gates (_enough); below them its counts only (no mean, SE or
    interval), reason counts_only, or underpowered as the test's. Descriptive: it decides nothing."""
    if _enough(reg, rows, tested):
        return _rounded(cr1_mean(rows, reg.confidence_level, reg.min_clusters))
    return dict(bets=len(rows), race_dates=_dates(rows), mean=None, se=None, df=None, t_critical=None, ci_low=None,
                ci_high=None, reason=UNDERPOWERED if tested else COUNTS_ONLY)


def _ranges(reg, values):
    """{"q(1 - a)-q(a)": that difference} for each registered quantile a below the median, widest first: a shift
    leaves it as it is and so, but for nearest-rank ties, does a change of sign, so it keeps a group's spread but
    neither its location nor its direction."""
    found = nearest_rank(values, reg.quantiles)
    return {f"{_q(1 - q)}-{_q(q)}": _round(found[1 - q] - found[q], QUANTILE_PLACES)
            for q in reg.quantiles if q < MEDIAN}


def _stats(reg, rows, registered, undefined=None, tested=False, counts_only=False):
    """{values, race_dates[, undefined], sd, ranges or quantiles[, mean]} of [(race date, float)] rows; statistics
    only from min_bets_for_statistics values and, over exactly a test's decisions or legs (tested), from the tests'
    gates as well (_enough). The blind pass gives the SD and the symmetric ranges (_ranges) only, no quantile less its
    median: an atom at the median (a mark-out of exactly 0) would make those the raw quantiles. The registered pass
    gives the quantiles as they are and the mean with its CR1 interval (_described, which says why a group has none).
    With counts_only, {values, race_dates[, undefined]} alone (_ticks_only)."""
    result = _counted(rows)
    if undefined is not None:
        result["undefined"] = undefined
    if counts_only:
        return result
    key = "quantiles" if registered else "ranges"
    result.update(sd=None, **{key: None})
    if registered:
        result["mean"] = _described(reg, rows, tested)
    values = [x for _, x in rows]
    if _enough(reg, rows, tested):
        result["sd"] = _round(sample_sd(values))
        result[key] = {_q(q): _round(value, QUANTILE_PLACES) for q, value in
                       nearest_rank(values, reg.quantiles).items()} if registered else _ranges(reg, values)
    return result


def _rate(count, total):
    return None if not total else _round(count / total)


def _seconds(reg, values):
    """{legs, seconds_quantiles}: the seconds from t to each fill, as they are in both passes (not an outcome)."""
    if len(values) < reg.min_bets_for_statistics:
        return {"legs": len(values), "seconds_quantiles": None}
    return {"legs": len(values), "seconds_quantiles": {_q(q): _round(value, QUANTILE_PLACES)
                                                       for q, value in nearest_rank(values, reg.quantiles).items()}}


def _strata(reg, decisions):
    """Eligible decisions by spread bucket x price band x matched-volume tercile, with the cut points: released from
    min_bets_for_statistics decisions, rounded to 2 significant figures (the cells use them unrounded)."""
    values = [decision.matched for decision in decisions]
    cuts = None if not values else [nearest_rank(values, [q])[q] for q in TERCILE_CUTS]

    def tercile(value):
        return VOLUME_TERCILES[0] if value <= cuts[0] else VOLUME_TERCILES[1] if value <= cuts[1] \
            else VOLUME_TERCILES[2]

    cells = {s: {p: dict.fromkeys(VOLUME_TERCILES, 0) for p, _, _ in reg.price_bands}
             for s, _, _ in reg.spread_buckets}
    for decision in decisions:
        cells[decision.spread_bucket][decision.price_band][tercile(decision.matched)] += 1
    released = None if len(values) < reg.min_bets_for_statistics else [_significant(cut, 2) for cut in cuts]
    return {"matched_volume_cut_points": released, "cells": cells}


def _fills(reg, decisions, role):
    """One role's fill counts (registration blind_pass.releases and diagnostics.rule)."""
    total = len(decisions)
    out = {"eligible": total, "legs": {}}
    for side in SIDES:
        rows = [decision.legs[role, side] for decision in decisions]
        filled = [row for row in rows if row.filled]
        out["legs"][side] = {"filled": len(filled), "fill_rate": _rate(len(filled), total),
                             "by_kind": {kind: sum(1 for row in filled if row.kind == kind) for kind in FILL_KINDS},
                             "touched_before_fill": sum(1 for row in rows if row.touched_before_fill),
                             "touched_never_filled": sum(1 for row in rows if row.touched_never_filled),
                             "seconds_to_fill": _seconds(reg, [row.seconds for row in filled])}
    both = sum(1 for decision in decisions if decision.legs[role, LAY].filled and decision.legs[role, BACK].filled)
    out["both_filled"], out["both_fill_rate"] = both, _rate(both, total)
    if role == MAIN:
        out["inventory"] = {state: sum(1 for decision in decisions if decision.state == state) for state in INVENTORY}
    filled = [(decision, decision.legs[role, side]) for decision in decisions for side in SIDES
              if decision.legs[role, side].filled]
    out["within_s_of_t"] = {str(seconds): {kind: sum(1 for _, row in filled if row.kind == kind
                                                     and row.seconds <= seconds) for kind in FILL_KINDS}
                            for seconds in reg.latency_s}
    out["after_the_hour"] = {kind: {"fills": sum(1 for _, row in filled if row.kind == kind and row.after_the_hour),
                                    "expected_if_uniform": _round(math.fsum(decision.hour_share for decision, row
                                                                            in filled if row.kind == kind))}
                             for kind in FILL_KINDS}
    return out


def _trd_expected(decisions):
    """The rises counted and not counted that would fall in the after-the-hour seconds if each rise's time were
    uniform over its decision's window (registration diagnostics.rule), beside the counts of those that did."""
    return {"rises_counted": _round(math.fsum(d.trd["rises_counted"] * d.hour_share for d in decisions)),
            "rises_not_counted": _round(math.fsum(sum(d.trd[name] for name in NOT_COUNTED) * d.hour_share
                                                  for d in decisions))}


def _net(reg, decisions, registered):
    """Net per decision over the decisions with a fill, and by inventory state (counts only in the blind pass: with
    the counts, the states' SDs would give the dispersion between their means, and a round trip's net is a known
    positive function of its prices); in the registered pass also over every eligible decision, with rounded and
    unrounded commission and with p normalised by the market's book. A group of every eligible decision is the
    primary's, so it needs the tests' gates (_enough)."""
    primary = _identities(decisions)

    def stats(rows, attr="net"):
        return _stats(reg, [(d.race_date, getattr(d, attr)) for d in rows], registered,
                      tested=_identities(rows) == primary)

    groups = {"filled": [d for d in decisions if d.state != INVENTORY[0]]}
    groups.update({state: [d for d in decisions if d.state == state] for state in INVENTORY[1:]})
    out = {name: stats(rows) if registered or name == "filled" else _counted([(d.race_date, d.net) for d in rows])
           for name, rows in groups.items()}
    if registered:
        for name, attr in (("all_eligible", "net"), ("all_eligible_unrounded_commission", "net_unrounded"),
                           ("all_eligible_bsp_normalised", "net_normalised")):
            out[name] = stats(decisions, attr)
    return out


def _filled_legs(decisions, role):
    """[(race date, LegRow)] of every filled leg of one role, LAY then BACK within each decision."""
    return [(decision.race_date, decision.legs[role, side]) for decision in decisions for side in SIDES
            if decision.legs[role, side].filled]


def _defined(rows, attr, key):
    """[(race date, value)] of the [(race date, LegRow)] rows whose attr (marks or null) holds a value at key."""
    return [(day, getattr(row, attr)[key]) for day, row in rows if isinstance(getattr(row, attr)[key], float)]


def _realised(reg, rows):
    """[(race date, edge less the mid's move against us at the secondary's horizon, in points)] of the filled legs
    with both defined (registration registered_pass.realised_spread)."""
    key = (SECONDARY, reg.secondary_horizon_s)
    return [(day, row.edge["points"] - row.marks[key]) for day, row in rows
            if isinstance(row.edge, dict) and isinstance(row.marks[key], float)]


def _series(reg, rows, attr, registered, series, tested, ticks_only=False):
    """{series: {horizon: stats with undefined reasons}} over [(race date, LegRow)] rows, reading the LegRow's attr
    (marks or null); tested holds the tests' populations (_populations), and with ticks_only the points series give
    their counts only (_ticks_only)."""
    out = {}
    for name in series:
        horizons = reg.horizons if name in MID_SERIES else reg.horizons_s
        out[name] = {}
        for horizon in horizons:
            values = [(day, row, getattr(row, attr)[name, horizon]) for day, row in rows]
            reasons = {reason: sum(1 for _, _, value in values if value == reason) for reason in UNDEFINED}
            defined = [(day, row, value) for day, row, value in values if isinstance(value, float)]
            out[name][str(horizon)] = _stats(reg, [(day, value) for day, _, value in defined], registered,
                                             undefined=reasons,
                                             tested=_identities(row for _, row, _ in defined) in tested,
                                             counts_only=ticks_only and name.endswith(POINTS))
    return out


def _main_groups(decisions):
    """{group: [(race date, LegRow)]} of the main legs that filled: all, by side, by fill kind, and those touched
    before their fill (whose mark-out starts after part of the move)."""
    legs = [(decision.race_date, side, decision.legs[MAIN, side]) for decision in decisions for side in SIDES
            if decision.legs[MAIN, side].filled]
    groups = {"all": [(day, row) for day, _, row in legs]}
    groups.update({side: [(day, row) for day, s, row in legs if s == side] for side in SIDES})
    groups.update({kind: [(day, row) for day, _, row in legs if row.kind == kind] for kind in FILL_KINDS})
    groups["touched_before_fill"] = [(day, row) for day, _, row in legs if row.touched_before_fill]
    return groups


def _markouts(reg, decisions, registered):
    groups, tested = _main_groups(decisions), _populations(reg, decisions)
    return {"main": {name: _series(reg, rows, "marks", registered, SERIES, tested, _ticks_only(registered, name))
                     for name, rows in groups.items()},
            "join": {"all": _series(reg, _filled_legs(decisions, JOIN), "marks", registered, SERIES, tested)},
            "null": {"all": _series(reg, groups["all"], "null", registered, MID_SERIES, tested)}}


def _edges(reg, decisions, registered):
    """The edge at fill over the main legs' groups (_main_groups) only: no join leg's edge enters a table."""
    out, tested = {}, _populations(reg, decisions)
    for name, rows in _main_groups(decisions).items():
        defined = [(day, row) for day, row in rows if isinstance(row.edge, dict)]
        reasons = {reason: sum(1 for _, row in rows if row.edge == reason) for reason in (UNDEFINED[0], WIDE)}
        same = _identities(row for _, row in defined) in tested
        out[name] = {unit: _stats(reg, [(day, row.edge[unit]) for day, row in defined], registered, undefined=reasons,
                                  tested=same, counts_only=unit == POINTS and _ticks_only(registered, name))
                     for unit in (TICKS, POINTS)}
    return out


def _realised_spread(reg, decisions, registered):
    """The realised spread at the secondary's horizon over the filled main legs (registration
    registered_pass.realised_spread), undefined where their mark-out is, for the same reasons; its values are its
    test's, so it needs the tests' gates (_enough)."""
    rows = _filled_legs(decisions, MAIN)
    key = (SECONDARY, reg.secondary_horizon_s)
    reasons = {reason: sum(1 for _, row in rows if row.marks[key] == reason) for reason in UNDEFINED}
    return {"all": _stats(reg, _realised(reg, rows), registered, undefined=reasons, tested=True)}


def _detectable(reg, sd, values, days):
    """The minimum detectable effect of a mean of `values` values with per-value SD sd on `days` race dates
    (registration power_note): Holm's first step over the decision times, statistics.power, independence."""
    families = len(reg.decision_offsets_s)
    factor = student_t_quantile(float(1 - reg.holm_alpha / (2 * families)), days - 1) + \
        student_t_quantile(float(reg.power), days - 1)
    return factor * sd / math.sqrt(values)


def _power(reg, decisions):
    """The blind minimum detectable effects of the primary and the secondary (registration power_note), from the
    released SDs and counts alone: the SD of net over the decisions with a fill and that of the secondary's own values
    (the mid mark-out in points over every filled main leg with one, both sides pooled); None where that SD is not
    released (_enough: the secondary's needs the tests' gates) or on fewer than two race dates."""
    filled = [d for d in decisions if d.state != INVENTORY[0]]
    nets = [(d.race_date, d.net) for d in filled]
    days = len({d.race_date for d in decisions})
    primary = None
    if _enough(reg, nets, _identities(filled) == _identities(decisions)) and days >= 2:
        primary = _detectable(reg, math.sqrt(len(filled) / len(decisions)) * sample_sd([x for _, x in nets]),
                              len(decisions), days)
    marks = _defined(_filled_legs(decisions, MAIN), "marks", (SECONDARY, reg.secondary_horizon_s))
    secondary = None
    if _enough(reg, marks, True) and _dates(marks) >= 2:
        secondary = _detectable(reg, sample_sd([x for _, x in marks]), len(marks), _dates(marks))
    return {"primary": {"decisions": len(decisions), "with_a_fill": len(filled),
                        "minimum_detectable_gbp_per_decision": _round(primary)},
            "secondary": {"legs": len(marks), "minimum_detectable_points": _round(secondary)}}


def _decision_tables(reg, tally, offset, registered):
    decisions = tally.decisions[offset]
    return {
        "codes": dict(tally.codes[offset]),
        "classes": dict(tally.classes[offset]),
        "strata": _strata(reg, decisions),
        "fills": {role: _fills(reg, decisions, role) for role in ROLES},
        "excluded_fills": {klass: {role: dict(kinds) for role, kinds in roles.items()}
                           for klass, roles in tally.excluded[offset].items()},
        "trd_changes": {name: sum(decision.trd[name] for decision in decisions) for name in TRD_COUNTS},
        "trd_after_the_hour_expected_if_uniform": _trd_expected(decisions),
        "commission_rounding": {effect: sum(1 for d in decisions if d.effect == effect) for effect in ROUNDING},
        "net": _net(reg, decisions, registered),
        "markouts": _markouts(reg, decisions, registered),
        "edge_at_fill": _edges(reg, decisions, registered),
        "realised_spread": _realised_spread(reg, decisions, registered),
        "power": _power(reg, decisions),
    }


def _registered(reg, tally):
    """{tests, descriptive, controls, flags} of the registered pass (lp.decide), every float rounded; descriptive and
    the controls' at_bsp decide nothing (over exactly a test's legs, they need its gates: _enough)."""
    key, at_bsp = (SECONDARY, reg.secondary_horizon_s), (SECONDARY, BSP_HORIZON)
    primary, secondary, realised, join, null, null_main, bsp, null_bsp = {}, {}, {}, {}, {}, {}, {}, {}
    for offset in reg.decision_offsets_s:
        decisions = tally.decisions[offset]
        tested = _populations(reg, decisions)
        primary[offset] = cr1_test(reg, [(d.race_date, d.net) for d in decisions])
        main = _filled_legs(decisions, MAIN)
        secondary[offset] = cr1_test(reg, _defined(main, "marks", key))
        realised[offset] = cr1_test(reg, _realised(reg, main))
        join[offset] = cr1_test(reg, _defined(_filled_legs(decisions, JOIN), "marks", key))
        pairs = [(day, row.null[key], row.marks[key]) for day, row in main
                 if isinstance(row.null[key], float) and isinstance(row.marks[key], float)]
        null[offset] = cr1_test(reg, [(day, value) for day, value, _ in pairs])
        null_main[offset] = cr1_test(reg, [(day, value) for day, _, value in pairs])
        legs = [(day, row) for day, row in main if isinstance(row.marks[at_bsp], float)]
        bsp[offset] = _described(reg, [(day, row.marks[at_bsp]) for day, row in legs],
                                 _identities(row for _, row in legs) in tested)
        legs = [(day, row) for day, row in legs if isinstance(row.null[at_bsp], float)]
        same = _identities(row for _, row in legs) in tested
        null_bsp[offset] = {"null": _described(reg, [(day, row.null[at_bsp]) for day, row in legs], same),
                            "main_same_legs": _described(reg, [(day, row.marks[at_bsp]) for day, row in legs], same)}
    tests, controls, flags = decide(reg, primary, secondary, realised, join, null, null_main)
    tests = {family: {str(o): _rounded(item) for o, item in results.items()} for family, results in tests.items()}
    controls = {"join": {str(o): _rounded(item) for o, item in controls["join"].items()},
                "null": {str(o): {"null": _rounded(item["null"]), "main_same_legs": _rounded(item["main_same_legs"]),
                                  "blocks": item["blocks"], "why": item["why"], "at_bsp": null_bsp[o]}
                         for o, item in controls["null"].items()}}
    return {"tests": tests, "descriptive": {"markout_at_bsp": {str(o): item for o, item in bsp.items()}},
            "controls": controls, "flags": {str(o): item for o, item in flags.items()}}


def _coverage(tally):
    def by_offset(counts):
        return {str(offset): count for offset, count in counts.items()}

    return {month: {"records": row["records"], "issued": by_offset(row["issued"]),
                    "eligible": by_offset(row["eligible"])} for month, row in tally.months.items()}


def summary(reg, tally, pass_name, input_sha256):
    """The summary of one pass (module docstring); the blind pass's holds no location statistic of an outcome."""
    registered = pass_name == REGISTERED
    value = {"kind": SUMMARY_KIND, "schema_version": SCHEMA_VERSION, "study": STUDY_ID, "pass": pass_name,
             "registration_sha256": reg.sha256, "input_sha256": input_sha256, "records": tally.records,
             "replayed_markets": tally.replayed, "diagnostics": dict(tally.diagnostics),
             "coverage_by_month": _coverage(tally),
             "decision_times": {str(offset): _decision_tables(reg, tally, offset, registered)
                                for offset in reg.decision_offsets_s}}
    if registered:
        value.update(_registered(reg, tally))
    else:
        value["withheld"] = WITHHELD
    return value


# ---------------------------------------------------------------------------------------------------------------
# Markdown

def _stat_header(registered):
    return ["group", "values", "race dates", "SD", *(["mean", "CI low", "CI high", "note"] if registered else [])]


def _stat_row(name, group, registered):
    row = [name, group["values"], group["race_dates"], group.get("sd")]
    if registered:
        mean = group.get("mean") or {}
        row += [mean.get("mean"), mean.get("ci_low"), mean.get("ci_high"), mean.get("reason")]
    return row


def _quantile_lines(title, groups, registered):
    """A table of the groups' quantiles as they are (registered) or their symmetric ranges (blind)."""
    key = "quantiles" if registered else "ranges"
    keys = next((list(group[key]) for _, group in groups if group.get(key)), [])
    lines = [f"{title} ({'nearest-rank quantiles' if registered else 'nearest-rank ranges q(1 - a) - q(a)'})", ""]
    return lines + _table(["group", "values", *keys],
                          [[name, group["values"], *((group.get(key) or {}).get(q) for q in keys)]
                           for name, group in groups])


def _markout_lines(title, tables, registered):
    """Rows group x series x horizon: counts, undefined reasons, SD (and the mean), then the quantiles or ranges."""
    rows, quantiles = [], []
    for group, series in tables.items():
        for name, horizons in series.items():
            for horizon, stats in horizons.items():
                label = f"{group} {name} {horizon if horizon == BSP_HORIZON else '+' + horizon + ' s'}"
                rows.append(_stat_row(label, stats, registered) + [_undefined(stats)])
                quantiles.append((label, stats))
    lines = [f"#### {title}", ""]
    lines += _table(_stat_header(registered) + ["undefined"], rows)
    return lines + _quantile_lines(f"{title}, quantiles", quantiles, registered)


def _undefined(stats):
    return ", ".join(f"{reason} {count}" for reason, count in stats["undefined"].items() if count) or "-"


def _mean_rows(results):
    return [[f"off {o} s", r["bets"], r["race_dates"], r["mean"], r["ci_low"], r["ci_high"], r["reason"]]
            for o, r in results.items()]


def _registered_lines(reg, value):
    """The registered pass's tests, descriptive rows beside them, controls and flags."""
    lines = ["## Tests (CR1 by race date, t(G - 1), Holm per family)", ""]
    for family in ("primary", "secondary"):
        lines += _table([family, "values", "race dates", "mean", "CI low", "CI high", "p", "Holm p", "rejected",
                         "note"],
                        [[f"off {o} s", r["bets"], r["race_dates"], r["mean"], r["ci_low"], r["ci_high"],
                          r["p_two_sided"], r["holm_p_adjusted"], r["holm_rejected"], r["reason"]]
                         for o, r in value["tests"][family].items()])
    level = format(float(reg.confidence_level * 100), "g")
    lines += ["The secondary's CI high is its CR1 interval's upper bound: not detected means only that the mean move "
              f"against us is plausibly below that bound ({level}%, unadjusted); it never supports story (a) on its "
              "own.", ""]
    lines += _table(["decision time", "adverse_selection_detected", "CI high (points)"],
                    [[f"off {o} s", r["adverse_selection_detected"], r["ci_high"]]
                     for o, r in value["tests"]["secondary"].items()])
    lines += ["Mark-out at BSP beside the secondary (mid, points; descriptive, not under Holm, never deciding)", ""]
    lines += _table(["at BSP", "values", "race dates", "mean", "CI low", "CI high", "note"],
                    _mean_rows(value["descriptive"]["markout_at_bsp"]))
    lines += ["Realised spread at the secondary's horizon: the edge at fill less the mid's move against us (points; "
              "decides the flag, unadjusted, not under Holm)", ""]
    lines += _table(["realised spread", "values", "race dates", "mean", "CI low", "CI high", "p", "note",
                     "not significantly negative"],
                    [[f"off {o} s", r["bets"], r["race_dates"], r["mean"], r["ci_low"], r["ci_high"],
                      r["p_two_sided"], r["reason"], r["not_significantly_negative"]]
                     for o, r in value["tests"]["realised_spread"].items()])
    lines += ["## Controls", ""]
    lines += _table(["join", "values", "mean", "p", "note", "detects adverse selection"],
                    [[f"off {o} s", r["bets"], r["mean"], r["p_two_sided"], r["reason"],
                      r["detects_adverse_selection"]] for o, r in value["controls"]["join"].items()])
    lines += _table(["null", "values", "null mean", "null p", "main over the same legs", "blocks", "why"],
                    [[f"off {o} s", r["null"]["bets"], r["null"]["mean"], r["null"]["p_two_sided"],
                      r["main_same_legs"]["mean"], r["blocks"], r["why"]]
                     for o, r in value["controls"]["null"].items()])
    lines += ["Null control at BSP, beside the main mark-out at BSP over the same legs (descriptive, never deciding)",
              ""]
    lines += _table(["null at BSP", "values", "null mean", "CI low", "CI high", "main over the same legs", "CI low",
                     "CI high", "note"],
                    [[f"off {o} s", r["at_bsp"]["null"]["bets"], r["at_bsp"]["null"]["mean"],
                      r["at_bsp"]["null"]["ci_low"], r["at_bsp"]["null"]["ci_high"],
                      r["at_bsp"]["main_same_legs"]["mean"], r["at_bsp"]["main_same_legs"]["ci_low"],
                      r["at_bsp"]["main_same_legs"]["ci_high"], r["at_bsp"]["null"]["reason"]]
                     for o, r in value["controls"]["null"].items()])
    lines += ["## Flags (for people to read, never a decision)", "",
              "A flag without adverse_selection_detected fits story (a), the spread is rent; a flag with it is the "
              "fourth reading, picked off but paid for it (docs/lp-study.md).", ""]
    checks = list(next(iter(value["flags"].values())))
    lines += _table(["decision time", *checks, "adverse_selection_detected"],
                    [[f"off {o} s", *item.values(), value["tests"]["secondary"][o]["adverse_selection_detected"]]
                     for o, item in value["flags"].items()])
    return lines


def markdown(reg, value):
    """summary.md: the summary's tables as Markdown, with the gates and the registered pass's reading notes."""
    registered = value["pass"] == REGISTERED
    lines = [f"# LP study {value['study']}: {value['pass']} pass", "",
             f"- registration sha256: `{value['registration_sha256']}`", f"- input sha256: `{value['input_sha256']}`",
             f"- index records: {value['records']}; markets replayed: {value['replayed_markets']}", ""]
    lines += _table(["diagnostic", "markets"], list(value["diagnostics"].items()))
    if not registered:
        lines += [f"Withheld in this pass: {value['withheld']}.", ""]
    lines += [f"Counts only below the gates: a group of net, mark-outs, edges or realised spreads gives statistics "
              f"from {reg.min_bets_for_statistics} values, and one over exactly a test's decisions or legs (net over "
              f"every eligible decision, the mid mark-outs at +{reg.secondary_horizon_s} s over all the main or join "
              f"legs, the realised spread) also needs the tests' {reg.min_tested_bets} values on "
              f"{reg.min_tested_race_dates} race dates (registration statistics.rule).", ""]
    offsets = list(value["decision_times"])
    lines += ["## Coverage by month (records of a registered tier; issued / eligible by decision time)", ""]
    lines += _table(["month", "records", *(f"off {offset} s" for offset in offsets)],
                    [[month, row["records"], *(f"{row['issued'][offset]} / {row['eligible'][offset]}"
                                               for offset in offsets)]
                     for month, row in value["coverage_by_month"].items()])
    for offset, tables in value["decision_times"].items():
        lines += [f"## Decision at off {offset} s", "", "### Codes", ""]
        lines += _table(["code", "records"], [[code, count] for code, count in tables["codes"].items() if count])
        lines += ["### Classes of issued decisions", ""]
        lines += _table(["class", "decisions"], list(tables["classes"].items()))
        strata = tables["strata"]
        lines += ["### Eligible decisions by spread bucket x price band x matched-volume tercile", "",
                  f"Matched-volume cut points (GBP of backer's stake, 2 significant figures): "
                  f"{strata['matched_volume_cut_points']}.", ""]
        lines += _table(["spread", "price", *VOLUME_TERCILES],
                        [[spread, price, *cells.values()] for spread, row in strata["cells"].items()
                         for price, cells in row.items()])
        lines += ["### Fills", ""]
        fill_rows = []
        for role, fills in tables["fills"].items():
            for side, leg in fills["legs"].items():
                fill_rows.append([role, side, fills["eligible"], leg["filled"], leg["fill_rate"],
                                  *leg["by_kind"].values(), leg["touched_before_fill"], leg["touched_never_filled"]])
            fill_rows.append([role, "both", fills["eligible"], fills["both_filled"], fills["both_fill_rate"],
                              *["-"] * (len(FILL_KINDS) + 2)])
        lines += _table(["role", "leg", "eligible", "filled", "rate", *FILL_KINDS, "touched_before_fill",
                         "touched_never_filled"], fill_rows)
        lines += _table(["inventory state", "decisions"], list(tables["fills"][MAIN]["inventory"].items()))
        lines += ["Fills by kind soon after t (a slower connection could change them) and in the first seconds after "
                  "a weekday hour (when Betfair's currency rates update), with the count expected if fill times "
                  "were uniform over their windows", ""]
        timing_rows = []
        for role, fills in tables["fills"].items():
            for seconds, kinds in fills["within_s_of_t"].items():
                timing_rows.append([role, f"within {seconds} s of t", *kinds.values()])
            timing_rows.append([role, "after the hour", *(item["fills"] for item in fills["after_the_hour"].values())])
            timing_rows.append([role, "after the hour, expected if uniform",
                                *(item["expected_if_uniform"] for item in fills["after_the_hour"].values())])
        lines += _table(["role", "fills", *FILL_KINDS], timing_rows)
        lines += ["Fills of the excluded decisions, by class (counts only; a class with none is left out here, not in "
                  "summary.json)", ""]
        lines += _table(["class", "role", *FILL_KINDS],
                        [[klass, role, *kinds.values()] for klass, roles in tables["excluded_fills"].items()
                         for role, kinds in roles.items() if any(kinds.values())])
        seconds = [(f"{role} {side}", leg["seconds_to_fill"]) for role, fills in tables["fills"].items()
                   for side, leg in fills["legs"].items()]
        keys = next((list(item["seconds_quantiles"]) for _, item in seconds if item["seconds_quantiles"]), [])
        lines += ["Seconds from t to each fill (nearest-rank quantiles)", ""]
        lines += _table(["legs", "filled", *keys], [[name, item["legs"], *((item["seconds_quantiles"] or {}).get(q)
                                                                            for q in keys)] for name, item in seconds])
        lines += ["### Traded-volume changes of the selected runners over the eligible windows", ""]
        lines += _table(["count", "changes"], list(tables["trd_changes"].items()))
        expected = tables["trd_after_the_hour_expected_if_uniform"]
        lines += ["Rises expected in the after-the-hour seconds if each rise's time were uniform over its window: "
                  f"counted {expected['rises_counted']}, not counted {expected['rises_not_counted']}.", ""]
        lines += ["### Commission rounding effects", ""]
        lines += _table(["effect", "decisions"], list(tables["commission_rounding"].items()))
        lines += ["### Net per decision (GBP, at BSP, after rounded commission)", ""]
        net = list(tables["net"].items())
        lines += _table(_stat_header(registered), [_stat_row(name, group, registered) for name, group in net])
        lines += _quantile_lines("Net per decision", net, registered)
        lines += ["### Mark-outs: the move against us after a fill (ticks; points = 100 / price)", ""]
        for name, title in (("main", "Main legs"), ("join", "Positive control: joining the best prices"),
                            ("null", "Null control: random times in the same windows")):
            lines += _markout_lines(title, tables["markouts"][name], registered)
        lines += ["### Edge at fill: our price over the start mid, in our favour", ""]
        edges = [(f"{group} {unit}", stats) for group, units in tables["edge_at_fill"].items()
                 for unit, stats in units.items()]
        lines += _table(_stat_header(registered) + ["undefined"],
                        [_stat_row(name, stats, registered) + [_undefined(stats)] for name, stats in edges])
        lines += _quantile_lines("Edge at fill", edges, registered)
        realised = list(tables["realised_spread"].items())
        lines += ["### Realised spread at the secondary's horizon: edge at fill less the mid's move against us "
                  "(points)", ""]
        lines += _table(_stat_header(registered) + ["undefined"],
                        [_stat_row(name, stats, registered) + [_undefined(stats)] for name, stats in realised])
        lines += _quantile_lines("Realised spread", realised, registered)
        power = tables["power"]
        lines += ["### Minimum detectable effects (power_note; independence, Holm's first step)", "",
                  f"- primary: {power['primary']['decisions']} decisions, {power['primary']['with_a_fill']} with a "
                  f"fill: GBP {power['primary']['minimum_detectable_gbp_per_decision']} per decision",
                  f"- secondary: {power['secondary']['legs']} legs: {power['secondary']['minimum_detectable_points']} "
                  "points", ""]
    if registered:
        lines += _registered_lines(reg, value)
    return "\n".join(lines).rstrip("\n") + "\n"


# ---------------------------------------------------------------------------------------------------------------
# The run

def _counts(reg, tally):
    return {"records": tally.records, "replayed_markets": tally.replayed,
            "issued": {str(offset): tally.codes[offset][ISSUED] for offset in reg.decision_offsets_s},
            "eligible": {str(offset): tally.classes[offset][ELIGIBLE] for offset in reg.decision_offsets_s},
            "filled_main_legs": {str(offset): sum(row.filled for d in tally.decisions[offset] for (role, _), row
                                                  in d.legs.items() if role == MAIN)
                                 for offset in reg.decision_offsets_s}}


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
            # Registration data.duplicates: a market of a registered tier is keyed by its id alone.
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
    markdown_bytes = markdown(reg, value).encode("utf-8")
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
    parser = argparse.ArgumentParser(prog="python3 -m betangel_research.betfair.backtest.lp_study",
                                     description="Registered measurement lp-v1 on local historical files; nothing "
                                                 "here places an order.")
    parser.add_argument("--pass", dest="pass_name", required=True, choices=PASSES, help="blind first, then registered")
    parser.add_argument("--registration-sha256", required=True, help="sha256 of the registration file's raw bytes")
    parser.add_argument("--archive-root", required=True, help="data root with manifests/, raw/ and index/")
    parser.add_argument("--out", required=True, help="a new directory outside every Git worktree and data tree")
    parser.add_argument("--code-commit", required=True, help="the 40-character commit the code was checked out at")
    parser.add_argument("--blind-receipt", help="registered pass: the blind pass's receipt.json")
    parser.add_argument("--registration", help="registration file (default: research/studies/lp-v1.json)")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    result = run_study(args.pass_name, args.registration_sha256, args.archive_root, args.out, args.code_commit,
                       blind_receipt=args.blind_receipt, registration_path=args.registration)
    sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
    return 0 if result["status"] == "completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
