"""Replay one market's primary member up to a decision time: the point-in-time view a P0 decision reads (task c2a).

The task (b) index holds definitions and removals with their pt, but no prices, so a decision that reads rc.ltp needs
the raw stream again. replay_view reads the index record's primary member (its provenance; never another source,
whatever it holds later) under the data root and folds that market's messages published at or before the decision
time t, with the index's own reader and fold (market_index._messages, _checked_changes and _Market): the same
limits, strict JSON, non-decreasing pt, each message checked whole before any of it is applied, and an image (img
true) replacing the market's state instead of merging into it. Messages are applied in file order and reading stops
at the first message published after t, so the view at t holds every whole pt batch at or before t, the batch at t
included (decision_timing.equal_pt_order timer_after_equal_pt_batch: the timer fires after that batch), and never
part of one.

Prices: each runner's rc.ltp is kept as a timeline of changes ([pt, value], raw), applied in file order, so of two
rc entries for one runner in one batch the later wins, as for any later delta. A change without img that omits
ltp for a runner leaves it as it was; an rc entry carrying "ltp": null, and an image that does not carry a runner's
ltp (the runner absent from its rc, or present without ltp, or an image with no rc at all), set it to null at that
pt: plan section 5, img_replaces_market_state. A price is usable only as a finite number above 0 (the index's
valued rule for rc.ltp); anything else is missing, never zero. An rc entry whose runner id does not parse cannot be
attributed to a runner: the view counts such entries (malformed_price_changes), and task (c2a)'s cohort makes the
ltp input unsupported for the whole market rather than guessing.

When replay_view may run (the caller's precondition, checked): the record has a view (not the index's no_view), its
anchor, the first-published marketTime, was published at or before t (anchor pt <= t), and t is before the index's
view_ends pt, where the index stops the decision view (the first-published off, the first outcome-bearing
definition, or the pt from which the member could not be read). A decision at or after view_ends has no view to
replay: the cohort gives it its stopped-view reason from the index alone, so no outcome-bearing definition ever
reaches a view, and a member that fails only after t never cancels the decision at t (reading stops before the
failure).

Integrity (a run-integrity failure is ReplayProblem, never a strategy abstention): the record's provenance must name
a member of this manifest with the same sha256, the manifest's archive and the plan, month, date, event id, file
type and file market id the manifest derived from the member's path; the member is opened by safe_target and
every byte is hashed (the bytes consumed, then the rest drained unparsed) and must equal the manifest's sha256 and
size; a member that stops being readable at or before t although the index says its view runs past t, a market
the member never mentions by t although the index's anchor was published by then, a first-published marketTime
other than the record's anchor (pt and value: t and the view boundary rest on it), and an outcome-bearing
definition (the index's rule: CLOSED, settledTime, bspReconciled true, a runner WINNER, LOSER or PLACED, or any
runner bsp) published at or before t, which the record's view_ends says cannot be there, all refuse; and the replayed
decision-safe state at t must equal the index's decision_time entries with pt <= t (definition timelines with
outcome statuses filtered as the index does, runners with first_pt <= t and their timelines, removals). The
comparison is made only where both describe the same prefix (anchor pt <= t < view_ends pt); a mismatch is never
repaired by switching to another source.

The view (DecisionView) is immutable all the way down and holds only decision-time facts: no outcome, coverage,
other_sources or whole-file hash reaches it. Raw values are frozen as they enter it (frozen: a JSON array becomes a
tuple and an object a tuple of (key, value) pairs sorted by key, recursively), so neither the caller nor a strategy
can change a value another reader sees. Standard library only; it imports archive, market_index and the repository's
temporal module (as market_index does). """

import bz2
from dataclasses import dataclass

from ...temporal import timestamp_ms
from .archive import DEFAULT_LIMITS, ArchiveProblem, HashingReader, safe_target
from .market_index import (DEFINITION_FIELDS, OUTCOME_MARKET_STATUSES, OUTCOME_RUNNER_STATUSES, VALUED_FIELDS,
                           _as_of, _checked_changes, _Market, _messages, _runner_id, _track, _Unreadable)

OUTCOME_STATUSES = OUTCOME_RUNNER_STATUSES + OUTCOME_MARKET_STATUSES


class ReplayProblem(Exception):
    """A run-integrity failure: the input is not what the manifest and the index say; str() is "<where>:<reason>"."""


def usable_ltp(value):
    """True for a price the decision may use: a finite number above 0 (int or float, never bool)."""
    return VALUED_FIELDS["rc.ltp"](value)


@dataclass(frozen=True)
class RunnerAtDecision:
    """One runner the member had listed at or before t, as of t: status and listed are the latest values at or before
    t (None when absent or reset by an image); ltp is the last rc.ltp value at or before t (raw, None when none or
    reset) and ltp_pt its pt."""

    selection_id: int
    first_pt: int
    status: object
    listed: object
    ltp: object
    ltp_pt: object


@dataclass(frozen=True)
class DecisionView:
    """The point-in-time view of one market at decision time t (decision_ms), in milliseconds since the epoch.

    anchor_pt is when the first-published marketTime (anchor_value, at anchor_ms) became known; definition is a sorted
    tuple of (field, value as of t) for every DEFINITION_FIELDS entry the member had published by t (None for one an
    image reset); runners holds every runner first listed at or before t, sorted by id; removals every removal at or
    before t as (selection id, pt, status)."""

    market_id: str
    plan: str
    anchor_pt: int
    anchor_value: str
    anchor_ms: int
    decision_ms: int
    definition: tuple
    runners: tuple
    removals: tuple
    malformed_price_changes: int

    def field(self, name):
        """The value of one definition field as of t, or None when the member had not published it."""
        for key, value in self.definition:
            if key == name:
                return value
        return None


def frozen(value):
    """A JSON value with every array made a tuple and every object a tuple of sorted (key, value) pairs,
    recursively; any other value as it is."""
    if isinstance(value, list):
        return tuple(frozen(item) for item in value)
    if isinstance(value, dict):
        return tuple(sorted((key, frozen(item)) for key, item in value.items()))
    return value


def anchor_and_decision(record, offset_s):
    """(anchor pt, anchor ms, decision ms t) of an index record with a view, or None for the index's no_view. A
    record with a view always has a parseable first-published marketTime (market_index Market.record)."""
    first = record["decision_time"]["first_published_market_time"]
    if first is None:
        return None
    anchor_ms = timestamp_ms(first["value"])
    return first["pt"], anchor_ms, anchor_ms + offset_s * 1000


class PriceFold:
    """rc.ltp timelines per runner, with the image rule in the module docstring; change(pt, change) folds one market
    change, in file order."""

    def __init__(self):
        self.ltp = {}
        self.malformed = []

    def change(self, pt, change):
        image = change.get("img") is True
        carried = set()
        for item in change.get("rc") or ():
            rid = _runner_id(item.get("id"))
            if rid is None:
                self.malformed.append(pt)
                continue
            carried.add(rid)
            if "ltp" in item:
                _track(self.ltp, rid, pt, item["ltp"])
            elif image:
                _track(self.ltp, rid, pt, None)
        if image:
            for rid in list(self.ltp):
                if rid not in carried:
                    _track(self.ltp, rid, pt, None)


def _member(manifest, record):
    """The manifest entry of the record's primary member, after checking the record names it exactly."""
    provenance = record["provenance"]
    if provenance["archive_sha256"] != manifest["archive"]["sha256"]:
        raise ReplayProblem(f"replay:{record['market_id']}:archive_mismatch")
    for member in manifest["members"]:
        if member["path"] == provenance["member_path"]:
            if member["sha256"] != provenance["member_sha256"]:
                raise ReplayProblem(f"replay:{record['market_id']}:member_sha256_mismatch")
            derived = {"plan": member["plan"], "path_month": member["month"], "path_date": member["date"],
                       "event_id_from_path": member["event_id"], "file_type": member["file_type"],
                       "file_market_id": member["market_id"]}
            if any(provenance.get(key) != value for key, value in derived.items()):
                raise ReplayProblem(f"replay:{record['market_id']}:provenance_mismatch")
            return member
    raise ReplayProblem(f"replay:{record['market_id']}:member_not_in_manifest")


def _fold(raw, market_id, t, limits):
    """(_Market or None, PriceFold, unreadable_from or None, (sha256, size) of every byte of the file): the market's
    messages published at or before t, folded in file order; unreadable_from is the pt from which the member could
    not be read when that happened before reading passed t (the failing message's pt when known, else the last pt
    applied, as market_index._read_member)."""
    market, prices, unreadable_from, applied = None, PriceFold(), None, None
    with open(raw, "rb") as handle:
        reader = HashingReader(handle)
        try:
            with bz2.BZ2File(reader) as stream:
                for pt, message in _messages(stream, limits):
                    if pt > t:
                        break
                    try:
                        checked = _checked_changes(message)
                    except _Unreadable as error:
                        raise _Unreadable(str(error), pt) from None
                    for mid, change in checked:
                        if mid != market_id:
                            continue
                        if market is None:
                            market = _Market(market_id, pt)
                        market.change(pt, change)
                        prices.change(pt, change)
                    applied = pt
        except _Unreadable as error:
            unreadable_from = error.pt if error.pt is not None else applied
            if unreadable_from is None:
                unreadable_from = -1                # failed before any message: nothing is known at any t
        except (OSError, EOFError, ValueError):
            unreadable_from = -1 if applied is None else applied
        digest = reader.drain()
    if unreadable_from is not None and unreadable_from > t:
        unreadable_from = None                      # a failing message published after t: not in this view
    return market, prices, unreadable_from, digest


def _decision_safe(key, entries):
    """The index's belt-and-braces filter: no outcome status in a decision-time status timeline."""
    if key == "status":
        return [entry for entry in entries if entry[1] not in OUTCOME_STATUSES]
    return entries


def _upto(timeline, t):
    return [entry for entry in timeline if entry[0] <= t]


def _index_projection(decision, t):
    """The index's decision_time entries published at or before t, in the replay's comparable shape."""
    definition = {key: _upto(tl, t) for key, tl in decision["definition"].items() if _upto(tl, t)}
    runners = []
    for item in decision["runners"]:
        if item["first_pt"] > t:
            continue
        fields = {key: _upto(tl, t) for key, tl in item.items() if key not in ("id", "first_pt") and _upto(tl, t)}
        runners.append({"id": item["id"], "first_pt": item["first_pt"], **fields})
    removals = [[item["id"], item["pt"], item["status"]] for item in decision["removals"] if item["pt"] <= t]
    return {"definition": definition, "runners": runners, "removals": removals}


def _replay_projection(market, t):
    definition = {}
    for key, timeline in sorted(market.fields.items()):
        kept = _decision_safe(key, _upto(timeline, t))
        if kept:
            definition[key] = kept
    runners = []
    for rid, state in sorted(market.runners.items()):
        if state["first_pt"] > t:
            continue
        fields = {}
        for key, timeline in sorted(state["fields"].items()):
            kept = _decision_safe(key, _upto(timeline, t))
            if kept:
                fields[key] = kept
        runners.append({"id": rid, "first_pt": state["first_pt"], **fields})
    removals = [[item["id"], item["pt"], item["status"]] for item in market.removals() if item["pt"] <= t]
    return {"definition": definition, "runners": runners, "removals": removals}


def _timing(record, offset_s):
    """(anchor pt, anchor ms, t) after the caller's precondition (a view, an anchor published by t, t before
    view_ends); ValueError otherwise."""
    timing = anchor_and_decision(record, offset_s)
    if timing is None:
        raise ValueError("replay:no_view")
    anchor_pt, _, t = timing
    ends = record["decision_time"]["view_ends"]["pt"]
    if anchor_pt > t:
        raise ValueError("replay:anchor_after_decision")
    if ends is None or t >= ends:
        raise ValueError("replay:decision_at_or_after_view_ends")
    return timing


def view_from_fold(record, market, prices, offset_s):
    """The DecisionView of one index record at its decision time from a fold of its member up to t (a _Market and a
    PriceFold holding nothing published after t), after the checks every route to a view must pass: the caller's
    precondition, the market present, the first-published marketTime equal to the record's anchor, no
    outcome-bearing definition folded, and the decision-safe state at t equal to the record's decision_time
    entries at or before t; ReplayProblem otherwise. Every value in the view is frozen. replay_view reaches it only
    after its integrity checks of the member; task (c2b3a)'s prefix check reaches it with a fold of a verified
    member's prefix."""
    market_id = record["market_id"]
    anchor_pt, anchor_ms, t = _timing(record, offset_s)
    where = f"replay:{market_id}"
    if market is None:
        raise ReplayProblem(f"{where}:market_not_replayed")
    first = record["decision_time"]["first_published_market_time"]
    if (market.fields.get("marketTime") or [None])[0] != [first["pt"], first["value"]]:
        raise ReplayProblem(f"{where}:anchor_mismatch")
    if market.first_outcome_pt is not None:
        raise ReplayProblem(f"{where}:outcome_before_decision")
    if _replay_projection(market, t) != _index_projection(record["decision_time"], t):
        raise ReplayProblem(f"{where}:index_mismatch")
    definition = tuple((key, frozen(_as_of(market.fields[key], t))) for key in DEFINITION_FIELDS
                       if key in market.fields and _upto(market.fields[key], t))
    runners = []
    for rid, state in sorted(market.runners.items()):
        if state["first_pt"] > t:
            continue
        timeline = _upto(prices.ltp.get(rid, []), t)
        runners.append(RunnerAtDecision(
            selection_id=rid, first_pt=state["first_pt"], status=frozen(_as_of(state["fields"].get("status"), t)),
            listed=_as_of(state["fields"].get("listed"), t), ltp=frozen(timeline[-1][1]) if timeline else None,
            ltp_pt=timeline[-1][0] if timeline else None))
    removals = tuple((item["id"], item["pt"], item["status"]) for item in market.removals() if item["pt"] <= t)
    return DecisionView(market_id=market_id, plan=record["provenance"]["plan"], anchor_pt=anchor_pt,
                        anchor_value=first["value"], anchor_ms=anchor_ms, decision_ms=t, definition=definition,
                        runners=tuple(runners), removals=removals,
                        malformed_price_changes=sum(1 for pt in prices.malformed if pt <= t))


def replay_view(root, manifest, record, offset_s, limits=DEFAULT_LIMITS):
    """The DecisionView of one index record at its decision time (module docstring); ReplayProblem when the input
    is not what the manifest and the index say, ValueError when the caller's precondition does not hold (no view,
    anchor published after t, or t at or after view_ends)."""
    market_id = record["market_id"]
    _, _, t = _timing(record, offset_s)
    member = _member(manifest, record)
    where = f"replay:{market_id}"
    try:
        raw = safe_target(root, manifest["archive"]["sha256"], member["path"])
    except ArchiveProblem as error:
        raise ReplayProblem(f"{where}:{error}") from None
    try:
        if not raw.is_file():
            raise ReplayProblem(f"{where}:member_missing")
        market, prices, unreadable_from, digest = _fold(raw, market_id, t, limits)
    except OSError:
        raise ReplayProblem(f"{where}:member_unreadable_file") from None
    if digest != (member["sha256"], member["size_bytes"]):
        raise ReplayProblem(f"{where}:member_hash_mismatch")
    if unreadable_from is not None:
        raise ReplayProblem(f"{where}:unreadable_before_view_ends")
    return view_from_fold(record, market, prices, offset_s)
