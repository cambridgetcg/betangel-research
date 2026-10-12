"""Market book cache for Exchange Stream API market change messages, and book snapshots (I7).

Cache rules (Betfair stream documentation and support articles, F54, accessed 2026-10-10; none
checked against a live stream):
- ct=SUB_IMAGE replaces the whole cache, so markets absent from the image are dropped; img=true on
  one market replaces that market (its definition included).
- Level ladders (batb/batl/bdatb/bdatl) are keyed by level. Size 0 at level k empties level k and
  every deeper level: best-N ladders are contiguous and [0,0,0] means the whole ladder is empty, so
  depth Betfair has withdrawn never resurfaces later. An empty list changes nothing: for bdatb/bdatl
  it means "filtered out by ladderLevels", not "empty".
- A change message with a non-null status (503: "data possibly stale because of latency") marks the
  whole cache unreliable until the next message without one. Meanwhile snapshot() raises
  ValueError("stream_unreliable"). snapshots_from_recording yields every cached market with
  invalid_reason "stream_unreliable" at the message that enters that window (a 503 heartbeat without
  mc, or one that changes another market, included), and the markets later 503 messages change
  (usable for nothing: paper refuses and fills nothing on them, no fill interval spans the window,
  but a change such as in-play is not hidden); at the next healthy message it yields every one of
  them again, so a change carried by a 503 message is never lost when the market does not change
  again.
- Price-point ladders (atb/atl/spb/spl) are keyed by price, size 0 deletes the point and an empty
  list empties the ladder.
- trd is cumulative volume per price. An update that would zero or empty every existing point is the
  documented settlement artefact: it is counted and ignored, because applying it and later seeing
  the volume again would look like a burst of new trading.
- A non-finite size (the bare Infinity token or the string "Infinity") becomes null and the snapshot
  carries invalid_reason "non_finite_size". A non-finite price poisons the runner until the next image.
- Whenever the market is not OPEN, every quote ladder and the last traded price are cleared and
  quote deltas are not stored, so no price is forward-filled through a suspension. Whether Betfair
  re-sends unchanged price points after a reopen is unverified, so a full-depth (atb/atl) market
  that was cleared that way, or whose atb/atl points were dropped because they arrived while it was
  not OPEN (an image taken during a suspension included), stays marked partial until its next
  image: its OPEN snapshots carry invalid_reason "ladder_partial_after_suspension" (an interior
  price not re-sent would otherwise read as empty, and paper would take its queue as zero). Level
  ladders need no mark: their contiguous prefix already stops at the first level not re-sent.
- A market sent twice in one image keeps the copy with the higher marketDefinition.version.
  Otherwise version is not assumed to be monotonic (F26), and a definition is replaced whole.

Snapshots report non-virtual ladders only: atb/atl when the market carries full depth, else
batb/batl, as the contiguous prefix from level 0 that is strictly ordered by price. Only handicap-0
runners from the market definition are reported (instruction v1 supports handicap 0 only).
"""

import copy
import math
from pathlib import Path
import re

from .. import stream as _stream
from ..temporal import timestamp_ms
from .constants import MARKET_STATUSES
from .receipts import parse_utc_ns, strict_json_loads, utc_from_ns
from .schemas import SNAPSHOT_KEY_TYPES, validate_book_snapshot

SOURCES = {"stream_cache": "stream_cache", "recording": "recording"}     # source -> fetched_by
_LEVEL_FIELDS = ("batb", "batl", "bdatb", "bdatl")
_PRICE_FIELDS = ("atb", "atl", "spb", "spl")
_NON_FINITE_TEXT = frozenset({"Infinity", "-Infinity", "NaN"})
_MAX_LEVEL = 9
COUNTER_KEYS = ("settlement_zeroing_seen", "non_finite_seen", "duplicate_definition_dropped",
                "deltas_dropped_while_not_open", "handicap_runners_omitted", "ladder_prefix_truncations",
                "stream_unreliable_seen")
# The shape temporal.timestamp_ms parses on every supported Python: 3.10's fromisoformat takes a
# fraction of exactly 3 or 6 digits (or none), where receipts.parse_utc_ns also takes 1-6.
_REPLAY_TIME_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{3}|\.[0-9]{6})?"
                             r"(?:Z|[+-][0-9]{2}:[0-9]{2})")


def replay_market_time(value):
    """value when the replay readers (stream.replay_snapshots, microstructure, via temporal.timestamp_ms)
    accept it as a whole-millisecond UTC time on Python 3.10 and later; else None."""
    if not isinstance(value, str) or not _REPLAY_TIME_RE.fullmatch(value):
        return None
    try:
        if parse_utc_ns(value) % 1_000_000:
            return None
        timestamp_ms(value)
    except ValueError:
        return None
    return value


def ladder_type(market_definition):
    """priceLadderDefinition as an object {"type": ...} (Swagger) or a bare string (doc table); else None."""
    if not isinstance(market_definition, dict):
        return None
    value = market_definition.get("priceLadderDefinition")
    if isinstance(value, dict):
        value = value.get("type")
    return value if isinstance(value, str) and value else None


def _is_number(value):
    return not isinstance(value, bool) and isinstance(value, (int, float))


def _finite_float(value):
    """float(value) when finite, else None; ValueError for anything that is not a number."""
    if isinstance(value, str) and value in _NON_FINITE_TEXT:
        return None
    if not _is_number(value):
        raise ValueError("stream number expected")
    try:
        result = float(value)
    except OverflowError:
        return None
    return result if math.isfinite(result) else None


def _new_runner():
    runner = {field: {} for field in _LEVEL_FIELDS + _PRICE_FIELDS}
    runner.update(trd=None, ltp=None, tv=None, tv_non_finite=False, poisoned=False)
    return runner


def _new_market():
    return {"definition": None, "runners": {}, "pt": None, "observed_at": None, "monotonic_ns": None,
            "full_depth": False, "depth_partial": False}


def _runner_key(runner_id, handicap):
    """(selection id as text, handicap as float). Ids are int64 in the stream; digit strings are accepted."""
    if type(runner_id) is int and runner_id > 0:
        selection = str(runner_id)
    elif isinstance(runner_id, str) and runner_id.isascii() and runner_id.isdigit() and int(runner_id) > 0:
        selection = str(int(runner_id))
    else:
        raise ValueError("runner id must be a positive integer")
    if handicap is None:
        return selection, 0.0
    value = _finite_float(handicap)
    if value is None:
        raise ValueError("runner handicap must be a finite number")
    return selection, value


class MarketBookCache:
    """Applies mcm messages in order and builds validated book snapshots."""

    def __init__(self, *, key_type, app_key_slot, source):
        if key_type not in SNAPSHOT_KEY_TYPES:
            raise ValueError("key_type must be delayed, live, historical or synthetic")
        if app_key_slot not in ("delayed", "live", None):
            raise ValueError("app_key_slot must be delayed, live or None")
        if source not in SOURCES:
            raise ValueError("source must be stream_cache or recording")
        self.key_type = key_type
        self.app_key_slot = app_key_slot
        self.source = source
        self._markets = {}
        self._last_pt = None
        self._unreliable = False
        self.counters = {key: 0 for key in COUNTER_KEYS}

    def markets(self):
        return sorted(self._markets)

    @property
    def stream_unreliable(self):
        """True while the latest change message carried a status (503): the cache may be stale."""
        return self._unreliable

    def has_definition(self, market_id):
        market = self._markets.get(market_id)
        return market is not None and market["definition"] is not None

    # --- applying messages --------------------------------------------------------------------

    def apply(self, msg, *, received_at=None, monotonic_ns=None):
        """Apply one change message; returns the ids of markets it changed (first-seen order).

        Non-mcm messages change nothing. pt must be an int that does not decrease, except that a
        SUB_IMAGE starts afresh. received_at (local receipt time) becomes the snapshot's
        observed_at; without it observed_at is the publish time pt.
        """
        if not isinstance(msg, dict):
            raise ValueError("stream message must be an object")
        if msg.get("op") != "mcm":
            return []
        pt = msg.get("pt")
        if type(pt) is not int or pt < 0:
            raise ValueError("mcm pt must be a non-negative integer")
        if msg.get("segmentType") is not None:
            raise ValueError("segmented stream messages are not supported")
        if received_at is not None:
            parse_utc_ns(received_at)
        if monotonic_ns is not None and (type(monotonic_ns) is not int or monotonic_ns < 0):
            raise ValueError("monotonic_ns must be a non-negative integer")
        changes = msg.get("mc")
        if changes is None:
            changes = []
        if not isinstance(changes, list) or any(not isinstance(c, dict) for c in changes):
            raise ValueError("mc must be a list of market changes")
        image = msg.get("ct") == "SUB_IMAGE"
        if not image and self._last_pt is not None and pt < self._last_pt:
            raise ValueError("mcm pt decreased")
        if image:
            self._markets.clear()
        self._last_pt = pt
        # status is null when the stream is healthy and 503 when its data may be stale (F50); any
        # other non-null value is unknown and treated the same way.
        self._unreliable = msg.get("status") is not None
        if self._unreliable:
            self.counters["stream_unreliable_seen"] += 1
        keep = self._image_copies_to_keep(changes)
        observed_at = received_at if received_at is not None else utc_from_ns(pt * 1_000_000)
        changed = []
        for index, change in enumerate(changes):
            market_id = change.get("id")
            if not isinstance(market_id, str) or not market_id:
                raise ValueError("market change id must be a non-empty string")
            if change.get("img") is True and keep.get(market_id, index) != index:
                self.counters["duplicate_definition_dropped"] += 1
                continue
            self._apply_market(market_id, change)
            market = self._markets[market_id]
            market["pt"] = pt
            market["observed_at"] = observed_at
            market["monotonic_ns"] = monotonic_ns
            if market_id not in changed:
                changed.append(market_id)
        return changed

    @staticmethod
    def _image_copies_to_keep(changes):
        """market id -> index of the img=true copy to keep (highest definition version; last on a tie)."""
        best = {}
        for index, change in enumerate(changes):
            if change.get("img") is not True:
                continue
            definition = change.get("marketDefinition")
            version = definition.get("version") if isinstance(definition, dict) else None
            rank = version if type(version) is int else -1
            current = best.get(change.get("id"))
            if current is None or rank >= current[0]:
                best[change.get("id")] = (rank, index)
        return {market_id: index for market_id, (_, index) in best.items()}

    def _apply_market(self, market_id, change):
        if change.get("img") is True or market_id not in self._markets:
            self._markets[market_id] = _new_market()
        market = self._markets[market_id]
        definition = change.get("marketDefinition")
        if definition is not None:
            if not isinstance(definition, dict):
                raise ValueError("marketDefinition must be an object")
            market["definition"] = copy.deepcopy(definition)
        is_open = market["definition"] is not None and market["definition"].get("status") == "OPEN"
        if not is_open:
            # Never carry quotes through a suspension, closure or an unknown status.
            if market["full_depth"]:
                market["depth_partial"] = True          # until the next image of this market
            for runner in market["runners"].values():
                for field in _LEVEL_FIELDS + _PRICE_FIELDS:
                    runner[field].clear()
                runner["ltp"] = None
        runner_changes = change.get("rc")
        if runner_changes is None:
            return
        if not isinstance(runner_changes, list) or any(not isinstance(rc, dict) for rc in runner_changes):
            raise ValueError("rc must be a list of runner changes")
        for rc in runner_changes:
            key = _runner_key(rc.get("id"), rc.get("hc"))
            runner = market["runners"].setdefault(key, _new_runner())
            self._apply_runner(market, runner, rc, is_open)

    def _apply_runner(self, market, runner, rc, is_open):
        quote_fields = [f for f in _LEVEL_FIELDS + _PRICE_FIELDS if rc.get(f) is not None]
        if is_open:
            for field in quote_fields:
                if field in ("atb", "atl"):
                    market["full_depth"] = True
                if field in _LEVEL_FIELDS:
                    self._apply_levels(runner, field, rc[field])
                else:
                    self._apply_points(runner, field, rc[field])
            if "ltp" in rc and rc["ltp"] is not None:
                runner["ltp"] = self._ltp(rc["ltp"])
        elif quote_fields or rc.get("ltp") is not None:
            self.counters["deltas_dropped_while_not_open"] += 1
            if "atb" in quote_fields or "atl" in quote_fields:
                # Price points the stream now counts as sent were dropped: the full-depth ladder rebuilt
                # after the reopen may lack interior prices until the next image.
                market["depth_partial"] = True
        if rc.get("trd") is not None:
            self._apply_traded(runner, rc["trd"])
        if rc.get("tv") is not None:
            value = _finite_float(rc["tv"])
            if value is None:
                self.counters["non_finite_seen"] += 1
                runner["tv"], runner["tv_non_finite"] = None, True
            elif value < 0:
                raise ValueError("runner tv must be non-negative")
            else:
                runner["tv"], runner["tv_non_finite"] = value, False

    def _ltp(self, value):
        price = _finite_float(value)
        if price is None:
            self.counters["non_finite_seen"] += 1
            return None
        return price if price > 1 else None

    def _size(self, value):
        size = _finite_float(value)
        if size is None:
            self.counters["non_finite_seen"] += 1
        elif size < 0:
            raise ValueError("ladder size must be non-negative")
        return size

    def _quote_price(self, runner, value, size):
        """A finite price above 1, or None after poisoning the runner (non-finite price)."""
        price = _finite_float(value)
        if price is None:
            self.counters["non_finite_seen"] += 1
            runner["poisoned"] = True
            return None
        if price <= 1:
            raise ValueError("a non-empty ladder price must exceed 1")
        return price

    def _apply_levels(self, runner, field, updates):
        if not isinstance(updates, list):
            raise ValueError(f"{field} must be a list of [level, price, size]")
        ladder = runner[field]
        for update in updates:
            if not isinstance(update, list) or len(update) != 3:
                raise ValueError(f"{field} updates must be [level, price, size]")
            level, price, size = update
            if type(level) is not int or not 0 <= level <= _MAX_LEVEL:
                raise ValueError(f"{field} level must be an integer from 0 to {_MAX_LEVEL}")
            size = self._size(size)
            if size == 0:
                # Contiguous best-N ladder: an empty level k leaves nothing at k or deeper.
                for stale in [k for k in ladder if k >= level]:
                    del ladder[stale]
                continue
            price = self._quote_price(runner, price, size)
            if price is None:
                ladder.pop(level, None)
                continue
            ladder[level] = [price, size]

    def _apply_points(self, runner, field, updates):
        if not isinstance(updates, list):
            raise ValueError(f"{field} must be a list of [price, size]")
        ladder = runner[field]
        if not updates:
            ladder.clear()          # price-point ladders: an empty list means the ladder is empty
            return
        for update in updates:
            if not isinstance(update, list) or len(update) != 2:
                raise ValueError(f"{field} updates must be [price, size]")
            price, size = update
            size = self._size(size)
            if size == 0:
                if _is_number(price):
                    ladder.pop(float(price), None)
                continue
            price = self._quote_price(runner, price, size)
            if price is not None:
                ladder[price] = size

    def _apply_traded(self, runner, updates):
        if not isinstance(updates, list):
            raise ValueError("trd must be a list of [price, size]")
        existing = runner["trd"]
        parsed = []
        for update in updates:
            if not isinstance(update, list) or len(update) != 2:
                raise ValueError("trd updates must be [price, size]")
            parsed.append(update)
        if existing and not parsed:
            # An empty price-point list would empty the cumulative traded ladder: the same artefact.
            self.counters["settlement_zeroing_seen"] += 1
            return
        if existing:
            zeroed = set()
            all_zero = True
            for price, size in parsed:
                if _is_number(size) and size == 0 and _is_number(price):
                    zeroed.add(float(price))
                else:
                    all_zero = False
            if all_zero and set(existing) <= zeroed:
                self.counters["settlement_zeroing_seen"] += 1
                return
        if existing is None:
            runner["trd"] = existing = {}
        for price, size in parsed:
            size = self._size(size)
            if size == 0:
                if _is_number(price):
                    existing.pop(float(price), None)
                continue
            price = self._quote_price(runner, price, size)
            if price is not None:
                existing[price] = size

    # --- snapshots ----------------------------------------------------------------------------

    def snapshot(self, market_id, *, unreliable_ok=False, stamp=None):
        """Book snapshot (schema I7, validated). ValueError if the market is unknown, its definition
        lacks the fields a snapshot needs, or the stream is flagged unreliable (status 503). With
        unreliable_ok (recordings only), a flagged stream gives a snapshot with invalid_reason
        "stream_unreliable" instead, which no consumer may trade on. stamp ({"pt", "observed_at",
        "monotonic_ns"} of the current message, recordings only) dates a snapshot of a market that
        message did not change at that message, not at the market's own last change."""
        if self._unreliable and not unreliable_ok:
            raise ValueError("stream_unreliable")
        market = self._markets.get(market_id)
        if market is None:
            raise ValueError("unknown_market")
        md = market["definition"]
        if md is None:
            raise ValueError("market_definition_missing")
        status = md.get("status")
        if status not in MARKET_STATUSES:
            raise ValueError("market_status_missing_or_unknown")
        if type(md.get("inPlay")) is not bool:
            raise ValueError("market_in_play_missing")
        bet_delay = md.get("betDelay")
        if type(bet_delay) is not int or bet_delay < 0:
            raise ValueError("market_bet_delay_missing")
        version = md.get("version")
        winners = md.get("numberOfWinners")
        event_type_id = md.get("eventTypeId")
        if type(event_type_id) is int and event_type_id >= 0:
            event_type_id = str(event_type_id)
        runners = {}
        non_finite = False
        definitions = md.get("runners")
        if not isinstance(definitions, list):
            definitions = []
        for definition in definitions:
            if not isinstance(definition, dict):
                raise ValueError("runner definitions must be objects")
            key = _runner_key(definition.get("id"), definition.get("hc"))
            if key[1] != 0:
                self.counters["handicap_runners_omitted"] += 1
                continue
            status_text = definition.get("status")
            state = market["runners"].get(key) or _new_runner()
            entry, bad = self._runner_snapshot(market, state)
            entry["status"] = status_text if isinstance(status_text, str) and status_text else "UNKNOWN"
            runners[key[0]] = entry
            non_finite = non_finite or bad
        when = stamp if stamp is not None else market
        snapshot = {
            "market_id": market_id, "source": self.source, "fetched_by": SOURCES[self.source],
            "key_type": self.key_type, "app_key_slot": self.app_key_slot, "observed_at": when["observed_at"],
            "monotonic_ns": when["monotonic_ns"], "pt": when["pt"], "status": status, "inplay": md["inPlay"],
            "version": version if type(version) is int and version >= 0 else None, "bet_delay": bet_delay,
            "is_market_data_delayed": None,      # a REST MarketBook field; the stream does not carry it
            "catalogue_present": True,           # the stream's marketDefinition carries the catalogue fields
            "market_time": self._market_time(md.get("marketTime")),
            "number_of_winners": winners if type(winners) is int and winners >= 0 else None,
            "betting_type": self._text(md.get("bettingType")), "market_type": self._text(md.get("marketType")),
            "event_type_id": self._text(event_type_id), "price_ladder_type": ladder_type(md),
            "invalid_reason": "stream_unreliable" if self._unreliable else "non_finite_size" if non_finite else (
                "ladder_partial_after_suspension" if market["full_depth"] and market["depth_partial"]
                and status == "OPEN" else None),
            "runners": runners,
        }
        return validate_book_snapshot(snapshot)

    @staticmethod
    def _text(value):
        return value if isinstance(value, str) and value else None

    @staticmethod
    def _market_time(value):
        """marketTime as published, when the replay readers parse it on every supported Python."""
        return replay_market_time(value)

    def _runner_snapshot(self, market, state):
        if market["full_depth"]:
            back = [[p, s] for p, s in sorted(state["atb"].items(), reverse=True)]
            lay = [[p, s] for p, s in sorted(state["atl"].items())]
        else:
            back = self._level_prefix(state["batb"], descending=True)
            lay = self._level_prefix(state["batl"], descending=False)
        traded = None
        if state["trd"] is not None:
            traded = {repr(p): s for p, s in sorted(state["trd"].items())}
        bad = (state["poisoned"] or state["tv_non_finite"]
               or any(s is None for field in _LEVEL_FIELDS for _, s in state[field].values())
               or any(s is None for field in _PRICE_FIELDS for s in state[field].values())
               or (state["trd"] is not None and any(s is None for s in state["trd"].values())))
        entry = {"status": None, "handicap": 0.0, "ltp": state["ltp"], "back": back, "lay": lay,
                 "traded": traded, "total_matched": state["tv"]}
        return entry, bad

    def _level_prefix(self, levels, *, descending):
        out = []
        level = 0
        while level in levels:
            price, size = levels[level]
            if out and (price >= out[-1][0] if descending else price <= out[-1][0]):
                break
            out.append([price, size])
            level += 1
        if len(out) != len(levels):
            self.counters["ladder_prefix_truncations"] += 1
        return out


def recv_sidecar_name(path):
    """The file name of a recorder segment's receipt sidecar (seg-NNN.recv.jsonl for seg-NNN.jsonl), whether
    or not it exists, or None when path is not a segment name."""
    name = Path(path).name
    if not name.endswith(".jsonl") or name.endswith(".recv.jsonl"):
        return None
    return name[:-len(".jsonl")] + ".recv.jsonl"


def _recv_rows(path):
    """The receipt sidecar of a recorder segment (seg-NNN.recv.jsonl next to seg-NNN.jsonl), or None."""
    name = recv_sidecar_name(path)
    if name is None:
        return None
    sidecar = Path(path).with_name(name)
    return sidecar if sidecar.is_file() else None


def snapshots_from_recording(path, *, key_type):
    """(pt, snapshot) for every market changed by each message, read through stream.messages().

    observed_at and monotonic_ns come from the recorder's receipt sidecar when one sits next to the
    segment (row n belongs to line n, checked by pt); without one (historical files) observed_at is
    the publish time pt, the only clock available. Markets without a definition yet are skipped.
    A status 503 (data possibly stale) is about the whole stream, not only the markets its message
    carries (amendment A3, C3): the message that enters the unreliable window, with or without mc,
    yields every cached market with invalid_reason "stream_unreliable" at its pt, so nothing trades
    or fills on any of them and no fill interval spans the window; later 503 messages yield the
    markets they change the same way, so a change (in-play, a suspension, a removal) is seen at
    once. At the first healthy message after the window every one of those markets is yielded
    again at that message's pt, now usable, before that message's own changes, whether or not that
    message changes it.
    """
    slot = key_type if key_type in ("delayed", "live") else None
    cache = MarketBookCache(key_type=key_type, app_key_slot=slot, source="recording")
    sidecar = _recv_rows(path)
    handle = sidecar.open("r", encoding="utf-8") if sidecar is not None else None
    pending = []          # markets yielded unusable in the current unreliable window, in first-seen order
    unreliable = False    # whether the previous message left the stream flagged unreliable
    try:
        for msg in _stream.messages(path):
            received_at = monotonic_ns = None
            if handle is not None:
                text = handle.readline()
                if not text:
                    raise ValueError("receipt sidecar is shorter than the segment")
                row = strict_json_loads(text)
                if not isinstance(row, dict) or row.get("pt") != msg["pt"]:
                    raise ValueError("receipt sidecar does not match the segment")
                received_at, monotonic_ns = row.get("received_at"), row.get("monotonic_ns")
            changed = cache.apply(msg, received_at=received_at, monotonic_ns=monotonic_ns)
            stamp = {"pt": msg["pt"], "monotonic_ns": monotonic_ns,
                     "observed_at": received_at if received_at is not None else utc_from_ns(msg["pt"] * 1_000_000)}
            if cache.stream_unreliable:
                affected = changed if unreliable else changed + [m for m in cache.markets() if m not in changed]
                unreliable = True
                for market_id in affected:
                    if market_id not in pending:
                        pending.append(market_id)
                    if cache.has_definition(market_id):
                        yield msg["pt"], cache.snapshot(market_id, unreliable_ok=True, stamp=stamp)
                continue
            unreliable = False
            catch_up, pending = [m for m in pending if m not in changed], []
            for market_id in catch_up:
                if cache.has_definition(market_id):
                    yield msg["pt"], cache.snapshot(market_id, stamp=stamp)
            for market_id in changed:
                if cache.has_definition(market_id):
                    yield msg["pt"], cache.snapshot(market_id)
        if handle is not None and handle.readline():
            raise ValueError("receipt sidecar is longer than the segment")
    finally:
        if handle is not None:
            handle.close()
