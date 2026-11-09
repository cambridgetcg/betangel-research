#!/usr/bin/env python3
"""Stage 0 rate count for Pilot 2, registered in docs/lp-rate-count.md: first merged at 9e69b73 before any recording
was read, and amended three times before any qualifying count was printed (amendment 1, 2026-11-04; amendment 2,
2026-11-05, no code change; amendment 3, 2026-11-09, the plain bot's decision time, off -590 s).

It counts how often GB/IE horse-racing WIN markets in the #41 recorder's delayed-key recordings show the spreads
Pilot 2 would quote in, per racing day (UTC date of the off) and pooled, and prints Gate A. It decides nothing else.
Gate A is fixed by the data, not by when the script runs: it pools the racing days up to the first UTC date on
which the running totals reach 10 racing days and 300 usable markets, and is read only once that date's recording
has ended. Until then the script prints only how many markets were recorded and usable, never what qualified.

Offline and read only. It reads the session directories (rec-*) under the one recordings root it is given that
started at or after 2026-11-03 23:05:00 UTC (amendment 1: none from before that first merge), and in each only
the closed segments its receipt lists (sha256 checked), plus its own bytes for the sha256 it prints. It writes no
file. Output is counts, rates, UTC dates and that sha256 only: never a market id, selection id, event name, venue,
price or time of day. Errors print a fixed code.

Limits: delayed (1-180 s) and conflated (180 s) data, best three levels, no virtual prices. The count is an upper
bound, not live recall.

Standard library only. Scripts may not import betangel_research.betfair (tests/test_betfair_boundaries.py), so the
line reader, the best-offer fold and the CLASSIC ladder below are small copies of betangel_research.stream.messages,
betfair.book.MarketBookCache (batb/batl, definitions, status, 503) and betfair.ticks; tests/test_lp_rate_count.py
checks each against the original.

Run with: python3 -I -B scripts/lp_rate_count.py --root <recordings dir> [--json]
"""

import argparse
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import heapq
import json
import math
from pathlib import Path
import re
import stat
import sys

SCHEMA = "lp-rate-count/v3"
REGISTRATION = "docs/lp-rate-count.md"

# --- the registered count (docs/lp-rate-count.md); changing any of these is a new registration ---------------
DECISION_S = -590                           # amendment 3: the plain bot decides from off -590 s
PERSISTENCE_S = -780                        # amendment 3: the end of the plain bot's delayed screen
WINDOW_S = (-600, -300)                     # inclusive at both ends
OFFSETS_S = (WINDOW_S[0], PERSISTENCE_S, DECISION_S)
MAX_AGE_MS = 180_000                        # usable only if the data is no older than this at off + X
CONFLATE_MS = 180_000                       # the conflation Betfair must have echoed for the session
COUNTRIES = ("GB", "IE")
EVENT_TYPE = "7"
LADDER_TYPE = "CLASSIC"
MIN_ACTIVE = 5
PRIMARY_BACK_H = (150, 590)                 # 1.50 <= best back <= 5.90, in hundredths
PRICE_LEVER_BACK_H = (600, 1000)            # 6.0 <= best back <= 10.0
PRIMARY_TICKS = (3, 4)                      # best lay exactly 3 or 4 CLASSIC ticks above best back
WIDTH_LEVER_TICKS = (5, 7)
GATE_MIN_DAYS = 10
GATE_MIN_USABLE = 300                       # usable markets at DECISION_S, pooled (Yu, 2026-11-03)
GATE_PER_MILLE = 14                         # 1.4% of markets usable at DECISION_S

# --- what a session must be to be read -------------------------------------------------------------------------
REQUIRED_FIELDS = ("EX_BEST_OFFERS", "EX_MARKET_DEF")
VIRTUAL_FIELD = "EX_BEST_OFFERS_DISP"
MAX_LADDER_LEVELS = 3
SESSION_RE = re.compile(r"rec-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}")
FIRST_START = "20261103T230500Z"            # UTC start in the name; earlier sessions are not read (amendment 1)
SEGMENT_RE = re.compile(r"seg-[0-9]{3,6}\.jsonl")
SHA256_RE = re.compile(r"[0-9a-f]{64}")

USABLE = "usable"
NOT_USABLE = ("no_snapshot", "invalid", "out_of_population", "not_open_or_in_play", "under_5_active")
SESSION_SKIPS = ("recorded_before_registration", "not_a_directory", "no_receipt", "receipt_unreadable",
                 "not_delayed_key", "conflation_not_registered", "subscription_not_registered")
SEGMENT_SKIPS = ("missing", "unreadable", "hash_mismatch", "replay_check_not_passed")
COLUMNS = ("recorded", "usable", "primary", "flagged", "persistence", "persistence_two_batches", "window",
           "price_lever", "width_lever")
RATE_COLUMNS = COLUMNS[2:]                  # every one a subset of usable; withheld until Gate A can be read
TABLE_COLUMNS = (("primary", "primary"), ("persistence", "persistence"), ("window", "window"),
                 ("price_lever", "price lever"), ("width_lever", "width lever"))
GATE_RESULTS = ("PASS", "FAIL", "NOT_ENOUGH_DAYS", "NOT_ENOUGH_MARKETS", "RECORDING_OPEN")
DECIDED = ("PASS", "FAIL")
DAY_DONE_STOPS = ("end_at_reached", "max_duration_reached", "max_seconds_reached")   # recording_day.DONE_STOPS
LIMITS = ("delayed 1-180 s and conflated to 180 s, best 3 levels, no virtual prices: "
          "an upper bound, not live recall")

# --- CLASSIC ladder (copy of constants.CLASSIC_BANDS and ticks.LADDER_CLASSIC), integer hundredths -------------
CLASSIC_BANDS = ((101, 200, 1), (200, 300, 2), (300, 400, 5), (400, 600, 10), (600, 1000, 20),
                 (1000, 2000, 50), (2000, 3000, 100), (3000, 5000, 200), (5000, 10000, 500), (10000, 100000, 1000))
LADDER = tuple(price for low, high, step in CLASSIC_BANDS for price in range(low, high, step)) + (100000,)
TICK = {price: index for index, price in enumerate(LADDER)}
if len(LADDER) != 350 or any(b <= a for a, b in zip(LADDER, LADDER[1:])):
    raise RuntimeError("CLASSIC ladder bands do not give 350 ascending ticks")

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_TIME_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{3}|\.[0-9]{6})?"
                      r"(?:Z|[+-][0-9]{2}:[0-9]{2})")
_NON_FINITE_TEXT = frozenset({"Infinity", "-Infinity", "NaN"})
_MAX_LEVEL = 9


class Refused(Exception):
    """A fixed refusal code; nothing else about the input is printed."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


# --- prices and times ------------------------------------------------------------------------------------------

def hundredths(value):
    """A stream price as int hundredths when it is exactly on the CLASSIC ladder, else None (as ticks.to_hundredths
    plus is_on_ladder: Decimal(repr(x)), so 2.98 is 298 exactly; more than 2 dp is off the ladder)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        try:
            scaled = Decimal(repr(value)) * 100
        except InvalidOperation:
            return None
        if scaled != scaled.to_integral_value():
            return None
        price = int(scaled)
    else:
        price = value * 100
    return price if price in TICK else None


def spread_ticks(back_h, lay_h):
    return TICK[lay_h] - TICK[back_h]


def runner_flags(back, lay):
    """(primary, price lever, width lever) for one ACTIVE runner's best back and best lay (stream prices or None)."""
    b, l = hundredths(back), hundredths(lay)
    if b is None or l is None or b >= l:
        return False, False, False
    ticks = spread_ticks(b, l)
    in_band = PRIMARY_BACK_H[0] <= b <= PRIMARY_BACK_H[1]
    narrow = PRIMARY_TICKS[0] <= ticks <= PRIMARY_TICKS[1]
    return (in_band and narrow,
            PRICE_LEVER_BACK_H[0] <= b <= PRICE_LEVER_BACK_H[1] and narrow,
            in_band and WIDTH_LEVER_TICKS[0] <= ticks <= WIDTH_LEVER_TICKS[1])


def market_time_ms(value):
    """A published marketTime as epoch ms (whole ms, 0/3/6 fraction digits, as book.replay_market_time), else None."""
    if not isinstance(value, str) or not _TIME_RE.fullmatch(value):
        return None
    try:
        moment = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError:
        return None
    if moment.microsecond % 1000:
        return None
    delta = moment - _EPOCH
    return (delta.days * 86400 + delta.seconds) * 1000 + delta.microseconds // 1000


def utc_date(ms):
    return (_EPOCH + timedelta(milliseconds=ms)).date().isoformat()


# --- reader and fold -------------------------------------------------------------------------------------------

def messages(text):
    """mcm messages of one segment, with stream.messages()'s rules: blank and non-mcm lines skipped, pt an int that
    never decreases, segmented messages refused."""
    previous = -1
    for line in text.split("\n"):
        if not line.strip():
            continue
        msg = json.loads(line)
        if not isinstance(msg, dict):
            raise ValueError("line is not an object")
        if msg.get("op") != "mcm":
            continue
        pt = msg.get("pt")
        if isinstance(pt, bool) or not isinstance(pt, int) or pt < previous:
            raise ValueError("invalid or decreasing pt")
        if msg.get("segmentType") is not None:
            raise ValueError("segmented stream messages are not supported")
        previous = pt
        yield msg


def _finite(value):
    """float(value) when finite, None when not (book._finite_float); ValueError for anything not a number."""
    if isinstance(value, str) and value in _NON_FINITE_TEXT:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("stream number expected")
    try:
        result = float(value)
    except OverflowError:
        return None
    return result if math.isfinite(result) else None


def runner_key(runner_id, handicap):
    """(selection id as text, handicap as float), as book._runner_key."""
    if type(runner_id) is int and runner_id > 0:
        selection = str(runner_id)
    elif isinstance(runner_id, str) and runner_id.isascii() and runner_id.isdigit() and int(runner_id) > 0:
        selection = str(int(runner_id))
    else:
        raise ValueError("runner id must be a positive integer")
    if handicap is None:
        return selection, 0.0
    value = _finite(handicap)
    if value is None:
        raise ValueError("runner handicap must be a finite number")
    return selection, value


class BestOffers:
    """The parts of book.MarketBookCache the count needs, for one segment.

    - ct=SUB_IMAGE clears every market; img=true replaces that market, definition included (the copy with the
      highest definition version when a market is sent twice in one image). A definition is replaced whole.
    - batb/batl are keyed by level; size 0 at level k empties k and every deeper level. Quote deltas are dropped
      while the market is not OPEN, and every ladder is cleared whenever it is not OPEN.
    - A message with a non-null status (503) leaves the whole fold unreliable until the next message without one.
    - A non-finite number in any runner's batb, batl, tv or trd (REMOVED runners included) marks the market invalid
      until its next image, which can be the next segment, up to 30 minutes later. book.py marks only the snapshots
      that still hold such a value; this stricter rule can only remove snapshots, and the 'invalid' count shows how
      many.
    """

    def __init__(self):
        self.markets = {}
        self.unreliable = False

    def apply(self, msg):
        """Apply one mcm message; returns [(market id, its new definition or None)] in order."""
        changes = msg.get("mc")
        if changes is None:
            changes = []
        if not isinstance(changes, list) or any(not isinstance(c, dict) for c in changes):
            raise ValueError("mc must be a list of market changes")
        if msg.get("ct") == "SUB_IMAGE":
            self.markets.clear()
        self.unreliable = msg.get("status") is not None
        keep = {}
        for index, change in enumerate(changes):
            if change.get("img") is True:
                definition = change.get("marketDefinition")
                version = definition.get("version") if isinstance(definition, dict) else None
                rank = version if type(version) is int else -1
                current = keep.get(change.get("id"))
                if current is None or rank >= current[0]:
                    keep[change.get("id")] = (rank, index)
        applied = []
        for index, change in enumerate(changes):
            market_id = change.get("id")
            if not isinstance(market_id, str) or not market_id:
                raise ValueError("market change id must be a non-empty string")
            if change.get("img") is True and keep[market_id][1] != index:
                continue
            applied.append((market_id, self._apply_market(market_id, change)))
        return applied

    def _apply_market(self, market_id, change):
        if change.get("img") is True or market_id not in self.markets:
            self.markets[market_id] = {"definition": None, "runners": {}, "tainted": False}
        market = self.markets[market_id]
        definition = change.get("marketDefinition")
        if definition is not None:
            if not isinstance(definition, dict):
                raise ValueError("marketDefinition must be an object")
            market["definition"] = definition
        is_open = market["definition"] is not None and market["definition"].get("status") == "OPEN"
        if not is_open:
            for runner in market["runners"].values():
                runner["batb"].clear()
                runner["batl"].clear()
        runner_changes = change.get("rc")
        if runner_changes is None:
            return definition
        if not isinstance(runner_changes, list) or any(not isinstance(rc, dict) for rc in runner_changes):
            raise ValueError("rc must be a list of runner changes")
        for rc in runner_changes:
            runner = market["runners"].setdefault(runner_key(rc.get("id"), rc.get("hc")), {"batb": {}, "batl": {}})
            if is_open:
                for field in ("batb", "batl"):
                    if rc.get(field) is not None:
                        self._levels(market, runner[field], rc[field])
            if rc.get("tv") is not None and _finite(rc["tv"]) is None:
                market["tainted"] = True
            traded = rc.get("trd")
            if isinstance(traded, list):
                for item in traded:
                    if isinstance(item, list) and len(item) == 2 and any(_finite(x) is None for x in item):
                        market["tainted"] = True
        return definition

    @staticmethod
    def _levels(market, ladder, updates):
        if not isinstance(updates, list):
            raise ValueError("level ladder must be a list of [level, price, size]")
        for update in updates:
            if not isinstance(update, list) or len(update) != 3:
                raise ValueError("level updates must be [level, price, size]")
            level, price, size = update
            if type(level) is not int or not 0 <= level <= _MAX_LEVEL:
                raise ValueError("level must be an integer from 0 to 9")
            size = _finite(size)
            if size is None:
                market["tainted"] = True
            elif size < 0:
                raise ValueError("ladder size must be non-negative")
            if size == 0:
                for stale in [k for k in ladder if k >= level]:
                    del ladder[stale]
                continue
            price = _finite(price)
            if price is None:
                market["tainted"] = True
                ladder.pop(level, None)
                continue
            if price <= 1:
                raise ValueError("a non-empty ladder price must exceed 1")
            ladder[level] = (price, size)

    def best(self, market_id, selection):
        """(best back price, best lay price) of a handicap-0 runner, each None when empty."""
        runner = self.markets[market_id]["runners"].get((selection, 0.0))
        if runner is None:
            return None, None
        back, lay = runner["batb"].get(0), runner["batl"].get(0)
        return (back[0] if back else None), (lay[0] if lay else None)

    def evaluate(self, market_id):
        """(code, primary, price lever, width lever) of the market's current state; code is 'usable' only when the
        market is in the population (GB/IE, event type 7, WIN, ODDS, one winner, CLASSIC ladder, OPEN, not in play,
        >= 5 ACTIVE)."""
        no = (False, False, False)
        market = self.markets.get(market_id)
        if market is None or market["definition"] is None:
            return ("no_snapshot",) + no
        if self.unreliable or market["tainted"]:
            return ("invalid",) + no
        md = market["definition"]
        event_type = md.get("eventTypeId")
        if type(event_type) is int:
            event_type = str(event_type)
        winners = md.get("numberOfWinners")
        ladder = md.get("priceLadderDefinition")         # an object {"type": ...} or a bare string, as book.ladder_type
        if isinstance(ladder, dict):
            ladder = ladder.get("type")
        if (md.get("countryCode") not in COUNTRIES or event_type != EVENT_TYPE or md.get("marketType") != "WIN"
                or md.get("bettingType") != "ODDS" or type(winners) is not int or winners != 1
                or ladder != LADDER_TYPE):
            return ("out_of_population",) + no
        if md.get("status") != "OPEN" or md.get("inPlay") is not False:
            return ("not_open_or_in_play",) + no
        definitions = md.get("runners")
        active = []
        for item in definitions if isinstance(definitions, list) else []:
            if not isinstance(item, dict):
                raise ValueError("runner definitions must be objects")
            selection, handicap = runner_key(item.get("id"), item.get("hc"))
            if handicap == 0 and item.get("status") == "ACTIVE":
                active.append(selection)
        if len(active) < MIN_ACTIVE:
            return ("under_5_active",) + no
        flags = [False, False, False]
        for selection in active:
            for index, value in enumerate(runner_flags(*self.best(market_id, selection))):
                flags[index] = flags[index] or value
        return (USABLE,) + tuple(flags)


class SegmentFold:
    """Folds one segment and captures, for each market and offset X, the state at off + X: the state after every
    message with pt <= off + X (so the batch at off + X is included), taken before the first later message. A
    segment says nothing about a moment after its last line (heartbeats included), so off + X past that line is not
    captured here: the state is never carried into a gap. Each capture carries the pt of the segment's last
    non-heartbeat line at or before it. Also records which markets show a usable qualifying state at some moment in
    the window."""

    def __init__(self, known_offs):
        self.known_offs = known_offs
        self.book = BestOffers()
        self.new_offs = {}
        self.seen = set()
        self.captures = {}
        self.window = set()
        self.revised = set()
        self.pt = self.data_pt = None
        self._batch_data = False
        self._flag_at_last_batch = False
        self._pending = []
        self._pushed = set()

    def off(self, market_id):
        return self.known_offs.get(market_id, self.new_offs.get(market_id))

    def run(self, text):
        for msg in messages(text):
            pt = msg["pt"]
            if self.pt is not None and pt > self.pt:
                self._settle(pt)
                self._batch_data = False
            applied = self.book.apply(msg)
            self.pt = pt
            if msg.get("ct") != "HEARTBEAT":
                self.data_pt = pt
                self._batch_data = True
            for market_id, definition in applied:
                self.seen.add(market_id)
                published = market_time_ms(definition.get("marketTime")) if definition is not None else None
                if self.off(market_id) is None and published is not None:
                    self.new_offs[market_id] = published
                off = self.off(market_id)
                if off is None:
                    continue
                if market_id not in self._pushed:
                    self._pushed.add(market_id)
                    for offset in OFFSETS_S:
                        if off + offset * 1000 >= pt:
                            heapq.heappush(self._pending, (off + offset * 1000, offset, market_id))
                if published is not None and published != off and pt <= off + DECISION_S * 1000:
                    self.revised.add(market_id)
        if self.pt is not None:
            self._settle(None)
        return self

    def _settle(self, next_pt):
        """The state as of self.pt is final: next_pt is the next message's pt (None at the end of the segment, where
        the state holds only at self.pt itself)."""
        flag_changed = self.book.unreliable != self._flag_at_last_batch
        self._flag_at_last_batch = self.book.unreliable
        if (self._batch_data or flag_changed) and self.data_pt is not None and self.pt - self.data_pt <= MAX_AGE_MS:
            for market_id in self.book.markets:
                off = self.off(market_id)
                if off is None or market_id in self.window:
                    continue
                if off + WINDOW_S[0] * 1000 < self.pt <= off + WINDOW_S[1] * 1000:
                    code, primary, _, _ = self.book.evaluate(market_id)
                    if code == USABLE and primary:
                        self.window.add(market_id)
        until = self.pt + 1 if next_pt is None else next_pt
        while self._pending and self._pending[0][0] < until:
            _, offset, market_id = heapq.heappop(self._pending)
            if self.data_pt is not None:
                self.captures[(market_id, offset)] = (self.data_pt, self.book.evaluate(market_id))


# --- sessions --------------------------------------------------------------------------------------------------

def _strict_json(text):
    def refuse(_name):
        raise ValueError("non-finite JSON constant")
    return json.loads(text, parse_constant=refuse)


def _is_regular(path):
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except OSError:
        return False


def _is_directory(path):
    try:
        return stat.S_ISDIR(path.lstat().st_mode)
    except OSError:
        return False


def load_receipt(directory):
    """(receipt, partial, ended, None) or (None, None, ended, skip code), from one read of receipt.json, else
    receipt.partial.json (a session still running, or one that crashed: partial is True). ended is True only for a
    final receipt.json, complete, whose stop_reason says the recording day was over (recording_day.DONE_STOPS); a
    stop for live work or a reconnect restart is not, because the day's recording resumes after it."""
    for name in ("receipt.json", "receipt.partial.json"):
        path = directory / name
        if _is_regular(path):
            try:
                receipt = _strict_json(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, RecursionError):
                return None, None, False, "receipt_unreadable"
            break
    else:
        return None, None, False, "no_receipt"
    ended = (name == "receipt.json" and isinstance(receipt, dict) and receipt.get("complete") is True
             and receipt.get("stop_reason") in DAY_DONE_STOPS)
    if (not isinstance(receipt, dict) or type(receipt.get("schema_version")) is not int
            or receipt["schema_version"] != 1 or not isinstance(receipt.get("segments"), list)):
        return None, None, ended, "receipt_unreadable"
    freshness = receipt.get("data_freshness")
    if (receipt.get("key_type") != "delayed" or not isinstance(freshness, dict)
            or freshness.get("label") != "delayed_conflated"):
        return None, None, ended, "not_delayed_key"
    echoed = freshness.get("conflate_ms_echoed")
    if type(echoed) is not int or echoed != CONFLATE_MS:
        return None, None, ended, "conflation_not_registered"
    subscription = receipt.get("subscription")
    data_filter = subscription.get("market_data_filter") if isinstance(subscription, dict) else None
    fields = data_filter.get("fields") if isinstance(data_filter, dict) else None
    levels = data_filter.get("ladderLevels") if isinstance(data_filter, dict) else None
    if (not isinstance(fields, list) or VIRTUAL_FIELD in fields or any(f not in fields for f in REQUIRED_FIELDS)
            or type(levels) is not int or not 1 <= levels <= MAX_LADDER_LEVELS):
        return None, None, ended, "subscription_not_registered"
    return receipt, name == "receipt.partial.json" or receipt.get("complete") is not True, ended, None


def session_date(name):
    """The UTC date a session started, from its name rec-YYYYMMDDTHHMMSSZ-<hex>."""
    return f"{name[4:8]}-{name[8:10]}-{name[10:12]}"


class Count:
    """Every session under one root that started at or after FIRST_START, read in session-name order (rec-<UTC
    start>, so time order: one recorder runs at a time on a profile) and each session's segments in index order. A
    market's off is the first parseable marketTime met in that order; a later revision moves nothing and is only
    counted."""

    def __init__(self):
        self.offs = {}
        self.seen = set()
        self.captures = {}
        self.window = set()
        self.revised = set()
        self.sessions = {"found": 0, "read": 0, "read_partial": 0, **{code: 0 for code in SESSION_SKIPS}}
        self.starts = []                                   # UTC start date of every session directory, in order
        self.latest_ended = False                          # the latest session directory ended its recording day
        # (both from FIRST_START on: a session that started before it adds no date and closes no day)
        self.segments = {"listed": 0, "read": 0, "unlisted": 0, "listing_failed": 0,
                         **{code: 0 for code in SEGMENT_SKIPS}}

    def read_root(self, root):
        try:
            names = sorted(entry.name for entry in root.iterdir() if SESSION_RE.fullmatch(entry.name))
        except OSError:
            raise Refused("root_unreadable") from None
        for name in names:
            directory = root / name
            self.sessions["found"] += 1
            if name[len("rec-"):len("rec-") + len(FIRST_START)] < FIRST_START:
                self.sessions["recorded_before_registration"] += 1     # not read at all, not even its receipt
                continue
            if not _is_directory(directory):                 # a file, or a symlink (never followed)
                self.sessions["not_a_directory"] += 1
                continue
            receipt, partial, ended, problem = load_receipt(directory)
            self.starts.append(session_date(name))
            self.latest_ended = ended                      # the same read that supplies the session's data
            if problem is not None:
                self.sessions[problem] += 1
                continue
            self.sessions["read"] += 1
            self.sessions["read_partial"] += partial
            self.read_session(directory, receipt)
        return self

    def day_closed(self, day):
        """True when no more data can come for markets with their off on this UTC date: a session started after
        it, or the latest session ended its recording day (one recorder runs at a time on a profile)."""
        return any(start > day for start in self.starts) or self.latest_ended

    def read_session(self, directory, receipt):
        listed = {}
        for entry in receipt["segments"]:
            self.segments["listed"] += 1
            path = entry.get("path") if isinstance(entry, dict) else None
            if not isinstance(path, str) or not SEGMENT_RE.fullmatch(path) or path in listed:
                self.segments["unreadable"] += 1
                continue
            listed[path] = entry
        try:
            on_disk = {p.name for p in directory.iterdir() if SEGMENT_RE.fullmatch(p.name)}
        except OSError:
            self.segments["listing_failed"] += 1           # unlisted segments then go uncounted in this session
            on_disk = set()
        self.segments["unlisted"] += len(on_disk - set(listed))
        for path in sorted(listed, key=lambda name: int(name[len("seg-"):-len(".jsonl")])):
            self.segments[self.read_segment(directory / path, listed[path])] += 1

    def read_segment(self, path, entry):
        if not _is_regular(path):
            return "missing"
        check = entry.get("replay_check")
        if not isinstance(check, dict) or check.get("status") != "passed":
            return "replay_check_not_passed"
        expected = entry.get("sha256")
        try:
            data = path.read_bytes()
        except OSError:
            return "unreadable"
        if not isinstance(expected, str) or not SHA256_RE.fullmatch(expected):
            return "unreadable"
        if hashlib.sha256(data).hexdigest() != expected:
            return "hash_mismatch"
        try:
            fold = SegmentFold(self.offs).run(data.decode("utf-8"))
        except (ValueError, TypeError, RecursionError):
            return "unreadable"
        self.commit(fold)
        return "read"

    def commit(self, fold):
        for market_id, off in fold.new_offs.items():
            self.offs.setdefault(market_id, off)
        self.seen |= fold.seen
        for key, (data_pt, outcome) in fold.captures.items():
            current = self.captures.get(key)
            if current is None or data_pt >= current[0]:
                self.captures[key] = (data_pt, outcome)
        self.window |= fold.window
        self.revised |= fold.revised

    def at(self, market_id, offset):
        """(data pt, outcome) of the last recorded state at or before off + offset, or (None, no_snapshot) when there
        is none or its data is older than 180 s there. The data pt names the delayed batch the state comes from."""
        captured = self.captures.get((market_id, offset))
        cutoff = self.offs[market_id] + offset * 1000
        if captured is None or cutoff - captured[0] > MAX_AGE_MS:
            return None, ("no_snapshot", False, False, False)
        return captured


# --- results ---------------------------------------------------------------------------------------------------

def gate_a(racing_days, primary, usable, recording_open=False):
    """PASS or FAIL only with 10 racing days, 300 usable markets and the recording of the last of those days ended
    (Yu, 2026-11-03)."""
    if racing_days < GATE_MIN_DAYS:
        return "NOT_ENOUGH_DAYS"
    if usable < GATE_MIN_USABLE:
        return "NOT_ENOUGH_MARKETS"
    if recording_open:
        return "RECORDING_OPEN"
    return "PASS" if primary * 1000 >= GATE_PER_MILLE * usable else "FAIL"


def rate_bp(count, out_of):
    """count / out_of in basis points, rounded down (so a FAIL never prints at the threshold); None when out_of is 0."""
    if out_of <= 0:
        return None
    return count * 10000 // out_of


def own_sha256():
    """sha256 of this script's own bytes, so a printed result names the code that produced it."""
    try:
        return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    except (OSError, NameError):
        raise Refused("script_unreadable") from None


def tally(count):
    """Every column per UTC date of the off and pooled, before anything is withheld; the not-usable reasons and
    revised-marketTime counts per date too, so a summary can pool them over the same dates as the columns."""
    days = {}
    not_usable = {}                                    # why markets were not usable at DECISION_S, per date
    revised = {}                                       # usable markets with a revised marketTime, per date
    for market_id in sorted(count.offs):
        day = utc_date(count.offs[market_id])
        row = days.setdefault(day, {column: 0 for column in COLUMNS})
        row["recorded"] += 1
        decision_pt, decision = count.at(market_id, DECISION_S)
        if decision[0] != USABLE:
            reasons = not_usable.setdefault(day, {code: 0 for code in NOT_USABLE})
            reasons[decision[0]] += 1
            continue
        earlier_pt, earlier = count.at(market_id, PERSISTENCE_S)
        _, start = count.at(market_id, WINDOW_S[0])
        flagged = earlier[0] == USABLE and earlier[1]
        row["usable"] += 1
        row["primary"] += decision[1]
        row["flagged"] += flagged
        row["persistence"] += decision[1] and flagged
        row["persistence_two_batches"] += decision[1] and flagged and earlier_pt != decision_pt
        row["window"] += (start[0] == USABLE and start[1]) or market_id in count.window
        row["price_lever"] += decision[2]
        row["width_lever"] += decision[3]
        revised[day] = revised.get(day, 0) + (market_id in count.revised)
    pooled = {column: sum(row[column] for row in days.values()) for column in COLUMNS}
    racing_days = sum(1 for row in days.values() if row["usable"] > 0)
    return {"days": days, "pooled": pooled, "racing_days": racing_days, "not_usable": not_usable,
            "schedule_revised": revised}


def gate_days(days):
    """The UTC date through which Gate A pools: the first date on which the running totals reach GATE_MIN_DAYS
    racing days and GATE_MIN_USABLE usable markets; None while they have not. Later dates never change Gate A."""
    racing_days = usable = 0
    for day in sorted(days):
        racing_days += days[day]["usable"] > 0
        usable += days[day]["usable"]
        if racing_days >= GATE_MIN_DAYS and usable >= GATE_MIN_USABLE:
            return day
    return None


def summarise(count):
    """The printed result: until Gate A can be read, only the recorded and usable counts are shown."""
    counted = tally(count)
    days = counted["days"]
    through = gate_days(days)
    pooled_days = [day for day in days if through is None or day <= through]
    pooled = {column: sum(days[day][column] for day in pooled_days) for column in COLUMNS}
    racing_days = sum(1 for day in pooled_days if days[day]["usable"] > 0)
    recording_open = through is not None and not count.day_closed(through)
    result = gate_a(racing_days, pooled["primary"], pooled["usable"], recording_open)
    withheld = result not in DECIDED

    def shown(row):
        if withheld:
            return {"recorded": row["recorded"], "usable": row["usable"]}
        return {**row, "rates_bp": {c: rate_bp(row[c], row["usable"]) for c in RATE_COLUMNS}}

    gate = {"result": result, "through": through, "racing_days": racing_days, "min_racing_days": GATE_MIN_DAYS,
            "usable": pooled["usable"], "min_usable": GATE_MIN_USABLE,
            "recording_open": recording_open, "threshold_per_mille": GATE_PER_MILLE}
    if not withheld:
        gate["primary"] = pooled["primary"]
    return {
        "schema": SCHEMA, "registration": REGISTRATION, "script_sha256": own_sha256(),
        "inputs": {"sessions": dict(count.sessions), "segments": dict(count.segments),
                   "markets_without_off": len(count.seen - set(count.offs))},
        "counts_withheld": withheld,
        "days": [{"date": day, "racing_day": days[day]["usable"] > 0, "in_gate_a": day in pooled_days,
                  **shown(days[day])} for day in sorted(days)],
        "pooled": {**shown(pooled), "racing_days": racing_days},
        "not_usable": {code: sum(counted["not_usable"].get(day, {}).get(code, 0) for day in pooled_days)
                       for code in NOT_USABLE},
        "schedule_revised": sum(counted["schedule_revised"].get(day, 0) for day in pooled_days),
        "gate_a": gate,
        "limits": LIMITS,
    }


def _percent(bp):
    return "-" if bp is None else f"{bp // 100}.{bp % 100:02d}%"


def render_table(summary):
    withheld = summary["counts_withheld"]
    shown = () if withheld else TABLE_COLUMNS

    def cells(label, row):
        out = [f"{label:<10}", f"{row['recorded']:>9}", f"{row['usable']:>7}"]
        for column, _ in shown:
            out.append(f"{row[column]:>6} {_percent(row['rates_bp'][column]):>8}")
        return "  ".join(out)

    inputs, gate, pooled = summary["inputs"], summary["gate_a"], summary["pooled"]
    sessions, segments = inputs["sessions"], inputs["segments"]
    header = "  ".join([f"{'date':<10}", f"{'recorded':>9}", f"{'usable':>7}"] + [f"{name:>15}" for _, name in shown])
    lines = [f"LP rate count ({SCHEMA}; definitions in {REGISTRATION}). Counts and rates only; "
             f"rates are of markets usable at {DECISION_S} s, rounded down.",
             f"script sha256 {summary['script_sha256']}", header]
    lines += [cells(row["date"], row) for row in summary["days"]]
    lines.append(cells("pooled", pooled))
    lines.append(f"racing days: {pooled['racing_days']} (UTC dates of the off with a market usable at {DECISION_S} s)")
    later = sum(1 for row in summary["days"] if not row["in_gate_a"])
    if later:
        lines.append(f"pooled and Gate A cover the dates through {gate['through']}; the {later} later dates are "
                     "descriptive only")
    if withheld:
        lines.append(f"qualifying counts withheld until Gate A can be read: {gate['min_racing_days']} racing days and "
                     f"{gate['min_usable']} usable markets, and the recording of the last of those dates ended")
    else:
        flagged = pooled["flagged"]
        lines.append(f"persistence in delayed data, not live recall: {pooled['persistence']} of {flagged} usable "
                     f"markets flagged at {PERSISTENCE_S} s ({_percent(rate_bp(pooled['persistence'], flagged))}); "
                     f"from two different data batches {pooled['persistence_two_batches']} "
                     f"({_percent(rate_bp(pooled['persistence_two_batches'], flagged))})")
    lines.append(f"not usable at {DECISION_S} s: " + ", ".join(f"{k} {v}" for k, v in summary["not_usable"].items()))
    lines.append(f"marketTime revised at or before off {DECISION_S} s (counted, not excluded): "
                 f"{summary['schedule_revised']} of {pooled['usable']} usable")
    lines.append(f"sessions: {sessions['read']} read of {sessions['found']}, {sessions['read_partial']} of them from "
                 "a partial receipt (running or crashed) (" + ", ".join(f"{k} {sessions[k]}" for k in SESSION_SKIPS)
                 + ")")
    lines.append(f"segments: {segments['read']} read of {segments['listed']} listed ("
                 + ", ".join(f"{k} {segments[k]}" for k in SEGMENT_SKIPS)
                 + f"); unlisted {segments['unlisted']}; session listings failed {segments['listing_failed']}; "
                 f"markets without an off {inputs['markets_without_off']}")
    state = "-" if gate["through"] is None else "is still open" if gate["recording_open"] else "has ended"
    needs = (f"racing days {gate['racing_days']}, needs {gate['min_racing_days']}; usable {gate['usable']}, "
             f"needs {gate['min_usable']}; pooled through {gate['through'] or '-'}, whose recording {state}")
    if withheld:
        lines.append(f"Gate A: {gate['result']} ({needs})")
    else:
        lines.append(f"Gate A: {gate['result']} ({needs}; "
                     f"primary {gate['primary']} of {gate['usable']} usable at {DECISION_S} s, "
                     f"{_percent(pooled['rates_bp']['primary'])} rounded down; passes when primary x 1000 >= "
                     f"{gate['threshold_per_mille']} x usable)")
    lines.append(f"Limits: {LIMITS}. It decides nothing beyond Gate A.")
    return "\n".join(lines)


# --- command line ----------------------------------------------------------------------------------------------

class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Refused("arguments")


def check_root(text):
    root = Path(text).expanduser()
    if not root.is_absolute():
        root = Path.cwd() / root
    try:
        resolved = root.resolve(strict=True)
    except (OSError, RuntimeError):
        raise Refused("root_missing") from None
    if not resolved.is_dir():
        raise Refused("root_not_a_directory")
    for ancestor in (resolved, *resolved.parents):
        try:
            inside = (ancestor / ".git").exists() or (ancestor / ".git").is_symlink()
        except OSError:
            inside = True
        if inside:
            raise Refused("root_inside_git_worktree")
    return resolved


def main(argv=None):
    parser = _Parser(prog="lp_rate_count.py", description="Stage 0 rate count for Pilot 2 (counts only).")
    parser.add_argument("--root", required=True, help="the recorder's recordings directory on this Mac")
    parser.add_argument("--json", action="store_true", help="print one JSON object instead of the table")
    try:
        args = parser.parse_args(argv)
        summary = summarise(Count().read_root(check_root(args.root)))
        text = json.dumps(summary, indent=1) if args.json else render_table(summary)
    except Refused as refusal:
        print(f"lp_rate_count: refused: {refusal.code}", file=sys.stderr)
        return 2
    except Exception:
        print("lp_rate_count: failed: unexpected_error (no counts printed)", file=sys.stderr)
        return 2
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
