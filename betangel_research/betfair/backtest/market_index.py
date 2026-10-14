"""Per-market index of ingested Betfair Historical Data members (index schema_version 1), built from market
definitions only.

Input: an archive's manifest (archive.py) and its raw members under the data root. The manifest must be canonical
(its bytes re-serialise identically), name its own sha256, carry the keys and shapes the index and audit read, and
every member's path must be safe and re-derive the plan, month, date, event id and file type the manifest states.
A member whose month the gate now refuses is not read. Every other member is streamed once through a bounded reader
equivalent to betangel_research.stream.messages (which opens by path with no line or size bound and no hash check,
so it is not reused): blank lines skipped, only op "mcm", an integer pt that never decreases, no segmentType, at
most max_line_bytes per line and max_member_decompressed_bytes per member, NaN, Infinity and numbers that overflow
to infinity refused. The compressed bytes are hashed while read; a member whose hash or size differs from the
manifest, or that is missing or cannot be opened, refuses the whole index (exit 2).

A member that is intact but cannot be read whole under these rules is listed as unreadable with its reason and
complete_before_pt. Each message is checked whole before any of it is applied, so everything before the failing
line is applied exactly; complete_before_pt is the pt of the last message applied, and only what was published
strictly before it is complete (the failing line may belong to that same pt batch, and a strategy never sees half a
batch). Every market seen before the failure is still indexed from what was published strictly before that pt
(read_complete false), so a bad line after the off neither drops a market from the population nor reaches back
into its decision-time view; when the failure comes before the decision cutoff, decision_time ends at that pt.

One record per (plan, market id) per archive (canonical JSON, one line each, sorted by plan and numeric market
id; two tiers of one market are two data products, never merged), each with separate top-level objects; ONLY
decision_time may ever reach a strategy, every other object is for provenance, the audit or the evaluator:
- provenance: archive and member sha256, member path, file type (market or event file), plan, month and date from
  the path, inspected_month, and other_sources (every other member of the same plan holding the same market,
  whether it was read whole, and whether its derived record agrees: agrees compares the decision, outcome and
  coverage parts, so it is outcome-derived; agrees_decision_time compares decision_time only). A record read whole
  is primary over a partial one; then a market file named after the market, then an event file, then a market file
  named after another market; then by path.
- checks: path_month, the month of the first-published marketTime, month_mismatch, date_mismatch (the path date
  against the UTC date of the first-published marketTime) and event_id_mismatch (the path event id against the
  first definition's eventId), each null when unknown, first_pt, whether a market file holds a market it is not
  named after, untracked_definition_fields (definition keys in neither DEFINITION_FIELDS nor
  OUTCOME_DEFINITION_FIELDS: only their names reach coverage) and unrecognised_runner_statuses.
- coverage (audit only, outcome-correlated): whole_file, the names of every stream field seen for the market
  anywhere in the member (mc.*, rc.*, md.*, md.runners.*), presence only, so md.runners.bsp or md.settledTime show
  whether a result came; before_cutoff, the names first seen strictly before the decision cutoff; and
  valued_whole_file and valued_before_cutoff for VALUED_FIELDS, counted only with a usable value (an rc.ltp above
  0, a finite rc.spn or rc.spf, a final bsp that is a finite number of at least MIN_BSP).
- decision_time: facts with the pt each became known, strictly before the decision cutoff: the first-published
  marketTime; timelines (changes only, [pt, value]) of DEFINITION_FIELDS, including marketTime revisions, status,
  inPlay, numberOfWinners as declared, marketBaseRate, bspMarket and version; runners (id, first_pt and timelines
  of RUNNER_FIELDS: sortPriority, name, hc, status, adjustmentFactor, removalDate, listed); removals (runner, pt,
  status REMOVED or REMOVED_VACANT, and removalDate and adjustmentFactor as of that pt, when present); and
  view_ends ({pt, reason}: the cutoff itself). The cutoff is the earliest of the first-published scheduled off, the
  first outcome-bearing definition (CLOSED, settledTime, bspReconciled true, a runner WINNER, LOSER or PLACED, or
  any runner bsp) and, for a member not read whole, complete_before_pt; so no outcome value and nothing published at
  or after the first-published off is in it. Like every entry, view_ends is pt-stamped: a consumer keeps only
  entries with pt <= its decision time, and a decision at or after view_ends.pt has no complete view here (the
  market had a result-bearing definition, reached its off, or the member could not be read further), so task (c)
  must give it an explicit abstain or unsupported reason, never simulate it from this truncated view and never
  drop it. A market with no parseable first-published marketTime has an empty decision_time and view_ends null.
- evaluator_only_outcome: the cutoff and why, whether the member was read whole (read_complete, unreadable_reason,
  complete_before_pt), every timeline from the cutoff on (revised metadata, later removals, in-play and status
  changes), OUTCOME_DEFINITION_FIELDS and runner bsp timelines whatever their pt (raw values), final runner status
  and bsp (null unless a finite number of at least MIN_BSP), final market status, whether and when CLOSED was
  reached, settled time, winners observed (null when no result-bearing definition exists), the declared number of
  winners at the end, dead_heat (see _dead_heat; null when not assessable), results_revised_after_close (a runner
  status or bsp, or numberOfWinners, changed after CLOSED; null when not closed), runners missing a final bsp (any
  final status but REMOVED, REMOVED_VACANT or HIDDEN), runners whose final bsp is present but unusable (for example
  the strings "NaN" or "Infinity"), every marketTime month, last pt and counts. Nothing in decision_time is derived
  from it.

Months are UTC calendar months of marketTime (archive.market_month). A record whose marketTime months (every one
seen, before any truncation) include one the gate refuses is not written (refused_markets in the head). The index
lives at index/<archive sha256>/markets-v1.jsonl with head-v1.json beside it (canonical JSON: manifest sha256,
markets sha256, every file's status, complete_before_pt and markets, refused markets); index_sha256 is the head's
sha256, and the head covers the markets file through markets_sha256. Records are spilled to a temporary file as
each member is read and merged from it, so memory holds one key per record rather than every record (the head,
with one entry per member, is still built in memory). Writes are atomic as in archive.py; a leftover spill or
.partial file refuses until a person removes it; an existing index with other bytes is refused (index_would_change,
or gate_changed when the manifest was written under another month gate), never replaced: a change of meaning needs
a new schema version and file name. Every file-system failure becomes a refusal with its place.
"""

import bz2
from collections import namedtuple
import hashlib
import itertools
import json
import math

from ...temporal import timestamp_ms
from .archive import (DEFAULT_LIMITS, MANIFEST_KIND, MANIFEST_SCHEMA_VERSION, MARKET_ID_RE, SHA256_RE, ArchiveProblem,
                      HashingReader, check_data_root, file_sha256, gate_snapshot, manifest_bytes, market_month,
                      month_gate, name_problem, parse_member_path, safe_path, safe_target, utc_moment, write_new)
from .contract import canonical_json

INDEX_SCHEMA_VERSION = 1
HEAD_KIND = "betfair_historical_market_index_head"
HEAD_FILE = f"head-v{INDEX_SCHEMA_VERSION}.json"
MARKETS_FILE = f"markets-v{INDEX_SCHEMA_VERSION}.jsonl"
SPILL_FILE = f"{MARKETS_FILE}.spill"
FILE_STATUSES = ("indexed", "refused", "unreadable")
# Market-definition fields known when published (each with its pt): eligible for decision_time before the cutoff.
DEFINITION_FIELDS = ("betDelay", "bettingType", "bspMarket", "complete", "countryCode", "crossMatching",
                     "discountAllowed", "eachWayDivisor", "eventId", "eventName", "eventTypeId", "inPlay",
                     "marketBaseRate", "marketTime", "marketType", "name", "numberOfActiveRunners", "numberOfWinners",
                     "openDate", "persistenceEnabled", "regulators", "runnersVoidable", "status", "suspendTime",
                     "timezone", "turnInPlayEnabled", "venue", "version")
# Outcomes or settlement state: evaluator_only_outcome whatever their pt.
OUTCOME_DEFINITION_FIELDS = ("bspReconciled", "settledTime")
_TRACKED_DEFINITION_KEYS = frozenset(DEFINITION_FIELDS + OUTCOME_DEFINITION_FIELDS + ("runners",))
RUNNER_FIELDS = ("adjustmentFactor", "hc", "listed", "name", "removalDate", "sortPriority", "status")
OUTCOME_RUNNER_FIELDS = ("bsp",)
OUTCOME_RUNNER_STATUSES = ("LOSER", "PLACED", "WINNER")
OUTCOME_MARKET_STATUSES = ("CLOSED",)
# The runner statuses the Historical Data spec lists; any other is reported in checks, never silently classified.
KNOWN_RUNNER_STATUSES = ("ACTIVE", "HIDDEN", "LOSER", "PLACED", "REMOVED", "REMOVED_VACANT", "WINNER")
REMOVED_STATUSES = ("REMOVED", "REMOVED_VACANT")
NO_BSP_EXPECTED = REMOVED_STATUSES + ("HIDDEN",)
# Market types whose dead heats _dead_heat can judge from WINNER/PLACED counts and the declared numberOfWinners.
WINNER_COUNT_MARKET_TYPES = ("OTHER_PLACE", "PLACE", "WIN")
# The lowest price Betfair offers; a final bsp below it, or not a finite number, is not a usable settlement price.
MIN_BSP = 1.01


def _number(value):
    return type(value) is int or (type(value) is float and math.isfinite(value))


def usable_bsp(value):
    return _number(value) and value >= MIN_BSP


VALUED_FIELDS = {"md.runners.bsp": usable_bsp, "rc.ltp": lambda value: _number(value) and value > 0,
                 "rc.spf": _number, "rc.spn": _number}


class IndexProblem(Exception):
    """An integrity or input problem that refuses the index; str() is "<where>:<reason>"."""


class _Unreadable(Exception):
    """A member that is intact but cannot be read under the stream rules; str() is the reason."""


def _refuse_constant(name):
    raise ValueError("non_finite_number")


def _finite_float(text):
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("non_finite_number")
    return value


def loads_strict(data):
    """json.loads refusing NaN, Infinity and a number that overflows to infinity (ValueError)."""
    return json.loads(data, parse_constant=_refuse_constant, parse_float=_finite_float)


def market_sort_key(market_id):
    exchange, _, number = market_id.partition(".")
    return int(exchange), int(number)


def load_manifest(root, archive_sha256):
    """(manifest, sha256 of its bytes) after the checks in the module docstring; IndexProblem otherwise."""
    where = f"manifest:{archive_sha256}"
    try:
        path = safe_path(root, "manifests", f"{archive_sha256}.json")
        data = path.read_bytes()
    except ArchiveProblem as error:
        raise IndexProblem(f"{where}:{error}") from None
    except FileNotFoundError:
        raise IndexProblem(f"{where}:missing") from None
    except OSError:
        raise IndexProblem(f"{where}:unreadable") from None
    try:
        manifest = loads_strict(data)
        canonical = manifest_bytes(manifest) == data
    except (ValueError, TypeError, RecursionError):
        raise IndexProblem(f"{where}:not_json") from None
    if not canonical:
        raise IndexProblem(f"{where}:not_canonical")
    try:
        if (manifest["schema_version"], manifest["kind"]) != (MANIFEST_SCHEMA_VERSION, MANIFEST_KIND):
            raise IndexProblem(f"{where}:unsupported")
        if manifest["archive"]["sha256"] != archive_sha256:
            raise IndexProblem(f"{where}:names_another_archive")
        if (not isinstance(manifest["plan"], (str, type(None))) or not isinstance(manifest["months"], list)
                or not isinstance(manifest["month_gate"], dict) or not isinstance(manifest["members"], list)
                or not all(isinstance(name, str) for name in manifest["directories"])
                or not all(isinstance(item, dict) and isinstance(item["path"], str) and isinstance(item["reason"], str)
                           for item in manifest["refused"])):
            raise IndexProblem(f"{where}:malformed")
        for member in manifest["members"]:
            unsafe = name_problem(member["path"])
            parsed, reason = (None, unsafe) if unsafe else parse_member_path(member["path"])
            stated = {key: member[key] for key in ("plan", "month", "date", "event_id", "file_type", "market_id")}
            if (reason or parsed != stated or not SHA256_RE.fullmatch(member["sha256"])
                    or type(member["size_bytes"]) is not int or type(member["decompressed_bytes"]) is not int
                    or type(member["inspected_month"]) is not bool):
                raise IndexProblem(f"{where}:member_inconsistent")
    except (KeyError, TypeError, AttributeError):
        raise IndexProblem(f"{where}:malformed") from None
    return manifest, hashlib.sha256(data).hexdigest()


def _messages(stream, limits):
    """(pt, message) for each mcm line of one decompressed member; _Unreadable(reason) on the first violation."""
    previous, total = -1, 0
    while True:
        line = stream.readline(limits.max_line_bytes + 1)
        if not line:
            return
        total += len(line)
        if len(line) > limits.max_line_bytes:
            raise _Unreadable("line_too_long")
        if total > limits.max_member_decompressed_bytes:
            raise _Unreadable("decompressed_too_large")
        if not line.strip():
            continue
        try:
            message = loads_strict(line)
        except (ValueError, RecursionError):
            raise _Unreadable("not_json") from None
        if not isinstance(message, dict):
            raise _Unreadable("malformed_message")
        if message.get("op") != "mcm":
            continue
        pt = message.get("pt")
        if type(pt) is not int or pt < previous:
            raise _Unreadable("invalid_or_decreasing_pt")
        if message.get("segmentType") is not None:
            raise _Unreadable("segmented")
        previous = pt
        yield pt, message


def _runner_id(value):
    if type(value) is int and value >= 0:
        return value
    if isinstance(value, str) and value.isdigit() and value.isascii():
        return int(value)
    return None


def _checked_changes(message):
    """[(market id, change)] of one mcm message after checking its whole shape, so applying it cannot fail half way;
    _Unreadable otherwise."""
    changes = message.get("mc", [])
    if not isinstance(changes, list):
        raise _Unreadable("malformed_message")
    checked = []
    for change in changes:
        market_id = change.get("id") if isinstance(change, dict) else None
        if not (isinstance(market_id, str) and MARKET_ID_RE.fullmatch(market_id)):
            raise _Unreadable("malformed_market_id")
        runner_changes = change.get("rc")
        if runner_changes is not None and (not isinstance(runner_changes, list)
                                           or not all(isinstance(item, dict) for item in runner_changes)):
            raise _Unreadable("malformed_runner_change")
        definition = change.get("marketDefinition")
        if definition is not None:
            if not isinstance(definition, dict) or not isinstance(definition.get("runners", []), list):
                raise _Unreadable("malformed_market_definition")
            ids = [_runner_id(item.get("id")) if isinstance(item, dict) else None
                   for item in definition.get("runners", [])]
            if None in ids or len(set(ids)) != len(ids):
                raise _Unreadable("malformed_runner")
        checked.append((market_id, change))
    return checked


def _same(a, b):
    return type(a) is type(b) and a == b


def _track(store, key, pt, value):
    """Append [pt, value] to store[key] when value differs from the last one (a timeline of changes only); a field
    never present has no timeline, one that disappears records null."""
    timeline = store.get(key)
    if timeline is None:
        if value is not None:
            store[key] = [[pt, value]]
    elif not _same(timeline[-1][1], value):
        timeline.append([pt, value])


def _last(timeline):
    return timeline[-1][1] if timeline else None


def _as_of(timeline, pt):
    value = None
    for when, item in timeline or []:
        if when > pt:
            break
        value = item
    return value


def _cut(store, before):
    """A timeline store keeping only the entries published strictly before pt `before`."""
    kept = {}
    for key, timeline in store.items():
        entries = [entry for entry in timeline if entry[0] < before]
        if entries:
            kept[key] = entries
    return kept


def _dead_heat(closed, market_type, declared, statuses):
    """True or False for a closed market whose dead heat the counts can judge, else None. EACH_WAY (numberOfWinners
    is the number of places; one WINNER and the placed runners PLACED): more than one WINNER, or more WINNER plus
    PLACED than declared. WIN, PLACE and OTHER_PLACE: more WINNER than declared. A declared number below 1 (flumine
    reads 0 as a variable number of winners), a missing one, another market type, or a market never CLOSED: None."""
    if not closed or type(declared) is not int or declared < 1:
        return None
    winners, placed = statuses.count("WINNER"), statuses.count("PLACED")
    if market_type == "EACH_WAY":
        return winners > 1 or winners + placed > declared
    if market_type in WINNER_COUNT_MARKET_TYPES:
        return winners > declared
    return None


def _status_text(value):
    return value if isinstance(value, str) else canonical_json(value).decode("utf-8")


class _Market:
    """Everything one member says about one market, folded from its checked messages in file order."""

    def __init__(self, market_id, pt):
        self.market_id, self.first_pt, self.last_pt = market_id, pt, pt
        self.changes = self.definitions = 0
        self.settled = (None, 0, 0)          # (last_pt, changes, definitions) before the current pt batch
        self.fields, self.outcome_fields, self.runners = {}, {}, {}
        self.seen, self.valued, self.untracked = {}, {}, {}      # name -> first pt
        self.first_outcome_pt = None

    def _note(self, name, pt, value):
        self.seen.setdefault(name, pt)
        check = VALUED_FIELDS.get(name)
        if check is not None and check(value):
            self.valued.setdefault(name, pt)

    def change(self, pt, change):
        if pt > self.last_pt:
            self.settled = (self.last_pt, self.changes, self.definitions)
        self.changes += 1
        self.last_pt = pt
        for key, value in change.items():
            if key not in ("id", "marketDefinition") and not (key == "img" and not value):
                self.seen.setdefault(f"mc.{key}", pt)
        for item in change.get("rc") or ():
            for key, value in item.items():
                if key != "id":
                    self._note(f"rc.{key}", pt, value)
        definition = change.get("marketDefinition")
        if definition is not None:
            self.definition(pt, definition)

    def definition(self, pt, md):
        self.definitions += 1
        self.seen.setdefault("mc.marketDefinition", pt)
        for key in md:
            if key != "runners":
                self.seen.setdefault(f"md.{key}", pt)
                if key not in _TRACKED_DEFINITION_KEYS:
                    self.untracked.setdefault(key, pt)
        for key in DEFINITION_FIELDS:
            _track(self.fields, key, pt, md.get(key))
        for key in OUTCOME_DEFINITION_FIELDS:
            _track(self.outcome_fields, key, pt, md.get(key))
        outcome = (md.get("status") in OUTCOME_MARKET_STATUSES or md.get("settledTime") is not None
                   or md.get("bspReconciled") is True)
        listed = set()
        for runner in md.get("runners", []):
            rid = _runner_id(runner["id"])
            listed.add(rid)
            state = self.runners.setdefault(rid, {"first_pt": pt, "fields": {}, "outcome": {}})
            for key, value in runner.items():
                if key != "id":
                    self._note(f"md.runners.{key}", pt, value)
            for key in RUNNER_FIELDS:
                if key != "listed":
                    _track(state["fields"], key, pt, runner.get(key))
            for key in OUTCOME_RUNNER_FIELDS:
                _track(state["outcome"], key, pt, runner.get(key))
            outcome = outcome or runner.get("status") in OUTCOME_RUNNER_STATUSES or runner.get("bsp") is not None
        for rid, state in self.runners.items():
            _track(state["fields"], "listed", pt, rid in listed)
        if outcome and self.first_outcome_pt is None:
            self.first_outcome_pt = pt

    def truncate(self, before):
        """Keep only what was published strictly before pt `before` (a member not read whole; see the module
        docstring): the batch at `before` and everything after it is dropped, counts included."""
        self.fields, self.outcome_fields = _cut(self.fields, before), _cut(self.outcome_fields, before)
        self.runners = {rid: {"first_pt": state["first_pt"], "fields": _cut(state["fields"], before),
                              "outcome": _cut(state["outcome"], before)}
                        for rid, state in self.runners.items() if state["first_pt"] < before}
        for store in (self.seen, self.valued, self.untracked):
            for name in [name for name, pt in store.items() if pt >= before]:
                del store[name]
        if self.first_outcome_pt is not None and self.first_outcome_pt >= before:
            self.first_outcome_pt = None
        if self.last_pt >= before:
            self.last_pt, self.changes, self.definitions = self.settled

    def market_time_months(self):
        return {month for month in (market_month(value) for _, value in self.fields.get("marketTime", [])) if month}

    def cutoff(self):
        """(first-published marketTime entry or None, cutoff pt or None, reason)."""
        first = (self.fields.get("marketTime") or [None])[0]
        try:
            published = timestamp_ms(first[1]) if first else None
        except (ValueError, TypeError, OverflowError):
            return first, None, "first_published_market_time_unparseable"
        if published is None:
            return None, None, "no_first_published_market_time"
        if self.first_outcome_pt is not None and self.first_outcome_pt < published:
            return first, self.first_outcome_pt, "first_outcome_bearing_definition"
        return first, published, "first_published_scheduled_off"

    def removals(self):
        found = []
        for rid, state in sorted(self.runners.items()):
            previous = None
            for pt, status in state["fields"].get("status", []):
                if status in REMOVED_STATUSES and previous not in REMOVED_STATUSES:
                    found.append({"id": rid, "pt": pt, "status": status,
                                  "removal_date": _as_of(state["fields"].get("removalDate"), pt),
                                  "adjustment_factor": _as_of(state["fields"].get("adjustmentFactor"), pt)})
                previous = status
        return found

    def record(self, provenance, unreadable=None):
        """The index record. unreadable is (reason, complete_before_pt) for a member not read whole, after
        truncate(complete_before_pt); None for one read whole."""
        first, cutoff, reason = self.cutoff()
        if unreadable is not None and cutoff is not None and unreadable[1] < cutoff:
            cutoff, reason = unreadable[1], "member_unreadable_from_pt"

        def before(timeline):
            return [] if cutoff is None else [entry for entry in timeline if entry[0] < cutoff]

        def after(timeline):
            return list(timeline) if cutoff is None else [entry for entry in timeline if entry[0] >= cutoff]

        def decision_safe(key, entries):     # belt and braces: the cutoff already excludes these
            if key == "status":
                return [e for e in entries if e[1] not in OUTCOME_RUNNER_STATUSES + OUTCOME_MARKET_STATUSES]
            return entries

        decision_definition, later_definition = {}, {}
        for key, timeline in sorted(self.fields.items()):
            if before(timeline):
                decision_definition[key] = decision_safe(key, before(timeline))
            if after(timeline):
                later_definition[key] = after(timeline)
        decision_runners, later_runners, final_runners, unusable_bsp, statuses = [], [], [], [], set()
        for rid, state in sorted(self.runners.items()):
            early = {key: decision_safe(key, before(tl)) for key, tl in sorted(state["fields"].items()) if before(tl)}
            if cutoff is not None and state["first_pt"] < cutoff:
                decision_runners.append({"id": rid, "first_pt": state["first_pt"], **early})
            late = {key: after(tl) for key, tl in sorted(state["fields"].items()) if after(tl)}
            late.update({key: tl for key, tl in sorted(state["outcome"].items())})
            if late:
                later_runners.append({"id": rid, **late})
            statuses.update(_status_text(value) for _, value in state["fields"].get("status", []) if value is not None)
            raw_bsp = _last(state["outcome"].get("bsp"))
            bsp = raw_bsp if usable_bsp(raw_bsp) else None
            if raw_bsp is not None and bsp is None:
                unusable_bsp.append(rid)
            final_runners.append({"id": rid, "status": _last(state["fields"].get("status")), "bsp": bsp,
                                  "listed": state["fields"]["listed"][-1][1]})
        removals = self.removals()
        market_statuses = self.fields.get("status", [])
        closed = [pt for pt, value in market_statuses if value in OUTCOME_MARKET_STATUSES]
        declared = _last(self.fields.get("numberOfWinners"))
        final_statuses = [runner["status"] for runner in final_runners]
        revised = None
        if closed:
            timelines = [self.fields.get("numberOfWinners", [])]
            for state in self.runners.values():
                timelines += [state["fields"].get("status", []), state["outcome"].get("bsp", [])]
            revised = any(entry[0] > closed[0] for timeline in timelines for entry in timeline)
        moment = utc_moment(first[1]) if first else None
        market_time_month = market_month(first[1]) if first else None
        event_id = _last((self.fields.get("eventId") or [])[:1])
        read_complete = unreadable is None
        decision = {
            "first_published_market_time": ({"pt": first[0], "value": first[1]}
                                            if first and cutoff is not None and first[0] < cutoff else None),
            "definition": decision_definition, "runners": decision_runners,
            "removals": [item for item in removals if cutoff is not None and item["pt"] < cutoff],
            "view_ends": None if cutoff is None else {"pt": cutoff, "reason": reason},
        }
        outcome = {
            "decision_cutoff": {"pt": cutoff, "reason": reason},
            "read_complete": read_complete, "unreadable_reason": None if read_complete else unreadable[0],
            "complete_before_pt": None if read_complete else unreadable[1],
            "definition_after_cutoff": later_definition,
            "outcome_definition": {key: tl for key, tl in sorted(self.outcome_fields.items())},
            "runners_after_cutoff": later_runners,
            "removals_after_cutoff": [item for item in removals if cutoff is None or item["pt"] >= cutoff],
            "final_runners": final_runners,
            "final_status": market_statuses[-1][1] if market_statuses else None,
            "closed_reached": bool(closed), "closed_first_pt": closed[0] if closed else None,
            "settled_time": _last(self.outcome_fields.get("settledTime")),
            "winners_observed": (final_statuses.count("WINNER") if closed or self.first_outcome_pt is not None
                                 else None),
            "declared_number_of_winners": declared,
            "dead_heat": _dead_heat(bool(closed), _last(self.fields.get("marketType")), declared, final_statuses),
            "results_revised_after_close": revised,
            "missing_final_bsp_runner_ids": [r["id"] for r in final_runners if r["status"] not in NO_BSP_EXPECTED
                                             and r["bsp"] is None],
            "unusable_final_bsp_runner_ids": unusable_bsp,
            "market_time_months": sorted(self.market_time_months()),
            "last_pt": self.last_pt, "definitions": self.definitions, "changes": self.changes,
        }
        checks = {
            "path_month": provenance["path_month"], "market_time_month": market_time_month,
            "month_mismatch": None if market_time_month is None else market_time_month != provenance["path_month"],
            "date_mismatch": None if moment is None else moment.date().isoformat() != provenance["path_date"],
            "event_id_mismatch": None if event_id is None else str(event_id) != provenance["event_id_from_path"],
            "first_pt": self.first_pt,
            "market_file_holds_other_market": (provenance["file_type"] == "market"
                                               and provenance["file_market_id"] != self.market_id),
            "untracked_definition_fields": sorted(self.untracked),
            "unrecognised_runner_statuses": sorted(text for text in statuses if text not in KNOWN_RUNNER_STATUSES),
        }

        def early(store):
            return sorted(name for name, pt in store.items() if cutoff is not None and pt < cutoff)

        coverage = {"whole_file": sorted(self.seen), "before_cutoff": early(self.seen),
                    "valued_whole_file": sorted(self.valued), "valued_before_cutoff": early(self.valued)}
        return {"schema_version": INDEX_SCHEMA_VERSION, "market_id": self.market_id, "provenance": provenance,
                "checks": checks, "coverage": coverage, "decision_time": decision, "evaluator_only_outcome": outcome}


def _read_member(path, limits):
    """({market id: _Market}, unreadable reason or None, (sha256, size) of the compressed bytes read,
    complete_before_pt: the pt of the last message applied, or None when none was). OSError when the file cannot be
    opened or read."""
    markets, reason, applied = {}, None, None
    with open(path, "rb") as handle:
        reader = HashingReader(handle)
        try:
            with bz2.BZ2File(reader) as stream:
                for pt, message in _messages(stream, limits):
                    for market_id, change in _checked_changes(message):
                        market = markets.get(market_id)
                        if market is None:
                            market = markets[market_id] = _Market(market_id, pt)
                        market.change(pt, change)
                    applied = pt
        except _Unreadable as error:
            reason = str(error)
        except (OSError, EOFError, ValueError):
            reason = "not_bz2"
        digest = reader.drain()
    return markets, reason, digest, applied


def _provenance(manifest, member):
    return {"archive_sha256": manifest["archive"]["sha256"], "member_path": member["path"],
            "member_sha256": member["sha256"], "file_type": member["file_type"], "file_market_id": member["market_id"],
            "event_id_from_path": member["event_id"], "plan": member["plan"], "path_month": member["month"],
            "path_date": member["date"], "inspected_month": member["inspected_month"]}


def _body(record):
    return canonical_json([record["decision_time"], record["evaluator_only_outcome"], record["coverage"]])


# One spilled record: where it sits in the spill, how it ranks for primary, and what other_sources needs.
_Entry = namedtuple("_Entry", "key rank offset length body decision member_sha256 file_type")


def _spill(root, manifest, spill, limits):
    """Read every member, write each record to spill as one canonical line; (files, entries, refused markets)."""
    sha = manifest["archive"]["sha256"]
    files, entries, refused_markets, offset = [], [], [], 0
    for member in manifest["members"]:
        entry = {"path": member["path"], "member_sha256": member["sha256"], "plan": member["plan"],
                 "path_month": member["month"], "file_type": member["file_type"]}
        gate = month_gate(member["month"])
        if gate:
            files.append({**entry, "status": "refused", "reason": gate, "complete_before_pt": None, "markets": []})
            continue
        where = f"raw:{member['path']}"
        try:
            raw = safe_target(root, sha, member["path"])
        except ArchiveProblem as error:
            raise IndexProblem(f"{where}:{error}") from None
        try:
            if not raw.is_file():
                raise IndexProblem(f"{where}:missing")
            markets, reason, digest, applied = _read_member(raw, limits)
        except OSError:
            raise IndexProblem(f"{where}:unreadable") from None
        if digest != (member["sha256"], member["size_bytes"]):
            raise IndexProblem(f"{where}:member_hash_mismatch")
        ids = sorted(markets, key=market_sort_key)
        files.append({**entry, "status": "unreadable" if reason else "indexed", "reason": reason,
                      "complete_before_pt": applied if reason else None, "markets": ids})
        for market_id in ids:
            market = markets[market_id]
            gates = sorted({g for g in map(month_gate, market.market_time_months()) if g})
            if gates:
                refused_markets.append({"market_id": market_id, "member_path": member["path"],
                                        "reason": f"market_time_{gates[0]}"})
                continue
            if reason:
                market.truncate(applied)
            record = market.record(_provenance(manifest, member), (reason, applied) if reason else None)
            line = canonical_json(record)
            named = member["file_type"] == "market" and member["market_id"] == market_id
            rank = (bool(reason), 0 if named else 1 if member["file_type"] == "event" else 2, member["path"])
            entries.append(_Entry((member["plan"], market_sort_key(market_id)), rank, offset, len(line),
                                  hashlib.sha256(_body(record)).digest(),
                                  hashlib.sha256(canonical_json(record["decision_time"])).digest(),
                                  member["sha256"], member["file_type"]))
            spill.write(line)
            offset += len(line)
    return files, entries, refused_markets


def _merged_lines(spill_path, entries):
    """The primary record of each (plan, market) with other_sources filled in, as canonical lines in index order
    (the primary ranks first: read whole, then a market file named after the market, then an event file, then a
    market file named after another market, then by path)."""
    entries.sort(key=lambda entry: (entry.key, entry.rank))
    with open(spill_path, "rb") as spill:
        for _, group in itertools.groupby(entries, key=lambda entry: entry.key):
            primary, *others = group
            spill.seek(primary.offset)
            record = loads_strict(spill.read(primary.length))
            record["provenance"]["other_sources"] = [
                {"member_path": other.rank[2], "member_sha256": other.member_sha256, "file_type": other.file_type,
                 "read_complete": not other.rank[0], "agrees": other.body == primary.body,
                 "agrees_decision_time": other.decision == primary.decision} for other in others]
            yield canonical_json(record) + b"\n"


def _write_partial(path, lines, where):
    """Write lines to "<path>.partial" (exclusive create); (partial path, sha256, line count)."""
    partial = path.with_name(path.name + ".partial")
    try:
        handle = open(partial, "xb")
    except FileExistsError:
        raise IndexProblem(f"{where}:partial_file_present") from None
    try:
        digest, count = hashlib.sha256(), 0
        with handle:
            for line in lines:
                handle.write(line)
                digest.update(line)
                count += 1
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    return partial, digest.hexdigest(), count


def index_archive(root, archive_sha256, limits=DEFAULT_LIMITS):
    """Index one ingested archive under an already checked data root; a JSON-ready summary, or IndexProblem."""
    manifest, manifest_sha = load_manifest(root, archive_sha256)
    where = f"index:{archive_sha256}"
    try:
        markets_path = safe_path(root, "index", archive_sha256, MARKETS_FILE)
        head_path = safe_path(root, "index", archive_sha256, HEAD_FILE)
        spill_path = safe_path(root, "index", archive_sha256, SPILL_FILE)
    except ArchiveProblem as error:
        raise IndexProblem(f"{where}:{error}") from None
    try:
        markets_path.parent.mkdir(parents=True, exist_ok=True)
        spill = open(spill_path, "xb")
    except FileExistsError:
        raise IndexProblem(f"{where}:partial_file_present") from None
    except OSError:
        raise IndexProblem(f"{where}:write_failed") from None
    partial = None
    try:
        with spill:
            files, entries, refused_markets = _spill(root, manifest, spill, limits)
        partial, markets_sha, count = _write_partial(markets_path, _merged_lines(spill_path, entries), where)
        head = {"schema_version": INDEX_SCHEMA_VERSION, "kind": HEAD_KIND, "archive_sha256": archive_sha256,
                "manifest_sha256": manifest_sha, "markets_file": MARKETS_FILE, "markets_sha256": markets_sha,
                "markets": count, "files": files,
                "refused_markets": sorted(refused_markets, key=lambda item: (item["member_path"], item["market_id"]))}
        head_data = canonical_json(head) + b"\n"
        markets_exist, head_exists = markets_path.exists(), head_path.exists()
        if ((markets_exist and file_sha256(markets_path)[0] != markets_sha)
                or (head_exists and head_path.read_bytes() != head_data)):
            changed = "gate_changed" if manifest["month_gate"] != gate_snapshot() else "index_would_change"
            raise IndexProblem(f"{where}:{changed}")
        if not markets_exist:
            partial.replace(markets_path)
        if not head_exists:
            write_new(head_path, [head_data])
    except ArchiveProblem as error:
        raise IndexProblem(f"{where}:{error}") from None
    except OSError:
        raise IndexProblem(f"{where}:write_failed") from None
    finally:
        spill_path.unlink(missing_ok=True)
        if partial is not None:
            partial.unlink(missing_ok=True)
    counts = {}
    for item in files:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    return {"archive_sha256": archive_sha256, "index_sha256": hashlib.sha256(head_data).hexdigest(),
            "markets_sha256": markets_sha, "head_path": str(head_path), "markets": count,
            "markets_refused": len(refused_markets), "files": len(files), "files_indexed": counts.get("indexed", 0),
            "files_unreadable": counts.get("unreadable", 0), "files_refused": counts.get("refused", 0),
            "state": "unchanged" if markets_exist and head_exists else "written"}


def archive_shas(root):
    """The sha256 of every "<sha256>.json" entry in manifests/ (whatever its type: load_manifest refuses one that is
    not a readable file), sorted; [] when there is no manifests/ yet. IndexProblem manifests:unsafe_target,
    manifests:not_a_directory or manifests:unreadable."""
    folder = root / "manifests"
    try:
        if folder.is_symlink():
            raise IndexProblem("manifests:unsafe_target")
        if not folder.exists():
            return []
        if not folder.is_dir():
            raise IndexProblem("manifests:not_a_directory")
        names = [path.name for path in folder.iterdir()]
    except OSError:
        raise IndexProblem("manifests:unreadable") from None
    return sorted(name[:-5] for name in names if name.endswith(".json") and SHA256_RE.fullmatch(name[:-5]))


def index_archives(data_root, archive=None, limits=DEFAULT_LIMITS):
    """Index one archive (archive: its sha256) or every archive under data_root; a JSON-ready dict with status
    indexed (archives listed), empty (no manifest), or refused (problems; archives indexed before the refusal are
    still listed)."""
    root, problems = check_data_root(data_root)
    if problems:
        return {"status": "refused", "problems": problems}
    if archive is not None and not (isinstance(archive, str) and SHA256_RE.fullmatch(archive)):
        return {"status": "refused", "problems": ["archive:malformed_sha256"]}
    try:
        shas = [archive] if archive is not None else archive_shas(root)
    except IndexProblem as error:
        return {"status": "refused", "problems": [str(error)]}
    done, problems = [], []
    for sha in shas:
        try:
            done.append(index_archive(root, sha, limits))
        except IndexProblem as error:
            problems.append(str(error))
        except OSError as error:
            problems.append(f"index:{sha}:io:{type(error).__name__}")
    if problems:
        return {"status": "refused", "problems": sorted(problems), "archives": done}
    return {"status": "indexed" if shas else "empty", "archives": done}
