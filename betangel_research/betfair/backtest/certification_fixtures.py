"""Synthetic fixtures for the engine certification (task c2b3b2a): a bytes-only TAR writer, member builders, the
synthetic worlds certify.py runs through the real ingest, index and runner evaluation, and the rules they run under.

Nothing here reads or writes a file, a clock or real data. Every world is a list of TARs, each a list of
(member path, member bytes); certify.py writes them, by exclusive create, into its own work root.

ustar(members): the bytes of a POSIX ustar archive of regular files, built here because the package may not create
archives with tarfile (tests/test_betfair_backtest_archive.py pins tarfile to archive.py, "r|" only; Astra's design
decision 2). One 512-byte header per member: the name (ASCII, 1 to 100 bytes, no prefix field), mode 0644, uid and
gid 0, the size in octal, mtime 0, typeflag "0", magic "ustar\\0" version "00", empty user and group names and the
checksum (the header's bytes summed with the checksum field as spaces); the data padded to 512 bytes; then two zero
blocks. Anything else (a name outside printable ASCII without spaces, longer than 100 bytes, absolute, or with an
empty, "." or ".." segment; a size the field cannot hold; a duplicate) is ValueError.
It is fixture construction only, not a general archive writer: tests cross-read it with tarfile.

Members are bz2 of JSON lines (json.dumps with sorted keys and compact separators, one message per line) or raw byte
lines where a case needs a line the index cannot read. The message builders follow the Historical Data stream shape
the index reads (mcm with pt and mc; marketDefinition, rc ltp, img).

Worlds (each function returns [[(path, bytes), ...] per TAR]):
- scenario_world(form): the prefix-invariance corpus (ported from tests/test_betfair_backtest_cohort.py, with more
  markets): every market's messages up to its decision time (pre), then its future (post) in one of four forms:
  full, cut (nothing after t), deleted (no outcome-bearing message after t in any population market's primary) or
  revised (every decision-time field flipped after t, a removal, a marketTime revision, other winners and BSPs, a
  resettlement). The anchor-by-t population, its decisions and its ledger must be the same in every form. Outside
  those primaries the late-anchor market and the event file's copy of COMPETING keep a full future in the deleted
  and revised forms, and the revised form also gives that copy other pre-t prices (it never decides COMPETING).
- sources_world(change): the named market file against the event file that also holds the market, with a pre-t
  change in either (the event file's never matters; the market file's does).
- membership_world(): members the month gate (a sealed-holdout month) and the content gate (a race whose post-t
  marketTime revision reaches 2026-01) refuse at ingest, beside a kept market.
- duplicate_world(): one market in two archives (the runner's first pass refuses it).
- limit_world(retained, filler_line, batch_at_t): one market whose prefix holds exactly `retained` raw bytes
  (limit_lines: non-mcm filler lines the index skips and, with batch_at_t, an equal-pt batch at t last), so a prefix
  limit of that size is exercised exactly and one byte less is not; kept_within and prefix_sha256 give the bytes,
  lines and digest a capture keeps, from the layout alone.
- abstaining_world(), unexercised_world(): no variant issues anything; every candidate's prefix is unexercised.
- conditions_world(): one market per cohort rule the scenario world does not reach.
- source_rules_worlds(): {name: world} of the primary-source rules the scenario world does not reach;
  event_alone_world(change): the sources world's event file alone.
- witness_outcome(): the resolved market of the settlement witness (one winner at WITNESS_BSP).

The P0 rules (p0_rules) and a second parameter point (second_point_rules: offset -600, stake 250, commission 0.02
per_market_half_up_penny, selection seed 7) are built directly, never from a contract file: the certificate is for
the engine under the structural P0 profile, and each run still validates its own frozen contract (decision 3).
"""

import bz2
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from fractions import Fraction
import hashlib
import json

from .accounting import AccountingRules, MarketOutcome, RunnerOutcome
from .cohort import P0_VARIANTS, CohortRules
from .metrics import BootstrapPlan

OFF = datetime(2015, 6, 1, 14, 0, tzinfo=timezone.utc)
OFFSET_S = -300
T = OFF + timedelta(seconds=OFFSET_S)
EVENT = "27000001"
FORMS = ("full", "cut", "deleted", "revised")
FIXTURE_CONTRACT_TAG = b"betangel-research/backtest/certification/synthetic-contract/v1\x00"
DROP = object()

# --- the TAR writer -----------------------------------------------------------------------------------------------

BLOCK = 512


def _octal(value, width):
    text = f"{value:o}".encode("ascii")
    if len(text) > width - 1:
        raise ValueError("ustar:field_overflow")
    return text.rjust(width - 1, b"0") + b"\x00"


def ustar(members):
    """The bytes of a ustar archive of regular files [(name, data)] (module docstring)."""
    out, names = bytearray(), set()
    for name, data in members:
        if not (isinstance(name, str) and 1 <= len(name) <= 100 and all("\x21" <= c <= "\x7e" for c in name)
                and all(part not in ("", ".", "..") for part in name.split("/"))):
            raise ValueError("ustar:name")
        if name in names:
            raise ValueError("ustar:duplicate_name")
        if not isinstance(data, (bytes, bytearray)):
            raise ValueError("ustar:data")
        names.add(name)
        header = bytearray(BLOCK)
        header[0:100] = name.encode("ascii").ljust(100, b"\x00")
        header[100:108] = _octal(0o644, 8)
        header[108:116] = _octal(0, 8)
        header[116:124] = _octal(0, 8)
        header[124:136] = _octal(len(data), 12)
        header[136:148] = _octal(0, 12)
        header[148:156] = b" " * 8
        header[156:157] = b"0"
        header[257:263] = b"ustar\x00"
        header[263:265] = b"00"
        header[148:156] = f"{sum(header):06o}".encode("ascii") + b"\x00 "
        out += header + bytes(data) + bytes(-len(data) % BLOCK)
    return bytes(out + bytes(2 * BLOCK))


# --- message builders ---------------------------------------------------------------------------------------------

def ms(when):
    return int(when.timestamp() * 1000)


def iso(when):
    return when.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def runner(rid, status="ACTIVE", bsp=None):
    item = {"id": rid, "status": status, "sortPriority": rid, "name": f"Horse {rid}"}
    if bsp is not None:
        item["bsp"] = bsp
    return item


def three():
    return [runner(1), runner(2), runner(3)]


def definition(off, runners, status="OPEN", in_play=False, **extra):
    value = {"bspMarket": True, "turnInPlayEnabled": True, "persistenceEnabled": True, "marketBaseRate": 5.0,
             "eventId": EVENT, "eventTypeId": "7", "numberOfWinners": 1, "bettingType": "ODDS", "marketType": "WIN",
             "marketTime": iso(off), "suspendTime": iso(off), "bspReconciled": False, "complete": True,
             "inPlay": in_play, "crossMatching": True, "runnersVoidable": False, "betDelay": 0, "status": status,
             "numberOfActiveRunners": sum(1 for r in runners if r.get("status") == "ACTIVE"), "runners": runners,
             "regulators": ["MR_INT"], "venue": "Synthetic Park", "countryCode": "GB", "discountAllowed": True,
             "timezone": "Europe/London", "openDate": iso(off), "version": 1, "eventName": "Synthetic race",
             "name": "1m Hcap"}
    value.update(extra)
    return {key: item for key, item in value.items() if item is not DROP}


def mcm(when, changes):
    pt = when if isinstance(when, int) else ms(when)
    return {"op": "mcm", "clk": "1", "pt": pt, "mc": changes}


def md(market_id, when, value, **change):
    return mcm(when, [{"id": market_id, "marketDefinition": value, **change}])


def prices(market_id, when, ltps, **change):
    """An rc change: ltps maps runner id to a price (DROP for an rc entry without ltp)."""
    rc = [{"id": rid} if price is DROP else {"id": rid, "ltp": price} for rid, price in ltps.items()]
    return mcm(when, [{"id": market_id, "rc": rc, **change}])


def settle(market_id, off, statuses, bsps):
    """Post-off messages: BSP reconciled, then CLOSED with the given final statuses."""
    final = [runner(rid, statuses[rid], bsps[rid]) for rid in sorted(statuses)]
    return [md(market_id, off + timedelta(minutes=2), definition(off, final, in_play=True, bspReconciled=True)),
            md(market_id, off + timedelta(minutes=8),
               definition(off, final, status="CLOSED", in_play=True, bspReconciled=True,
                          settledTime=iso(off + timedelta(minutes=8))))]


def lines(messages):
    """The raw member text: one compact JSON line per message (bytes are kept as they are)."""
    return b"".join(m if isinstance(m, bytes) else json.dumps(m, sort_keys=True, separators=(",", ":")).encode()
                    + b"\n" for m in messages)


def member(messages):
    return bz2.compress(lines(messages))


def path(market, when=OFF, plan="BASIC", event=EVENT):
    return f"{plan}/{when.year}/{when.strftime('%b')}/{when.day}/{event}/{market}.bz2"


def by_pt(*streams):
    """Several markets' messages merged into one event-file stream, in pt order (stable within a pt)."""
    return sorted((m for stream in streams for m in stream), key=lambda m: m["pt"])


# --- the scenario world --------------------------------------------------------------------------------------------

def pre_standard(mid, ltps=None, off=OFF, **extra):
    return [md(mid, off - timedelta(hours=2), definition(off, three(), **extra)),
            prices(mid, off - timedelta(minutes=10), ltps or {1: 3.0, 2: 2.5, 3: 6.0})]


def post_full(mid, winner=2, bsps=(3.1, 2.4, 7.0), off=OFF):
    return [prices(mid, off - timedelta(minutes=2), {1: 1.9, 2: 8.0, 3: 1.5}),
            *settle(mid, off, {1: "LOSER", 2: "LOSER", 3: "LOSER", winner: "WINNER"}, dict(zip((1, 2, 3), bsps)))]


def post_deleted(mid, off=OFF):
    """Post-t messages that bear no outcome: a price and a pre-off definition."""
    return [prices(mid, off - timedelta(minutes=2), {1: 1.9, 2: 8.0, 3: 1.5}),
            md(mid, off - timedelta(minutes=1), definition(off, three()))]


def post_revised(mid, off=OFF):
    """Every decision-time field flipped after t, a removal after t, a marketTime revision, other winners and BSPs,
    then a resettlement."""
    revised = off + timedelta(minutes=15)
    return [md(mid, off - timedelta(minutes=4), definition(off, three(), in_play=True, bspMarket=False,
                                                           countryCode="US", eventTypeId="4339", marketType="PLACE",
                                                           status="SUSPENDED")),
            md(mid, off - timedelta(minutes=3), definition(revised, [runner(1), runner(2, "REMOVED"), runner(3)])),
            prices(mid, off - timedelta(minutes=2), {1: 50.0, 3: 1.01}),
            md(mid, off + timedelta(minutes=1), definition(revised, [runner(1), runner(2, "REMOVED"), runner(3)],
                                                           in_play=True, bspReconciled=True)),
            *settle(mid, off, {1: "WINNER", 2: "REMOVED", 3: "LOSER"}, {1: 9.0, 2: None, 3: 1.2}),
            *settle(mid, off + timedelta(minutes=30), {1: "LOSER", 2: "REMOVED", 3: "WINNER"},
                    {1: 9.0, 2: None, 3: 1.3})]


def segmented(mid, when):
    """A message the index cannot read, with a known pt."""
    return {"op": "mcm", "clk": "1", "pt": ms(when), "segmentType": "SEG_START", "mc": [{"id": mid}]}


def post_removed_favourite(mid, form):
    """The favourite (runner 2) removed after t, then the market settled without it."""
    if form == "full":
        return [md(mid, OFF - timedelta(minutes=3), definition(OFF, [runner(1), runner(2, "REMOVED"), runner(3)])),
                *settle(mid, OFF, {1: "WINNER", 2: "REMOVED", 3: "LOSER"}, {1: 3.4, 2: None, 3: 6.5})]
    return {"cut": [], "deleted": post_deleted(mid), "revised": post_revised(mid)}[form]


APRIL = datetime(2015, 4, 30, 14, 0, tzinfo=timezone.utc)
LATE = "1.400000012"             # first published at off-4m: no off-5m decision
COMPETING = "1.400000011"        # its market file decides; the event file holding it too never does
EVENT_ONLY = "1.400000022"       # only the event file mentions it: the event file is its primary
AFTER_OFF = "1.400000021"        # first mentioned after the off: no view

# market id -> (pre messages, post kind, member path); post kinds: standard, corrupt_after_t, removed_favourite, none
SCENARIOS = {
    "1.400000001": lambda: (pre_standard("1.400000001"), "standard", path("1.400000001")),
    # an equal-pt batch at t, the second message changing the favourite
    "1.400000002": lambda: (pre_standard("1.400000002") + [prices("1.400000002", ms(T), {1: 2.0}),
                                                            prices("1.400000002", ms(T), {3: 1.5})],
                            "standard", path("1.400000002")),
    # an image before t that drops runner 3 and resets prices
    "1.400000003": lambda: (pre_standard("1.400000003") + [
        md("1.400000003", OFF - timedelta(minutes=8), definition(OFF, [runner(1), runner(2)]), img=True,
           rc=[{"id": 1, "ltp": 4.0}, {"id": 2, "ltp": 2.0}])], "standard", path("1.400000003")),
    # a removal before t
    "1.400000004": lambda: (pre_standard("1.400000004") + [
        md("1.400000004", OFF - timedelta(minutes=7), definition(OFF, [runner(1), runner(2, "REMOVED"), runner(3)]))],
        "standard", path("1.400000004")),
    # a tie
    "1.400000005": lambda: (pre_standard("1.400000005", {1: 2.5, 2: 2.5, 3: 6.0}), "standard", path("1.400000005")),
    # a missing price
    "1.400000006": lambda: (pre_standard("1.400000006", {1: 2.5, 2: 3.5}), "standard", path("1.400000006")),
    # unreadable inside the prefix: a line whose pt is unknown, after the last pt applied before t
    "1.400000007": lambda: (pre_standard("1.400000007") + [b"{not json\n"], "standard", path("1.400000007")),
    # unreadable only after t: a segmented message (known pt) first after t, or a bad line later
    "1.400000008": lambda: (pre_standard("1.400000008"), "corrupt_after_t", path("1.400000008")),
    # an early close before t (the view stops before t)
    "1.400000009": lambda: ([md("1.400000009", OFF - timedelta(hours=2), definition(OFF, three())),
                             md("1.400000009", OFF - timedelta(minutes=10),
                                definition(OFF, [runner(1, "WINNER", 2.0), runner(2, "LOSER", 3.0),
                                                 runner(3, "LOSER", 4.0)], status="CLOSED"))],
                            "none", path("1.400000009")),
    # suspended at t
    "1.400000010": lambda: (pre_standard("1.400000010") + [
        md("1.400000010", OFF - timedelta(minutes=6), definition(OFF, three(), status="SUSPENDED"))],
        "standard", path("1.400000010")),
    # excluded at t: another country
    "1.400000013": lambda: ([md("1.400000013", OFF - timedelta(hours=2), definition(OFF, three(), countryCode="US")),
                             prices("1.400000013", OFF - timedelta(minutes=10), {1: 3.0})], "standard",
                            path("1.400000013")),
    # unclassifiable at t: no countryCode before t (every later definition carries one)
    "1.400000014": lambda: ([md("1.400000014", OFF - timedelta(hours=2), definition(OFF, three(), countryCode=DROP)),
                             prices("1.400000014", OFF - timedelta(minutes=10), {1: 3.0})], "standard",
                            path("1.400000014")),
    # not a BSP market at t
    "1.400000015": lambda: (pre_standard("1.400000015", bspMarket=False), "standard", path("1.400000015")),
    # a null and a zero price: both missing, never zero
    "1.400000016": lambda: (pre_standard("1.400000016", {1: None, 2: 0, 3: 4.0}), "standard", path("1.400000016")),
    # unreadable at or before t with a known pt (a segmented message at t - 1 minute)
    "1.400000017": lambda: (pre_standard("1.400000017") + [segmented("1.400000017", T - timedelta(minutes=1))],
                            "none", path("1.400000017")),
    # the favourite (runner 2) removed after t
    "1.400000018": lambda: (pre_standard("1.400000018"), "removed_favourite", path("1.400000018")),
    # another tier: an ADVANCED member
    "1.400000019": lambda: (pre_standard("1.400000019"), "standard", path("1.400000019", plan="ADVANCED")),
    # a month outside the evaluated split: a race on 2015-04-30
    "1.400000020": lambda: (pre_standard("1.400000020", off=APRIL), "april", path("1.400000020", when=APRIL)),
}


def _post(mid, kind, form):
    if kind == "none" or form == "cut":
        return []
    if kind == "april":
        return {"full": post_full(mid, off=APRIL), "deleted": post_deleted(mid, off=APRIL),
                "revised": post_revised(mid, off=APRIL)}[form]
    if kind == "removed_favourite":
        return post_removed_favourite(mid, form)
    post = {"full": post_full(mid), "deleted": post_deleted(mid), "revised": post_revised(mid)}[form]
    if kind == "corrupt_after_t":
        bad = segmented(mid, OFF - timedelta(minutes=4))
        post = {"full": [bad] + post, "deleted": post + [b"{not json\n"], "revised": [bad] + post}[form]
    return post


def scenario_world(form):
    """The scenario corpus under one form (module docstring): one TAR."""
    if form not in FORMS:
        raise ValueError("scenario:form")
    members = []
    for mid, make in SCENARIOS.items():
        pre, kind, member_path = make()
        members.append((member_path, member(pre + _post(mid, kind, form))))
    members.append((path(COMPETING), member(pre_standard(COMPETING) + _post(COMPETING, "standard", form))))
    members.append((path(EVENT), member(event_messages(form))))
    late = [] if form == "cut" else [md(LATE, OFF - timedelta(minutes=4), definition(OFF, three()))] + post_full(LATE)
    members.append((path(LATE), member(late)))
    after_off = [] if form == "cut" else [md(AFTER_OFF, OFF + timedelta(minutes=1), definition(OFF, three()))]
    members.append((path(AFTER_OFF), member(after_off)))
    return [members]


def event_messages(form, competing_ltps=None, event_only_ltps=None):
    """The event file: COMPETING (which has its own market file) and EVENT_ONLY (which has none)."""
    competing = pre_standard(COMPETING, competing_ltps or ({1: 1.5, 2: 9.0, 3: 9.0} if form == "revised"
                                                           else {1: 9.0, 2: 9.0, 3: 1.5}))
    only = pre_standard(EVENT_ONLY, event_only_ltps or {1: 4.0, 2: 5.0, 3: 3.5})
    if form != "cut":
        competing += post_full(COMPETING, winner=3)
        only += {"full": post_full(EVENT_ONLY, winner=3), "deleted": post_deleted(EVENT_ONLY),
                 "revised": post_revised(EVENT_ONLY)}[form]
    return by_pt(competing, only)


def sources_world(change):
    """The scenario corpus's competing pair alone, with a pre-t change in the event file or in the market file."""
    if change not in ("none", "event_file", "market_file"):
        raise ValueError("sources:change")
    market_ltps = {1: 2.0, 2: 2.5, 3: 6.0} if change == "market_file" else {1: 3.0, 2: 2.5, 3: 6.0}
    event_ltps = {1: 1.5, 2: 9.0, 3: 9.0} if change == "event_file" else {1: 9.0, 2: 9.0, 3: 1.5}
    return [[(path(COMPETING), member(pre_standard(COMPETING, market_ltps) + post_full(COMPETING))),
             (path(EVENT), member(by_pt(pre_standard(COMPETING, event_ltps) + post_full(COMPETING, winner=3))))]]


SEALED = datetime(2026, 2, 10, 14, 0, tzinfo=timezone.utc)
YEAR_END = datetime(2025, 12, 31, 23, 30, tzinfo=timezone.utc)


def membership_world():
    """A kept market beside a sealed-month member and a content-gated member (refused at ingest)."""
    kept = "1.410000001"
    gated = [md("1.410000003", YEAR_END - timedelta(hours=2), definition(YEAR_END, three())),
             md("1.410000003", YEAR_END - timedelta(minutes=2),
                definition(YEAR_END + timedelta(hours=1), three()))]           # marketTime revised into 2026-01
    return [[(path(kept), member(pre_standard(kept) + post_full(kept))),
             (path("1.410000002", when=SEALED, event="27000002"),
              member(pre_standard("1.410000002", off=SEALED))),
             (path("1.410000003", when=YEAR_END, event="27000003"), member(gated))]]


def duplicate_world():
    """One market in two archives (another event directory in the second)."""
    mid = "1.420000001"
    messages = pre_standard(mid) + post_full(mid)
    return [[(path(mid), member(messages))], [(path(mid, event="27000009"), member(messages))]]


LIMIT_MARKET = "1.430000001"


def _filler_text(index, size):
    """size bytes of deterministic hex (a sha256 chain), which bz2 cannot shrink past the ingest's ratio bound."""
    out, counter = bytearray(), 0
    while len(out) < size:
        out += hashlib.sha256(b"certification-filler/v1:%d:%d" % (index, counter)).hexdigest().encode("ascii")
        counter += 1
    return bytes(out[:size])


def limit_lines(retained, filler_line=1 << 20, batch_at_t=False):
    """The raw prefix lines of the limit market at t, exactly `retained` bytes: its messages up to t, non-mcm filler
    lines (the index skips them; the prefix keeps them) and, with batch_at_t, a closing equal-pt batch at t (two
    price messages at t: runner 1 to 2.0, then runner 3 to 1.5, which becomes the favourite)."""
    head = [lines([message]) for message in pre_standard(LIMIT_MARKET)]
    tail = ([lines([prices(LIMIT_MARKET, ms(T), {1: 2.0})]), lines([prices(LIMIT_MARKET, ms(T), {3: 1.5})])]
            if batch_at_t else [])
    need = retained - sum(map(len, head)) - sum(map(len, tail))
    if need < 0:
        raise ValueError("limit:retained_too_small")
    fillers = []
    while need:                                         # the shortest filler line, {"op":"f","x":""}\n, is 18 bytes
        size = min(need, filler_line)
        if size < 18:
            raise ValueError("limit:remainder_too_small")
        if 0 < need - size < 18:
            size -= 18
        fillers.append(b'{"op":"f","x":"' + _filler_text(len(fillers), size - 18) + b'"}\n')
        need -= size
    out = head + fillers + tail
    if sum(map(len, out)) != retained:
        raise ValueError("limit:arithmetic")
    return out


def limit_world(retained, filler_line=1 << 20, batch_at_t=False):
    """One market whose prefix at t holds exactly `retained` raw bytes (limit_lines), then its first message after t."""
    data = b"".join(limit_lines(retained, filler_line, batch_at_t))
    return [[(path(LIMIT_MARKET), bz2.compress(data + lines(post_full(LIMIT_MARKET))))]]


def kept_within(retained, limit, filler_line=1 << 20, batch_at_t=False):
    """(bytes, lines) a prefix capture keeps under `limit` when the limit is checked before each append: the whole
    lines that fit, stopping at the first that would not."""
    kept = count = 0
    for line in limit_lines(retained, filler_line, batch_at_t):
        if kept + len(line) > limit:
            break
        kept, count = kept + len(line), count + 1
    return kept, count


def prefix_sha256(retained, filler_line=1 << 20, batch_at_t=False):
    """The tagged sha256 of the whole limit prefix (the engineering module's derived-prefix digest, written again)."""
    data = b"".join(limit_lines(retained, filler_line, batch_at_t))
    return hashlib.sha256(b"betangel-research/backtest/primary-prefix/v1\x00" + data).hexdigest()


def abstaining_world():
    """Two markets that are not BSP markets: eligible, decided, nothing issued by either variant."""
    return [[(path(mid), member(pre_standard(mid, bspMarket=False) + post_full(mid)))
             for mid in ("1.440000001", "1.440000002")]]


def unexercised_world():
    """Three markets whose stream holds a line with no trustworthy pt before the boundary: two not JSON, one a price
    change published before the one it follows (a decreasing pt). Every prefix is unexercised."""
    backwards = prices("1.450000003", OFF - timedelta(minutes=20), {1: 4.0})
    return [[(path(mid), member(pre_standard(mid) + [b"{not json\n"] + post_full(mid)))
             for mid in ("1.450000001", "1.450000002")]
            + [(path("1.450000003"), member(pre_standard("1.450000003") + [backwards] + post_full("1.450000003")))]]


CONDITIONS_LATE_SPLIT = datetime(2016, 5, 10, 14, 0, tzinfo=timezone.utc)
MAY_FIRST = datetime(2016, 5, 1, 0, 2, tzinfo=timezone.utc)   # anchor month 2016-05; t (off - 5 min) is in April
APRIL_LAST = datetime(2016, 4, 30, 23, 30, tzinfo=timezone.utc)       # its UTC month is 2016-04, its path day May 1


def _condition(i):
    return f"1.470000{i:03d}"


def conditions_world():
    """One market per cohort rule the scenario world does not reach (cohort docstring: classes, market conditions 2
    to 6 and their order, the price input), each otherwise standard; the index reads them all."""
    c = _condition
    malformed_rc = {"op": "mcm", "clk": "1", "pt": ms(OFF - timedelta(minutes=7)),
                    "mc": [{"id": c(11), "rc": [{"id": "x", "ltp": 1.5}]}]}
    weird = [runner(1), runner(2, "WEIRD"), runner(3)]
    markets = {
        c(1): pre_standard(c(1)) + [md(c(1), OFF - timedelta(minutes=6), definition(OFF, three(), in_play=True))],
        c(2): pre_standard(c(2)) + [md(c(2), OFF - timedelta(minutes=6), definition(OFF, weird))],
        c(3): [md(c(3), OFF - timedelta(hours=2), definition(OFF, [runner(1, "REMOVED"), runner(2, "REMOVED")]))],
        c(4): pre_standard(c(4), eventTypeId="4339"),
        c(5): pre_standard(c(5), marketType="PLACE"),
        c(7): pre_standard(c(7), status=None),
        c(8): pre_standard(c(8), bspMarket=None),
        c(9): pre_standard(c(9), countryCode=DROP, marketType="PLACE"),
        c(10): pre_standard(c(10)) + [md(c(10), OFF - timedelta(minutes=8), definition(OFF, three()), img=True,
                                         rc=[{"id": 1, "ltp": 4.0}, {"id": 2, "ltp": 2.0}])],
        # a malformed price change beside a runner with no price at all
        c(11): pre_standard(c(11), {1: 3.0, 2: 2.5}) + [malformed_rc],
        c(12): pre_standard(c(12), inPlay="yes"),
        c(13): [md(c(13), OFF - timedelta(hours=2), definition(OFF, [runner(0), runner(1), runner(2)])),
                prices(c(13), OFF - timedelta(minutes=10), {0: 2.0, 1: 3.0, 2: 4.0})],
        # two conditions at once: the earlier one gives the reason
        c(14): pre_standard(c(14), status=None, bspMarket=False),
        c(15): pre_standard(c(15), in_play=True, bspMarket=None),
        c(16): [md(c(16), OFF - timedelta(hours=2), definition(OFF, weird, bspMarket=False)),
                prices(c(16), OFF - timedelta(minutes=10), {1: 3.0, 2: 2.5, 3: 6.0})],
        # present, but not text or not a bool
        c(17): pre_standard(c(17), status=5),
        c(18): pre_standard(c(18), bspMarket="yes"),
        # absent class fields
        c(21): pre_standard(c(21), eventTypeId=DROP),
        c(22): pre_standard(c(22), marketType=DROP),
        # two conditions at once, again: 2 before 3; 5 before 6; an unknown runner status before a runner id of 0
        c(24): pre_standard(c(24), status=None, in_play=True),
        c(25): [md(c(25), OFF - timedelta(hours=2),
                   definition(OFF, [runner(1, "REMOVED"), runner(2, "WEIRD"), runner(3, "REMOVED")]))],
        c(26): [md(c(26), OFF - timedelta(hours=2), definition(OFF, [runner(0), runner(1, "WEIRD"), runner(2)])),
                prices(c(26), OFF - timedelta(minutes=10), {0: 2.0, 1: 3.0, 2: 4.0})],
        # a class field and a runner status present but not text
        c(27): pre_standard(c(27), eventTypeId=7),
        c(28): [md(c(28), OFF - timedelta(hours=2), definition(OFF, [runner(1), runner(2, 5), runner(3)])),
                prices(c(28), OFF - timedelta(minutes=10), {1: 3.0, 2: 2.5, 3: 6.0})],
        # a marketTime revised before t, and a price republished unchanged before t
        c(23): [md(c(23), OFF - timedelta(hours=2), definition(OFF, three())),
                prices(c(23), OFF - timedelta(minutes=30), {1: 3.0, 2: 2.5, 3: 6.0}),
                md(c(23), OFF - timedelta(minutes=20), definition(OFF + timedelta(minutes=10), three())),
                prices(c(23), OFF - timedelta(minutes=10), {1: 3.0, 2: 2.5, 3: 6.0})],
    }
    members = [(path(mid), member(messages + post_full(mid))) for mid, messages in markets.items()]
    # the month of the anchor, not of the path: a 2016-05 race, one filed under Apr 30 and one under May 1
    for i, off, filed in ((6, CONDITIONS_LATE_SPLIT, CONDITIONS_LATE_SPLIT), (19, MAY_FIRST, APRIL_LAST),
                          (20, APRIL_LAST, MAY_FIRST)):
        members.append((path(c(i), when=filed), member(pre_standard(c(i), off=off) + post_full(c(i), off=off))))
    return [members]


def _source(i):
    return f"1.460000{i:03d}"


def source_rules_worlds():
    """{name: world} of the primary-source rules (market_index docstring) the scenario world does not reach."""
    s = _source
    heartbeat = {"op": "mcm", "clk": "1", "pt": ms(OFF - timedelta(hours=3)), "mc": []}
    gated = [md(s(6), YEAR_END - timedelta(hours=2), definition(YEAR_END, three())),
             md(s(6), YEAR_END - timedelta(minutes=2), definition(YEAR_END + timedelta(hours=1), three()))]
    return {
        # Astra's R1 pair: the named file holds only a heartbeat; the event file holds the market.
        "r1_pair": [[(path(s(1)), member([heartbeat])), (path(EVENT), member(pre_standard(s(1)) + post_full(s(1))))]],
        # two files named after the market, in two event directories, with other prices; the second by path
        # mentions it first
        "two_named_files": [[(path(s(2), event="27000001"),
                              member(pre_standard(s(2), {1: 2.0, 2: 3.0, 3: 4.0}) + post_full(s(2)))),
                             (path(s(2), event="27000002"),
                              member([md(s(2), OFF - timedelta(hours=3), definition(OFF, three())),
                                      prices(s(2), OFF - timedelta(minutes=10), {1: 4.0, 2: 3.0, 3: 2.0})]
                                     + post_full(s(2))))]],
        # no file of its own: two other markets' files mention it, in two event directories without event files;
        # the second by path mentions it first
        "earliest_mention": [[(path(s(10), event="27000003"), member(by_pt(
            pre_standard(s(10)), [md(s(3), OFF - timedelta(minutes=90), definition(OFF, three())),
                                  prices(s(3), OFF - timedelta(minutes=9), {1: 2.0, 2: 3.0, 3: 4.0})]))),
            (path(s(11), event="27000004"), member(by_pt(
                pre_standard(s(11)), pre_standard(s(3), {1: 4.0, 2: 3.0, 3: 2.0}))))]],
        # no file of its own: another market's file mentions it first; that directory's event file never does
        "event_file_redirect": [[(path(s(12), event="27000005"),
                                  member(by_pt(pre_standard(s(12)), pre_standard(s(4))))),
                                 (path("27000005", event="27000005"), member(pre_standard(s(13))))]],
        # no file of its own: two other markets' files mention it first at the same pt: the first by path decides
        "earliest_mention_tie": [[(path(s(15), event="27000007"), member(by_pt(
            pre_standard(s(15)), pre_standard(s(5), {1: 2.0, 2: 3.0, 3: 4.0})))),
            (path(s(16), event="27000008"), member(by_pt(
                pre_standard(s(16)), pre_standard(s(5), {1: 4.0, 2: 3.0, 3: 2.0}))))]],
        # its named file is refused at ingest by its content (a post-t marketTime revised into 2026-01); the event
        # file in that directory holds it too
        "refused_primary": [[(path(s(6), when=YEAR_END, event="27000006"), member(gated)),
                             (path("27000006", when=YEAR_END, event="27000006"),
                              member(by_pt(pre_standard(s(14), off=YEAR_END), gated[:1])))]],
    }


def event_alone_world(change):
    """The event file of sources_world alone (no market file): its pre-t change, unchanged or not, decides."""
    event_ltps = {1: 1.5, 2: 9.0, 3: 9.0} if change == "event_file" else {1: 9.0, 2: 9.0, 3: 1.5}
    return [[(path(EVENT), member(by_pt(pre_standard(COMPETING, event_ltps) + post_full(COMPETING, winner=3))))]]


def expected_decision_id(rules, market_id, selection_id):
    """A row's decision id (cohort docstring, plan D9), written again from its description: the sha256 of the
    compact JSON of [domain, contract sha256, market id, selection id or null, decision axis]."""
    text = json.dumps(["betangel-research/backtest/decision-id/v1", rules.contract_sha256, market_id, selection_id,
                       rules.axis], separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


WITNESS_MARKET, WITNESS_BSP = "1.480000001", "2.0675"


def witness_outcome():
    """The settlement witness's market (engineering KAT style, every indicator resolved): runner 1 wins at
    WITNESS_BSP, runner 2 loses."""
    return MarketOutcome(market_id=WITNESS_MARKET, first_published_off=OFF, read_complete=True, closed=True,
                         market_void=False, result_revised_after_close=False, bsp_reconciled=True, dead_heat=False,
                         declared_winners=1, removed_after_decision=(),
                         runners={1: RunnerOutcome("WINNER", Decimal(WITNESS_BSP)),
                                  2: RunnerOutcome("LOSER", Decimal("3.0"))})


# --- rules ----------------------------------------------------------------------------------------------------------

VARIANTS = tuple(sorted(P0_VARIANTS))


def _cohort_rules(offset_s, stake_pence, selection_seed, label):
    return CohortRules(contract_sha256=hashlib.sha256(FIXTURE_CONTRACT_TAG + label.encode()).hexdigest(),
                       axis=f"first_published_scheduled_off{offset_s:+d}s", offset_s=offset_s, delay_s=0,
                       selection_seed=selection_seed, stake_pence=stake_pence, tier="BASIC", countries=("GB", "IE"),
                       market_types=("WIN",), start_month="2015-05", end_month="2016-04", variants=VARIANTS)


def p0_rules():
    """(CohortRules, AccountingRules, BootstrapPlan) at P0's operational values."""
    return (_cohort_rules(-300, 100, 1, "p0"),
            AccountingRules(commission_rate=Decimal("0.05"), rounding="per_market_ceil_penny", stake_pence=100,
                            variants=VARIANTS),
            BootstrapPlan(resamples=200, confidence_level=Fraction(19, 20), seed=2))


def second_point_rules():
    """The second parameter point: the parameters must flow through, not be constants of the engine."""
    return (_cohort_rules(-600, 250, 7, "second_point"),
            AccountingRules(commission_rate=Decimal("0.02"), rounding="per_market_half_up_penny", stake_pence=250,
                            variants=VARIANTS),
            BootstrapPlan(resamples=200, confidence_level=Fraction(19, 20), seed=3))


# --- expected answers -----------------------------------------------------------------------------------------------
# Hand-labelled per case (certify.CASES), never generated from the engine. Each comment says where the answer comes
# from: the plan, the module docstrings that specify the engine, Astra's design decisions, or the fixture itself.
# Every answer that is not fixture arithmetic was derived twice, by two derivations blind to the engine and to each
# other, from the specification (the cohort, market_index, replay, outcome, accounting, archive, engineering and
# metrics docstrings and docs/backtest-plan.md) and the fixtures, then adjudicated against the engine: the scenario
# rows first (c2b3b2a-derivation.json), then the timing, inputs, forms, limits and controls, the conditions and
# sources worlds and the read limits (c2b3b2a-derivation-r2, -r3 and -r4.json, 2026-10-15). In every round both
# derivations agreed with each other and with the engine on every value.

# The limit market: pre_standard prices {1: 3.0, 2: 2.5, 3: 6.0} at off-10m; nothing else before t. Eligible (BASIC,
# 2015-06, GB, WIN, horse racing, OPEN, not in play, a BSP market, three ACTIVE listed runners): ltp_favourite issues
# the unique lowest, runner 2; the seeded draw over runners 1-3 (seed 1, label "selection/v1/1.430000001") is
# SEEDED_DRAWS[LIMIT_MARKET]; every other row is not_selected.
# The scenario cases' answers. The seeded draws were computed from the hash-stream description with hashlib alone,
# as the tests compute them again. Per market the rules
# applied are: the population classes in the cohort order (tier, month, event type, country, market type; a field
# absent at t is unclassifiable); market conditions 1 to 6 in order; then the price input (null, 0 or absent is
# missing); ltp_favourite issues the unique lowest usable price, abstains ltp_tie on a shared lowest and
# ltp_missing_in_market when any row is unsupported; the seeded variant issues its draw unless that row is
# unsupported. Every P0 bet is unresolved (void_unknown at least: outcome never infers market_void), with the further
# reasons of the accounting precedence table.
AFTER_T, END_OF_STREAM = ("passed", None, "first_message_after_t"), ("passed", None, "end_of_stream")
KNOWN_FAILURE = ("passed", None, "known_failure_at_or_before_t")
UNKNOWN_PT = ("unexercised", "unknown_pt_before_boundary:not_json", None)
LATE_ANCHOR, VIEWLESS = ("not_candidate", "anchor_not_known_by_decision", None), ("not_candidate", "no_view", None)
ISS, NOT = ("issued", None, None), ("not_selected", None, None)
CLOSED_BY_T = ("abstained", "market_not_open_pre_off", "first_outcome_bearing_definition")
NOT_OPEN, NOT_BSP = ("abstained", "market_not_open_pre_off", None), ("abstained", "bsp_market_false", None)
TIE, PRICE_MISSING_IN_MARKET = ("abstained", "ltp_tie", None), ("abstained", "ltp_missing_in_market", None)
NO_PRICE, UNREADABLE = ("unsupported", "ltp", "missing"), ("unsupported", "decision_view_unreadable", None)

# market id -> (class, reason, prefix [status, reason, boundary], rows [(selection, ltp_favourite, seeded, drawn)])
SCENARIO_FULL_MARKETS = {
    '1.400000001': ('eligible', None, AFTER_T, [(1, NOT, ISS, True), (2, ISS, NOT, False), (3, NOT, NOT, False)]),
    '1.400000002': ('eligible', None, AFTER_T, [(1, NOT, NOT, False), (2, NOT, ISS, True), (3, ISS, NOT, False)]),
    '1.400000003': ('eligible', None, AFTER_T, [(1, NOT, ISS, True), (2, ISS, NOT, False)]),
    '1.400000004': ('eligible', None, AFTER_T, [(1, ISS, NOT, False), (3, NOT, ISS, True)]),
    '1.400000005': ('eligible', None, AFTER_T, [(1, TIE, NOT, False), (2, TIE, NOT, False), (3, TIE, ISS, True)]),
    '1.400000006': ('eligible', None, AFTER_T, [
        (1, PRICE_MISSING_IN_MARKET, NOT, False), (2, PRICE_MISSING_IN_MARKET, ISS, True),
        (3, NO_PRICE, NO_PRICE, False)]),
    '1.400000007': ('eligible', None, UNKNOWN_PT, [(None, UNREADABLE, UNREADABLE, None)]),
    '1.400000008': ('eligible', None, AFTER_T, [(1, NOT, NOT, False), (2, ISS, NOT, False), (3, NOT, ISS, True)]),
    '1.400000009': ('eligible', None, END_OF_STREAM, [(None, CLOSED_BY_T, CLOSED_BY_T, None)]),
    '1.400000010': ('eligible', None, AFTER_T, [
        (1, NOT_OPEN, NOT_OPEN, None), (2, NOT_OPEN, NOT_OPEN, None),
        (3, NOT_OPEN, NOT_OPEN, None)]),
    '1.400000011': ('eligible', None, AFTER_T, [(1, NOT, ISS, True), (2, ISS, NOT, False), (3, NOT, NOT, False)]),
    '1.400000012': ('anchor_not_known_by_decision', None, LATE_ANCHOR, []),
    '1.400000013': ('excluded', 'country_not_in_population', AFTER_T, []),
    '1.400000014': ('unclassifiable', 'countryCode', AFTER_T, []),
    '1.400000015': ('eligible', None, AFTER_T, [
        (1, NOT_BSP, NOT_BSP, None), (2, NOT_BSP, NOT_BSP, None),
        (3, NOT_BSP, NOT_BSP, None)]),
    '1.400000016': ('eligible', None, AFTER_T, [
        (1, NO_PRICE, NO_PRICE, False), (2, NO_PRICE, NO_PRICE, True),
        (3, PRICE_MISSING_IN_MARKET, NOT, False)]),
    '1.400000017': ('eligible', None, KNOWN_FAILURE, [(None, UNREADABLE, UNREADABLE, None)]),
    '1.400000018': ('eligible', None, AFTER_T, [(1, NOT, NOT, False), (2, ISS, NOT, False), (3, NOT, ISS, True)]),
    '1.400000019': ('excluded', 'plan_not_contract_tier', AFTER_T, []),
    '1.400000020': ('excluded', 'month_not_evaluated', AFTER_T, []),
    '1.400000021': ('no_view', None, VIEWLESS, []),
    '1.400000022': ('eligible', None, AFTER_T, [(1, NOT, ISS, True), (2, NOT, NOT, False), (3, ISS, NOT, False)]),
}

# market id -> (class, reason, rows), under the second parameter point (t = off - 10 minutes)
SECOND_POINT_MARKETS = {
    '1.400000001': ('eligible', None, [(1, NOT, NOT, False), (2, ISS, ISS, True), (3, NOT, NOT, False)]),
    '1.400000002': ('eligible', None, [(1, NOT, NOT, False), (2, ISS, ISS, True), (3, NOT, NOT, False)]),
    '1.400000003': ('eligible', None, [(1, NOT, ISS, True), (2, ISS, NOT, False), (3, NOT, NOT, False)]),
    '1.400000004': ('eligible', None, [(1, NOT, NOT, False), (2, ISS, ISS, True), (3, NOT, NOT, False)]),
    '1.400000005': ('eligible', None, [(1, TIE, ISS, True), (2, TIE, NOT, False), (3, TIE, NOT, False)]),
    '1.400000006': ('eligible', None, [
        (1, PRICE_MISSING_IN_MARKET, ISS, True), (2, PRICE_MISSING_IN_MARKET, NOT, False),
        (3, NO_PRICE, NO_PRICE, False)]),
    '1.400000007': ('eligible', None, [(None, UNREADABLE, UNREADABLE, None)]),
    '1.400000008': ('eligible', None, [(1, NOT, ISS, True), (2, ISS, NOT, False), (3, NOT, NOT, False)]),
    '1.400000009': ('eligible', None, [(None, CLOSED_BY_T, CLOSED_BY_T, None)]),
    '1.400000010': ('eligible', None, [(1, NOT, NOT, False), (2, ISS, ISS, True), (3, NOT, NOT, False)]),
    '1.400000011': ('eligible', None, [(1, NOT, NOT, False), (2, ISS, ISS, True), (3, NOT, NOT, False)]),
    '1.400000012': ('anchor_not_known_by_decision', None, []),
    '1.400000013': ('excluded', 'country_not_in_population', []),
    '1.400000014': ('unclassifiable', 'countryCode', []),
    '1.400000015': ('eligible', None, [
        (1, NOT_BSP, NOT_BSP, None), (2, NOT_BSP, NOT_BSP, None),
        (3, NOT_BSP, NOT_BSP, None)]),
    '1.400000016': ('eligible', None, [
        (1, NO_PRICE, NO_PRICE, False), (2, NO_PRICE, NO_PRICE, True),
        (3, PRICE_MISSING_IN_MARKET, NOT, False)]),
    '1.400000017': ('eligible', None, [(1, NOT, NOT, False), (2, ISS, ISS, True), (3, NOT, NOT, False)]),
    '1.400000018': ('eligible', None, [(1, NOT, NOT, False), (2, ISS, NOT, False), (3, NOT, ISS, True)]),
    '1.400000019': ('excluded', 'plan_not_contract_tier', []),
    '1.400000020': ('excluded', 'month_not_evaluated', []),
    '1.400000021': ('no_view', None, []),
    '1.400000022': ('eligible', None, [(1, NOT, NOT, False), (2, NOT, ISS, True), (3, ISS, NOT, False)]),
}

# A bet's reasons beyond void_unknown: 1.400000008's member is unreadable after t, so its outcome is incomplete; on
# 1.400000018 the favourite (ltp_favourite's runner 2) is removed after t, which is the seeded bet's other runner.
TRUNCATED = ('outcome_incomplete',
             'market_not_closed', 'void_unknown', 'revision_unknown', 'bsp_not_reconciled', 'dead_heat_unknown',
             'removals_unknown', 'own_status_unexpected', 'bsp_missing')
SETTLEMENT_REASONS = {("1.400000008", "ltp_favourite"): TRUNCATED, ("1.400000008", "seeded_random_runner"): TRUNCATED,
                      ("1.400000018", "ltp_favourite"): ("void_unknown", "own_runner_removed_after_decision"),
                      ("1.400000018", "seeded_random_runner"): ("void_unknown", "other_runner_removed_after_decision")}


def _input(inputs, market_id, selection):
    if selection is None:
        return None
    ltp, ltp_pt = inputs[market_id][selection]
    return {"ltp": ltp, "ltp_pt": ltp_pt}


def _row_intent(rules, decision_ms, outcome):
    """A variant's intent on its row (cohort docstring: an issued outcome carries the contract's intent, BACK at BSP
    for the fixed stake, effective at t + delay_s; every other outcome has none)."""
    if outcome[0] != "issued":
        return None
    return {"effective_ms": decision_ms + rules.delay_s * 1000, "price": "BSP", "side": "BACK",
            "stake_pence": rules.stake_pence}


def _expand(table, rules, timing, inputs, with_prefix):
    """A market table in the projection's shape (certify._market_in_full): each row's decision id by
    expected_decision_id, its decision block from the market's timing and the rules' axis, and each variant's
    intent on that row from the rules and the market's own decision time."""
    markets = {}
    for market_id, spec in table.items():
        population, reason, rows = spec[0], spec[1], spec[-1]
        anchor_pt, anchor_ms, decision_ms = timing[market_id]
        market = {"population": population, "reason": reason, "timing": [anchor_pt, anchor_ms, decision_ms],
                  "rows": [[selection, {"ltp_favourite": [*favourite, None], "seeded_random_runner": [*seeded, drawn]},
                            _input(inputs, market_id, selection), expected_decision_id(rules, market_id, selection),
                            [rules.axis, decision_ms, anchor_pt, anchor_ms],
                            {"ltp_favourite": _row_intent(rules, decision_ms, favourite),
                             "seeded_random_runner": _row_intent(rules, decision_ms, seeded)}]
                           for selection, favourite, seeded, drawn in rows]}
        if with_prefix:
            market["prefix"] = list(spec[2])
        markets[market_id] = market
    return markets


def _settlement(markets):
    bets = [[variant, market_id, row[0], "unresolved",
             list(SETTLEMENT_REASONS.get((market_id, variant), ("void_unknown",))), "missing_bsp_or_result"]
            for market_id, market in markets.items() for row in market["rows"]
            for variant, outcome in row[1].items() if outcome[0] == "issued"]
    return sorted(bets)


def _ledger(table):
    """The ledger's row order (cohort ledger_rows: anchor_ms, then market id, then the selection-null row first,
    then selection id). Every market with rows has the anchor OFF (the April market is excluded and has none), so
    this is market id order, then each market's rows as the table lists them (selection order)."""
    return [[market_id, row[0]] for market_id in sorted(table) for row in table[market_id][-1]]


def _draws(table):
    """market id -> (the ACTIVE listed runners at t, the drawn runner), read off a market table."""
    return {market_id: (tuple(row[0] for row in spec[-1]), next(row[0] for row in spec[-1] if row[3] is True))
            for market_id, spec in table.items() if any(row[3] is True for row in spec[-1])}


def _intent(stake_pence, offset_s):
    return [{"effective_ms": ms(OFF + timedelta(seconds=offset_s)), "price": "BSP", "side": "BACK",
             "stake_pence": stake_pence}]


def _timing(offset_s):
    """market id -> (anchor_pt, anchor_ms, decision_ms): the anchor is the pt and value of the first-published
    marketTime (each market's first definition: off-2h; LATE's at off-4m; the April race's at its own off-2h), t the
    anchor plus the offset (cohort docstring, decision time); the no-view record has none."""
    timing = {market_id: (ms(OFF - timedelta(hours=2)), ms(OFF), ms(OFF + timedelta(seconds=offset_s)))
              for market_id in SCENARIO_FULL_MARKETS}
    timing[LATE] = (ms(OFF - timedelta(minutes=4)), ms(OFF), ms(OFF + timedelta(seconds=offset_s)))
    timing["1.400000020"] = (ms(APRIL - timedelta(hours=2)), ms(APRIL), ms(APRIL + timedelta(seconds=offset_s)))
    timing[AFTER_OFF] = (None, None, None)
    return timing


_TEN = ms(OFF - timedelta(minutes=10))


def _at(ltps, pt=_TEN):
    """{selection: (ltp, ltp_pt)}: each runner's last price change at or before t and its pt (replay: a timeline of
    changes; an absent or first-null price has none, so (None, None); a raw 0 is kept as 0 and is missing; a price
    an image resets is (None, the image's pt), written out where it occurs)."""
    return {selection: (ltp, None if ltp is None else pt) for selection, ltp in ltps.items()}


_STANDARD = _at({1: 3.0, 2: 2.5, 3: 6.0})
# market id -> {selection: (ltp, ltp_pt)} for every row with a selection (rows: SCENARIO_FULL_MARKETS)
SCENARIO_INPUTS = {
    "1.400000001": _STANDARD,
    "1.400000002": {1: (2.0, ms(T)), 2: (2.5, _TEN), 3: (1.5, ms(T))},          # the batch at t is in the view
    "1.400000003": _at({1: 4.0, 2: 2.0}, ms(OFF - timedelta(minutes=8))),      # the image's prices
    "1.400000004": {1: (3.0, _TEN), 3: (6.0, _TEN)},                           # runner 2 removed before t: no row
    "1.400000005": _at({1: 2.5, 2: 2.5, 3: 6.0}), "1.400000006": _at({1: 2.5, 2: 3.5, 3: None}),
    "1.400000008": _STANDARD, "1.400000010": _STANDARD, "1.400000011": _STANDARD, "1.400000015": _STANDARD,
    "1.400000016": _at({1: None, 2: 0, 3: 4.0}), "1.400000018": _STANDARD,
    "1.400000022": _at({1: 4.0, 2: 5.0, 3: 3.5}),                              # the event file's prices
}
# At off-10m the view holds only the off-2h definition and the off-10m prices (the later changes come after t).
SECOND_POINT_INPUTS = dict(SCENARIO_INPUTS, **{market_id: _STANDARD for market_id in (
    "1.400000002", "1.400000003", "1.400000004", "1.400000017")})
SCENARIO_TIMING, SECOND_POINT_TIMING = _timing(-300), _timing(-600)

SEEDED_DRAWS = dict(_draws(SCENARIO_FULL_MARKETS), **{LIMIT_MARKET: ((1, 2, 3), 1)})
SEEDED_DRAWS_SECOND_POINT = _draws(SECOND_POINT_MARKETS)
OUTSIDE_THE_POPULATION = {"1.400000012": "anchor_not_known_by_decision", "1.400000021": "no_view"}
# The population markets whose primary holds a future after t in the full form: not 1.400000007 (a line without a pt
# ends its read at off-10m) and not 1.400000009 and 1.400000017 (no post messages). Revised: not 1.400000008 either.
WITH_FUTURE = ['1.400000001', '1.400000002', '1.400000003', '1.400000004', '1.400000005', '1.400000006',
               '1.400000008', '1.400000010', '1.400000011', '1.400000013', '1.400000014', '1.400000015',
               '1.400000016', '1.400000018', '1.400000019', '1.400000020', '1.400000022']
REVISED_FUTURE = [market_id for market_id in WITH_FUTURE if market_id != "1.400000008"]
# 1.400000009 closed before t (a CLOSED definition with results and BSPs at off-10m): its record bears an outcome in
# every form, from before t. No other population primary bears one in the cut and deleted forms.
EARLY_CLOSE = ["1.400000009"]
REVISED_FLIPS = ["bspMarket", "countryCode", "eventTypeId", "inPlay", "marketTime", "marketType",
                 "numberOfActiveRunners", "openDate", "status", "suspendTime", "removals"]
P0_SETTLEMENT_RULES = ["0.05", "per_market_ceil_penny", 100, list(VARIANTS)]
SECOND_POINT_SETTLEMENT_RULES = ["0.02", "per_market_half_up_penny", 250, list(VARIANTS)]
_P0_FULL = _expand(SCENARIO_FULL_MARKETS, p0_rules()[0], SCENARIO_TIMING, SCENARIO_INPUTS, True)
SCENARIO_ANSWERS = {
    "scenario_full": {
        "markets": _P0_FULL, "intents": _intent(100, -300), "ledger": _ledger(SCENARIO_FULL_MARKETS),
        "settlement": _settlement(_P0_FULL), "settlement_rules": P0_SETTLEMENT_RULES,
        # 22 records: 2 outside the population (late anchor, no view), 20 candidates; 1.400000007's prefix is
        # unexercised (a line without a pt before its boundary), so 19 are compared and pass; incomplete because of it.
        "tally": {"records": 22, "not_candidate": 2, "candidates": 20, "unexercised": 1, "compared": 19,
                  "passed": 19, "failed": 0, "missing": 0},
        "tally_status": "incomplete", "reconciliation_status": "passed"},
    # The cut form holds no message after t: the late-anchor and after-the-off markets have no record at all, so
    # their outcomes change too; no primary is read past t; nothing after t reaches decision_time.
    "scenario_cut": {"same_population": True, "same_decisions": True, "same_ledger": True, "diagnostics": {},
                     "compared_markets": 20,
                     "distinct": {"markets_files_differ": True,
                                  "changed_outcomes": sorted(WITH_FUTURE + [LATE, AFTER_OFF]),
                                  "after_t": [], "outcome_bearing": EARLY_CLOSE, "published_after_t": {}}},
    # The deleted form's off-1m definition restores the standard one: decision_time gains an entry after t only
    # where the state at t differed from it (an image or removal left two runners active; suspended; another
    # country, or none; not a BSP market). 1.400000008 is read to its bad line at the end.
    "scenario_deleted": {"same_population": True, "same_decisions": True, "same_ledger": True,
                         "diagnostics": OUTSIDE_THE_POPULATION, "compared_markets": 20,
                         "distinct": {"markets_files_differ": True, "changed_outcomes": WITH_FUTURE,
                                      "after_t": WITH_FUTURE, "outcome_bearing": EARLY_CLOSE,
                                      "published_after_t": {"1.400000003": ["numberOfActiveRunners"],
                                                            "1.400000004": ["numberOfActiveRunners"],
                                                            "1.400000010": ["status"],
                                                            "1.400000013": ["countryCode"],
                                                            "1.400000014": ["countryCode"],
                                                            "1.400000015": ["bspMarket"]}}},
    # The revised form flips every decision-time field after t (off-4m: in play, not BSP, US, another event type,
    # PLACE, suspended; off-3m: marketTime, suspendTime and openDate revised, runner 2 removed, so two active).
    # 1.400000008 stops at its segment at off-4m in both forms, so its outcome is unchanged.
    "scenario_revised": {"same_population": True, "same_decisions": True, "same_ledger": True,
                         "diagnostics": OUTSIDE_THE_POPULATION, "compared_markets": 20,
                         "distinct": {"markets_files_differ": True, "changed_outcomes": REVISED_FUTURE,
                                      "after_t": REVISED_FUTURE,
                                      "outcome_bearing": sorted(REVISED_FUTURE + EARLY_CLOSE),
                                      "published_after_t": {market_id: REVISED_FLIPS
                                                            for market_id in REVISED_FUTURE}}},
    "second_point": {"markets": _expand(SECOND_POINT_MARKETS, second_point_rules()[0], SECOND_POINT_TIMING,
                                        SECOND_POINT_INPUTS, False),
                     "intents": _intent(250, -600), "ledger": _ledger(SECOND_POINT_MARKETS),
                     "settlement_rules": SECOND_POINT_SETTLEMENT_RULES,
                     # every input record has its decision, the ledger and the bets are the issued rows and both
                     # variants issue: passed (the tally may still be incomplete; it is not part of this case)
                     "reconciliation_status": "passed"},
}

def _three_runner_rows(issued_favourite, drawn):
    return [[rid, {"ltp_favourite": ["issued" if rid == issued_favourite else "not_selected", None, None, None],
                   "seeded_random_runner": ["issued" if rid == drawn else "not_selected", None, None, rid == drawn]}]
            for rid in (1, 2, 3)]


# (bytes, lines, tagged sha256) a capture keeps in each limit case, from the fixture's layout alone (limit_lines,
# kept_within, prefix_sha256; the tests compute them again): a passed prefix keeps every line before the first
# message after t; an unexercised one keeps the whole lines before the first that would pass the prefix limit or
# the read limit (kept_within knows only the prefix limit), with no digest.
LIMIT_KEPT = {
    "limit_small_exact": [4096, 3, "daa389c74ec3a93eb8dd19c8ab5fa26f0279b1bd3ed6edac0d0f63f625d4edd6"],
    "limit_small_over": [1056, 2, None],
    "limit_batch_exact": [4096, 5, "addec52f6ceb56948f22d2a2b4e9419590a7a170b4fb188fd4e0ccb51217f34f"],
    "limit_batch_inside": [4000, 4, None],
    "limit_64mib_exact": [67108864, 68, "70d3b20559eaa364e49fd32bd4fdade8588b7b286c913ab3ddb75f31b0cb0636"],
    "limit_64mib_over": [67108769, 67, None],
    "read_limits_exact": [4096, 5, "eaf59cb6289a39785009655b4dc71118a48c935a82b26d78ef3e8d91af75e401"],
    "read_limits_unexercised": [1056, 2, None],
}
RUN_LIMIT, RUN_LINE = 64 * 1024 * 1024, 16 * 1024 * 1024


def _run_tally(passed, max_line_bytes=RUN_LINE, unexercised=None):
    """The run's own tally over the one limit market (prefix limit 64 MiB): compared and passed (both variants issue),
    or unexercised for the reason given, so incomplete."""
    if passed:
        return {"status": "passed", "prefix_limit_bytes": RUN_LIMIT, "max_line_bytes": max_line_bytes, "compared": 1,
                "unexercised_by_reason": {}}
    return {"status": "incomplete", "prefix_limit_bytes": RUN_LIMIT, "max_line_bytes": max_line_bytes,
            "compared": 0, "unexercised_by_reason": {unexercised: 1}}


def _limit_expected(case_id, passed, prefix_limit, favourite, run_passed, max_line_bytes=RUN_LINE):
    """A limit case whose lines the index reads whole: its prefix result, the bytes, lines and digest kept, the
    decision (a prefix limit never changes it) and the run's own tally."""
    prefix = ["passed", None, "first_message_after_t"] if passed else ["unexercised", "prefix_buffer_limit", None]
    return {"prefix": prefix, "prefix_limit_bytes": prefix_limit, "kept": LIMIT_KEPT[case_id],
            "decision": {"population": "eligible", "reason": None,
                         "rows": _three_runner_rows(favourite, SEEDED_DRAWS[LIMIT_MARKET][1])},
            "run_tally": (_run_tally(True, max_line_bytes) if run_passed
                          else _run_tally(False, max_line_bytes, "prefix_buffer_limit"))}


STATUS_UNKNOWN = ("unsupported", "market_definition", "status_unknown")
IN_PLAY_UNKNOWN = ("unsupported", "market_definition", "in_play_unknown")
BSP_UNKNOWN = ("unsupported", "market_definition", "bsp_market_unknown")
RUNNER_STATUS_UNKNOWN = ("unsupported", "market_definition", "runner_status_unknown")
RUNNER_ID_NOT_POSITIVE = ("unsupported", "market_definition", "runner_id_not_positive")
MALFORMED_PRICE, NO_ACTIVE = ("unsupported", "ltp", "malformed_price_change"), ("abstained", "no_active_runner", None)


def _all(outcome, selections=(1, 2, 3)):
    """Rows where a market condition gives both variants one outcome and no draw is made."""
    return [(selection, outcome, outcome, None) for selection in selections]


# The conditions world under P0: market id -> (class, reason, rows). Each comment names the rule that decides.
CONDITIONS_MARKETS = {
    '1.470000001': ('eligible', None, _all(NOT_OPEN)),                    # 3: in play at t
    '1.470000002': ('eligible', None, _all(RUNNER_STATUS_UNKNOWN, (None,))),  # 5: a listed runner WEIRD
    '1.470000003': ('eligible', None, _all(NO_ACTIVE, (None,))),          # 6: every runner removed
    '1.470000004': ('excluded', 'event_type_not_horse_racing', []),
    '1.470000005': ('excluded', 'market_type_not_in_population', []),
    '1.470000006': ('excluded', 'month_not_evaluated', []),               # a 2016-05 anchor
    '1.470000007': ('eligible', None, _all(STATUS_UNKNOWN)),              # 2: status null (absent)
    '1.470000008': ('eligible', None, _all(BSP_UNKNOWN)),                 # 4: bspMarket null (absent)
    '1.470000009': ('excluded', 'market_type_not_in_population', []),     # a known mismatch beats no country
    '1.470000010': ('eligible', None, [                                   # the image resets runner 3's price
        (1, PRICE_MISSING_IN_MARKET, NOT, False), (2, PRICE_MISSING_IN_MARKET, NOT, False),
        (3, NO_PRICE, NO_PRICE, True)]),
    '1.470000011': ('eligible', None, [                                   # malformed beats runner 3's missing price
        (1, MALFORMED_PRICE, MALFORMED_PRICE, True), (2, MALFORMED_PRICE, MALFORMED_PRICE, False),
        (3, MALFORMED_PRICE, MALFORMED_PRICE, False)]),
    '1.470000012': ('eligible', None, _all(IN_PLAY_UNKNOWN)),             # 3: inPlay "yes"
    '1.470000013': ('eligible', None, _all(RUNNER_ID_NOT_POSITIVE, (0, 1, 2))),  # 5: runner 0; rows apart
    '1.470000014': ('eligible', None, _all(STATUS_UNKNOWN)),              # 2 before 4 (not a BSP market)
    '1.470000015': ('eligible', None, _all(NOT_OPEN)),                    # 3 before 4 (bspMarket null)
    '1.470000016': ('eligible', None, _all(NOT_BSP, (None,))),            # 4 before 5; a WEIRD runner: one row
    '1.470000017': ('eligible', None, _all(STATUS_UNKNOWN)),              # 2: status 5, not text
    '1.470000018': ('eligible', None, _all(BSP_UNKNOWN)),                 # 4: bspMarket "yes", not a bool
    '1.470000019': ('excluded', 'month_not_evaluated', []),               # anchor May; path and t in April
    '1.470000020': ('eligible', None, [(1, NOT, NOT, False), (2, ISS, ISS, True), (3, NOT, NOT, False)]),  # April
    '1.470000021': ('unclassifiable', 'eventTypeId', []),
    '1.470000022': ('unclassifiable', 'marketType', []),
    '1.470000023': ('eligible', None, [(1, NOT, NOT, False), (2, ISS, ISS, True), (3, NOT, NOT, False)]),
    '1.470000024': ('eligible', None, _all(STATUS_UNKNOWN)),              # 2 before 3 (in play)
    '1.470000025': ('eligible', None, _all(RUNNER_STATUS_UNKNOWN, (None,))),  # 5 before 6
    '1.470000026': ('eligible', None, _all(RUNNER_STATUS_UNKNOWN, (None,))),  # status unknown before id 0
    '1.470000027': ('unclassifiable', 'eventTypeId', []),                 # eventTypeId 7, not text
    '1.470000028': ('eligible', None, _all(RUNNER_STATUS_UNKNOWN, (None,))),  # runner status 5, not text
}


def _standard_timing(off):
    return ms(off - timedelta(hours=2)), ms(off), ms(off + timedelta(seconds=OFFSET_S))


CONDITIONS_TIMING = dict({market_id: _standard_timing(OFF) for market_id in CONDITIONS_MARKETS}, **{
    '1.470000006': _standard_timing(CONDITIONS_LATE_SPLIT), '1.470000019': _standard_timing(MAY_FIRST),
    '1.470000020': _standard_timing(APRIL_LAST)})
_EIGHT = ms(OFF - timedelta(minutes=8))
CONDITIONS_INPUTS = dict({market_id: _STANDARD for market_id in (
    '1.470000001', '1.470000007', '1.470000008', '1.470000012', '1.470000014', '1.470000015', '1.470000017',
    '1.470000018', '1.470000024')}, **{
    '1.470000010': {1: (4.0, _EIGHT), 2: (2.0, _EIGHT), 3: (None, _EIGHT)},
    '1.470000011': _at({1: 3.0, 2: 2.5, 3: None}), '1.470000013': _at({0: 2.0, 1: 3.0, 2: 4.0}),
    '1.470000020': _at({1: 3.0, 2: 2.5, 3: 6.0}, ms(APRIL_LAST - timedelta(minutes=10))),
    # the off-10m republish of the same prices is no change: their pt stays off-30m
    '1.470000023': _at({1: 3.0, 2: 2.5, 3: 6.0}, ms(OFF - timedelta(minutes=30)))})
SEEDED_DRAWS_CONDITIONS = _draws(CONDITIONS_MARKETS)

KAT_CASE_IDS = ["s9_win_and_loss_in_two_markets", "s9_plus_and_minus_ten_in_one_market",
                "s9_plus_and_minus_ten_in_two_markets", "penny_rounding_against_us",
                "winnings_floor_and_commission_ceiling", "commission_half_up_rounding", "variants_are_never_netted",
                "void_is_unresolved_with_exposure", "dead_heat_is_unresolved_with_exposure",
                "own_runner_removed_after_decision_is_unresolved",
                "other_runner_removed_after_decision_is_unresolved", "result_revised_after_close_is_unresolved",
                "missing_bsp_is_unresolved"]
EXPECTED = {
    # engineering.KAT_CASES: 13 hand-worked cases, all passing (task c2b3a, accepted), exactly the runner's set.
    "kat": {"status": "passed", "cases": 13, "failed_cases": [], "case_ids": KAT_CASE_IDS},
    **SCENARIO_ANSWERS,
    # One winning bet at BSP 2.0675 (witness_outcome). P0: 100 x 1.0675 = 106.75, rounded down to 106; 5% of 106 =
    # 5.30, rounded up (ceil) to 6; net 100. Second point: 250 x 1.0675 = 266.875 -> 266; 2% of 266 = 5.32, half up
    # -> 5; net 261. Each rounding mode would give the other answer here (half up 5 at P0; ceil 6 at the second).
    "settlement_witness": {"p0": {"rules": P0_SETTLEMENT_RULES[:3], "bet": ["won", 106], "market": [106, 6, 100]},
                           "second_point": {"rules": SECOND_POINT_SETTLEMENT_RULES[:3], "bet": ["won", 266],
                                            "market": [266, 5, 261]}},
    # Two markets issue: the April race (its intent at its own t) and the market whose marketTime was revised before
    # t (its intent at t from the first-published off, not the revision).
    "market_conditions": {"markets": _expand(CONDITIONS_MARKETS, p0_rules()[0], CONDITIONS_TIMING, CONDITIONS_INPUTS,
                                             False),
                          "intents": _intent(100, -300) + [dict(_intent(100, -300)[0], effective_ms=ms(
                              APRIL_LAST - timedelta(minutes=5)))],
                          # both variants issue (the April race and 1.470000023), everything reconciles: passed
                          "reconciliation_status": "passed"},
    # The named market file is the primary (market_index docstring); a pre-t change in the event file changes nothing;
    # the same change in the market file moves the favourite, and so does the event file's change when it is alone.
    "sources_named_file_decides": {"primary": path(COMPETING), "event_file_change_same": True,
                                   "market_file_favourites": [2, 1], "event_alone_favourites": [3, 1]},
    # Each record: [primary member path, class, reason, the favourite issued] (market_index primary rule).
    "sources_primary_rules": {
        # the named file holds only a heartbeat, and it is still the primary: no view
        "r1_pair": {"records": {"1.460000001": [path("1.460000001"), "no_view", None, None]},
                    "refused_markets": [], "refused_members": []},
        # the first named file by path, though the second mentions the market first
        "two_named_files": {"records": {"1.460000002": [path("1.460000002"), "eligible", None, 1]},
                            "refused_markets": [], "refused_members": []},
        # no named file: the earliest mention (the second file by path), its directory without an event file
        "earliest_mention": {"records": {
            "1.460000003": [path("1.460000011", event="27000004"), "eligible", None, 3],
            "1.460000010": [path("1.460000010", event="27000003"), "eligible", None, 2],
            "1.460000011": [path("1.460000011", event="27000004"), "eligible", None, 2]},
            "refused_markets": [], "refused_members": []},
        # no named file: the earliest mention's directory has an event file, which never mentions it: no view
        "event_file_redirect": {"records": {
            "1.460000004": [path("27000005", event="27000005"), "no_view", None, None],
            "1.460000012": [path("1.460000012", event="27000005"), "eligible", None, 2],
            "1.460000013": [path("27000005", event="27000005"), "eligible", None, 2]},
            "refused_markets": [], "refused_members": []},
        # equal first mentions: the first by path
        "earliest_mention_tie": {"records": {
            "1.460000005": [path("1.460000015", event="27000007"), "eligible", None, 1],
            "1.460000015": [path("1.460000015", event="27000007"), "eligible", None, 2],
            "1.460000016": [path("1.460000016", event="27000008"), "eligible", None, 2]},
            "refused_markets": [], "refused_members": []},
        # the named file is refused at ingest (content month), so the market is refused: no record, and the event
        # file's record of it is listed; the event file's other market is a 2025-12 race, outside the split
        "refused_primary": {
            "records": {"1.460000014": [path("27000006", when=YEAR_END, event="27000006"), "excluded",
                                        "month_not_evaluated", None]},
            "refused_markets": [["1.460000006", path("27000006", when=YEAR_END, event="27000006"),
                                 "primary_source_refused"]],
            "refused_members": [[path("1.460000006", when=YEAR_END, event="27000006"),
                                 "content_month_sealed_holdout"]]}},
    # archive.py month gate: 2026-02 is the sealed holdout (month_sealed_holdout); the race on 2025-12-31 whose
    # post-t definition revises marketTime into 2026-01 is refused by its content (content_month_<reason>). The kept
    # market is a standard eligible one.
    "membership_gates": {"refused": [[path("1.410000003", when=YEAR_END, event="27000003"),
                                      "content_month_sealed_holdout"],
                                     [path("1.410000002", when=SEALED, event="27000002"), "month_sealed_holdout"]],
                         "decided": {"1.410000001": "eligible"}},
    # runner._first_pass: two records of one (plan, market id) refuse (cohort:<id>:duplicate_market).
    "duplicate_across_archives": {"refused": "cohort:1.420000001:duplicate_market"},
    # engineering.check_prefix: a prefix of exactly the limit passes; one more byte is unexercised
    # (prefix_buffer_limit, checked before the append); a prefix limit never changes the decision. limit_world's
    # prices: runner 2 is the favourite (2.5); with the batch at t, runner 3 (1.5); the seeded draw is runner 1. The
    # run's own tally (64 MiB) compares each small case and passes (both variants issue); at 64 MiB + 1 the run's
    # prefix is unexercised too, so incomplete.
    "limit_small_exact": _limit_expected("limit_small_exact", True, 4096, 2, True),
    "limit_small_over": _limit_expected("limit_small_over", False, 4096, 2, True),
    "limit_batch_exact": _limit_expected("limit_batch_exact", True, 4096, 3, True),
    "limit_batch_inside": _limit_expected("limit_batch_inside", False, 4095, 3, True),
    "limit_64mib_exact": _limit_expected("limit_64mib_exact", True, RUN_LIMIT, 3, True),
    "limit_64mib_over": _limit_expected("limit_64mib_over", False, RUN_LIMIT, 3, False),
    # Read limits (max_line_bytes 1024, the index's and the capture's): lines of at most 1024 bytes are read whole
    # (read_limits_exact, as limit_small_exact); a 1025-byte line before t stops the index too (line_too_long, no
    # pt), so the view ends at the last pt applied, before t: one selection-null row, unsupported
    # decision_view_unreadable (cohort market condition 1), and the capture meets the same line before its boundary.
    "read_limits_exact": _limit_expected("read_limits_exact", True, RUN_LIMIT, 2, True, 1024),
    "read_limits_unexercised": {
        "prefix": ["unexercised", "unknown_pt_before_boundary:line_too_long", None], "prefix_limit_bytes": RUN_LIMIT,
        "kept": LIMIT_KEPT["read_limits_unexercised"],
        "decision": {"population": "eligible", "reason": None, "rows": [[None, {
            "ltp_favourite": ["unsupported", "decision_view_unreadable", None, None],
            "seeded_random_runner": ["unsupported", "decision_view_unreadable", None, None]}]]},
        "run_tally": _run_tally(False, 1024, "unknown_pt_before_boundary:line_too_long")},
    # Two markets that are not BSP markets: both compared (prefix passed), nothing issued by either variant, so the
    # tally and the reconciliation are incomplete with both variants unissued (engineering docstring).
    "abstaining_incomplete": {"tally_status": "incomplete", "tally_compared": 2,
                              "unissued": ["ltp_favourite", "seeded_random_runner"], "unexercised_by_reason": {},
                              "reconciliation_status": "incomplete",
                              "unexercised_variants": ["ltp_favourite", "seeded_random_runner"]},
    # A line that is not JSON, or a price change with a decreasing pt, right after the last pt applied before t: the
    # index ends the view there (decisions are unsupported decision_view_unreadable, nothing issued) and each prefix
    # is unexercised before its boundary.
    "unexercised_incomplete": {"tally_status": "incomplete", "tally_compared": 0,
                               "unissued": ["ltp_favourite", "seeded_random_runner"],
                               "unexercised_by_reason": {"unknown_pt_before_boundary:invalid_or_decreasing_pt": 1,
                                                         "unknown_pt_before_boundary:not_json": 2},
                               "reconciliation_status": "incomplete",
                               "unexercised_variants": ["ltp_favourite", "seeded_random_runner"]},
    # Negative controls: each must be caught exactly as Astra's controls state.
    "nc_future_leak": {"leak_changes_decision": True,
                       "prefix": ["failed", "decision_differs:rows", "first_message_after_t"]},
    # The tally counts a failed comparison (compared and failed), so it is failed.
    "nc_pre_t_change": {"prefix": ["failed", "decision_differs:rows", "first_message_after_t"],
                        "tally": {"status": "failed", "failures": [["BASIC", "1.400000011", "prefix",
                                                                    "decision_differs:rows"]],
                                  "counts_reconcile": True,
                                  "counts": {"compared": 1, "failed": 1, "failed_against_record": 0, "missing": 0}}},
    "nc_borrowed_rows": {"prefix": ["failed", "decision_differs:rows", None],
                         "reconciliation_problems": ["decisions:1_not_eligible_with_rows",
                                                     "rows:3_not_their_decisions"],
                         "reconciliation_status": "failed"},
    "nc_ledger_byte": {"status": "failed", "problems": ["ledger:not_the_decisions"]},
    "nc_swapped_bets": {"status": "failed", "problems": ["bets:not_the_issued_rows"]},
    "nc_dropped_decision": {"status": "failed", "problems": ["decisions:missing_for_1_records"]},
    # Accounting settled every bet but the first (a seeded_random_runner bet): the settled bets are not the issued
    # rows, and that variant's issued count and stake are not resolved plus unresolved.
    "nc_short_settlement": {"status": "failed", "problems": [
        "settled:not_the_issued_rows", "seeded_random_runner:issued_not_resolved_plus_unresolved",
        "seeded_random_runner:issued_stake_not_s_r_plus_s_u"]},
    "nc_decision_without_record": {"status": "failed", "problems": ["decisions:1_without_an_input_record"]},
    "nc_duplicate_record": {"status": "failed", "problems": ["records:duplicate"]},
    "nc_other_variant_row": {"status": "failed", "problems": ["rows:1_without_exactly_the_contract_variants"]},
    # The settlement's rules declare one variant; its metrics then refuse the other variant's markets.
    "nc_settlement_other_variants": {"status": "failed", "problems": [
        "settlement:other_variants", "metrics:ValueError:undeclared_variant:seeded_random_runner"]},
    # A decision time 1 ms off is not the record's classification: failed at the record stage.
    "nc_wrong_decision_time": {"prefix": ["failed", "decision_not_the_records_classification", None],
                               "tally": {"status": "failed", "failures": [
                                   ["BASIC", "1.400000001", "record", "decision_not_the_records_classification"]],
                                   "counts_reconcile": True,
                                   "counts": {"compared": 0, "failed": 0, "failed_against_record": 1, "missing": 0}}},
    "nc_member_tampered": {"refused": "prefix:BASIC/2015/Jun/1/27000001/1.400000013.bz2:member_hash_mismatch"},
    "nc_markets_changed": {"refused": "markets_changed"},
    "nc_same_key_substitution": {"refused": "prefix_tally:1.400000001:decision_not_the_one_checked",
                                 "missing_after": 1},
    "nc_rejected_tally_addition": {"refused": "prefix_tally:1.400000013:malformed_prefix", "unchanged": True,
                                   "missing_after_retry": 0},
}
