"""Archive ingest, market index and coverage audit (task (b)) on synthetic TARs and synthetic bz2 market streams.

Every fixture is generated here in a temporary directory: no licensed or sample data is read or copied. These
tests show that the refusals, the month gate, idempotency, the hash checks, the decision/outcome split, partial
reads and the audit counts behave as documented on these synthetic shapes, and that only archive.py may import
tarfile, opening "r|" read streams only. They do not show that a real Betfair TAR uses the assumed member layout
or ends the way POSIX says (docs/backtest-plan.md section 2.8), nor anything about any strategy. The file-system
failure tests use chmod and are skipped when run as root, which ignores file modes.
"""

import ast
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
import bz2
import hashlib
import io
import json
import math
import os
from pathlib import Path
import random
import tarfile
import tempfile
import time
import unittest
from unittest import mock

from betangel_research.betfair.backtest import archive, coverage_audit, market_index
from betangel_research.betfair.backtest.__main__ import main
from betangel_research.betfair.backtest.archive import DEFAULT_LIMITS, ingest_archive
from betangel_research.betfair.backtest.coverage_audit import audit
from betangel_research.betfair.backtest.market_index import index_archives

REPO = Path(__file__).resolve().parent.parent
OFF = datetime(2015, 6, 1, 14, 0, tzinfo=timezone.utc)
EVENT = "27000001"
OUTCOME_TEXT = ("WINNER", "LOSER", "PLACED", "CLOSED")
OUTCOME_KEYS = ("bsp", "bspReconciled", "settledTime")
END = b"\0" * 1024
IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0
# The explicit empty decision_time, spelled out here rather than taken from market_index.no_view().
NO_VIEW = {"definition": {}, "first_published_market_time": None, "removals": [], "runners": [],
           "view_ends": {"pt": None, "reason": "primary_source_no_observation_before_cutoff"}}


def ms(when):
    return int(when.timestamp() * 1000)


def iso(when):
    return when.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def runner(rid, status="ACTIVE", af=None, bsp=None, removal=None):
    item = {"id": rid, "status": status, "sortPriority": rid, "name": f"Horse {rid}"}
    if af is not None:
        item["adjustmentFactor"] = af
    if bsp is not None:
        item["bsp"] = bsp
    if removal is not None:
        item["removalDate"] = removal
    return item


def definition(market_time, runners, status="OPEN", in_play=False, **extra):
    value = {"bspMarket": True, "turnInPlayEnabled": True, "persistenceEnabled": True, "marketBaseRate": 5.0,
             "eventId": EVENT, "eventTypeId": "7", "numberOfWinners": 1, "bettingType": "ODDS", "marketType": "WIN",
             "marketTime": market_time, "suspendTime": market_time, "bspReconciled": False, "complete": True,
             "inPlay": in_play, "crossMatching": True, "runnersVoidable": False, "betDelay": 0, "status": status,
             "numberOfActiveRunners": sum(1 for r in runners if r["status"] == "ACTIVE"), "runners": runners,
             "regulators": ["MR_INT"], "venue": "Synthetic Park", "countryCode": "GB", "discountAllowed": True,
             "timezone": "Europe/London", "openDate": market_time, "version": 1, "eventName": "Synthetic 1st Jun",
             "name": "1m Hcap"}
    value.update(extra)
    return value


def drop(value, *keys):
    """A market definition without some keys."""
    return {key: item for key, item in value.items() if key not in keys}


def mcm(when, changes):
    return {"op": "mcm", "clk": "1", "pt": ms(when), "mc": changes}


def md_change(market_id, when, value):
    return mcm(when, [{"id": market_id, "marketDefinition": value}])


def heartbeat(pt):
    """An mcm heartbeat with no mc, at pt (milliseconds)."""
    return {"op": "mcm", "clk": "h", "pt": pt, "ct": "HEARTBEAT"}


def lines(messages):
    return b"".join(json.dumps(m).encode() + b"\n" for m in messages)


def stream(messages):
    return bz2.compress(lines(messages))


def full_market(market_id="1.100000001", off=OFF):
    """Four runners: 3 removed before the off, 4 removed after the first-published off, a pre-off marketTime
    revision, one LTP, BSP at the off, CLOSED with one winner."""
    t = iso(off)
    base = [runner(1, af=50.0), runner(2, af=30.0), runner(3, af=10.0), runner(4, af=10.0)]
    removed3 = runner(3, "REMOVED", af=10.0, removal=iso(off - timedelta(minutes=90)))
    removed4 = runner(4, "REMOVED", af=10.0, removal=iso(off + timedelta(minutes=1)))
    later = iso(off + timedelta(minutes=10))
    return [
        md_change(market_id, off - timedelta(hours=2), definition(t, base)),
        md_change(market_id, off - timedelta(minutes=90), definition(t, [base[0], base[1], removed3, base[3]])),
        md_change(market_id, off - timedelta(minutes=60), definition(later, [base[0], base[1], removed3, base[3]])),
        mcm(off - timedelta(minutes=50), [{"id": market_id, "rc": [{"id": 1, "ltp": 2.5}]}]),
        md_change(market_id, off + timedelta(minutes=1), definition(later, [base[0], base[1], removed3, removed4])),
        md_change(market_id, off + timedelta(minutes=10),
                  definition(later, [runner(1, af=50.0, bsp=2.6), runner(2, af=30.0, bsp=3.1), removed3, removed4],
                             in_play=True, bspReconciled=True)),
        md_change(market_id, off + timedelta(minutes=20),
                  definition(later, [runner(1, "WINNER", af=50.0, bsp=2.6), runner(2, "LOSER", af=30.0, bsp=3.1),
                                     removed3, removed4], status="CLOSED", in_play=True, bspReconciled=True,
                             settledTime=iso(off + timedelta(minutes=20)))),
    ]


def closed_market(market_id, statuses, bsps, off=OFF, **extra):
    t = iso(off)
    opening = [runner(i + 1) for i in range(len(statuses))]
    final = [runner(i + 1, status, bsp=bsp) for i, (status, bsp) in enumerate(zip(statuses, bsps))]
    return [md_change(market_id, off - timedelta(hours=1), definition(t, opening, **extra)),
            md_change(market_id, off + timedelta(minutes=5),
                      definition(t, final, status="CLOSED", in_play=True, bspReconciled=True,
                                 settledTime=iso(off + timedelta(minutes=5)), **extra))]


def path(market, when=OFF, plan="BASIC", event=EVENT):
    month = when.strftime("%b")
    return f"{plan}/{when.year}/{month}/{when.day}/{event}/{market}.bz2"


def make_tar(target, members, fmt=tarfile.PAX_FORMAT, **options):
    """members: (name or TarInfo, bytes) for a regular file, or a TarInfo (no data) for any other type."""
    with tarfile.open(target, "w", format=fmt, **options) as tar:
        for member in members:
            if isinstance(member, tarfile.TarInfo):
                tar.addfile(member, io.BytesIO(b"\0" * member.size) if member.size else None)
                continue
            name, data = member
            info = name if isinstance(name, tarfile.TarInfo) else tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return target


def special(name, kind, linkname="target"):
    info = tarfile.TarInfo(name)
    info.type = kind
    if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
        info.linkname = linkname
    return info


def header_block(name, size=0, kind=tarfile.REGTYPE, size_field=None, fmt=tarfile.USTAR_FORMAT, patch=None):
    """One hand-made 512-byte header with a correct checksum; size_field replaces the 12-byte size field."""
    info = tarfile.TarInfo(name)
    info.size, info.type = size, kind
    block = bytearray(info.tobuf(fmt)[:512])
    if size_field is not None:
        block[124:136] = size_field
    for offset, value in (patch or {}).items():
        block[offset:offset + len(value)] = value
    block[148:156] = b" " * 8
    block[148:156] = b"%06o\0 " % sum(block)
    return bytes(block)


def pad(data):
    return data + b"\0" * (-len(data) % 512)


def member_offsets(tar_path):
    """{name: (header offset, data offset, size)} read with random access (tests only)."""
    with tarfile.open(tar_path) as tar:
        return {info.name: (info.offset, info.offset_data, info.size) for info in tar.getmembers()}


def walk(value):
    """Every key and every string value in a JSON value."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk(item)
    elif isinstance(value, str):
        yield value


def numbers(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from numbers(item)
    elif isinstance(value, list):
        for item in value:
            yield from numbers(item)
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        yield value


def all_pts(decision):
    """Every pt in a decision_time object."""
    pts = [entry[0] for timeline in decision["definition"].values() for entry in timeline]
    pts += [entry[0] for r in decision["runners"] for key, tl in r.items() if key not in ("id", "first_pt")
            for entry in tl]
    pts += [r["first_pt"] for r in decision["runners"]] + [item["pt"] for item in decision["removals"]]
    return pts


class Workspace(unittest.TestCase):
    """A temporary directory with a data root and a place for TARs, both outside the repository."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.root = self.tmp / "data"
        self.root.mkdir()
        self.roots = 0

    def new_root(self):
        self.roots += 1
        root = self.tmp / f"data{self.roots}"
        root.mkdir()
        return root

    def tar(self, members, name="archive.tar", **options):
        return make_tar(self.tmp / name, members, **options)

    def ingest(self, members, name="archive.tar", root=None, tar_options=None, **limits):
        return ingest_archive(self.tar(members, name, **(tar_options or {})), root or self.root,
                              DEFAULT_LIMITS.stricter(**limits) if limits else DEFAULT_LIMITS)

    def assert_refused(self, result, problem):
        self.assertEqual(result["status"], "refused", result)
        self.assertIn(problem, result["problems"])

    def records(self, sha, root=None):
        lines_ = ((root or self.root) / "index" / sha / market_index.MARKETS_FILE).read_bytes().splitlines()
        return {record["market_id"]: record for record in map(json.loads, lines_)}

    def head(self, sha, root=None):
        return json.loads(((root or self.root) / "index" / sha / market_index.HEAD_FILE).read_bytes())

    def build(self, members, root=None):
        root = root or self.root
        result = ingest_archive(self.tar(members, f"build{self.roots}.tar"), root)
        self.assertEqual(result["status"], "ingested", result)
        indexed = index_archives(root)
        self.assertEqual(indexed["status"], "indexed", indexed)
        return result["archive_sha256"], indexed

    def chmod(self, place, mode):
        """Change a mode for one test and restore it afterwards, so the temporary directory can be removed."""
        old = place.stat().st_mode & 0o777
        place.chmod(mode)
        self.addCleanup(place.chmod, old)


class ArchiveRefusalTests(Workspace):
    def good(self, market="1.100000001"):
        return (path(market), stream(full_market(market)))

    def test_unsafe_member_types_and_names_refuse_the_whole_archive(self):
        cases = {
            "absolute path": ("/" + path("1.100000002"), "absolute_path"),
            "drive letter": ("C:" + path("1.100000002"), "absolute_path"),
            "parent component": ("BASIC/../" + path("1.100000002"), "parent_component"),
            "space in a name": (path("1.100000002").replace("BASIC/", "BASIC /"), "unsafe_name"),
            "non-ASCII name": (path("1.100000002").replace("BASIC", "BÄSIC"), "unsafe_name"),
            "hidden file": ("BASIC/._x.bz2", "unsafe_name"),
            "dot component": ("BASIC/./x.bz2", "unsafe_name"),
            "backslash": ("BASIC\\x.bz2", "unsafe_name"),
            "nine components": ("BASIC/a/b/c/d/e/f/g/h.bz2", "name_too_long"),
        }
        for label, (name, reason) in cases.items():
            with self.subTest(case=label):
                result = self.ingest([self.good(), (name, stream(full_market("1.100000002")))])
                self.assertEqual(result["status"], "refused", result)
                self.assertTrue(any(p.endswith(":" + reason) for p in result["problems"]), result)
                self.assertFalse((self.root / "raw").exists())
                self.assertFalse((self.root / "manifests").exists())
        # Eight components is the most allowed: refused by the month gate (another layout), not as too long.
        eight = self.ingest([self.good(), ("BASIC/a/b/c/d/e/f/h.bz2", b"x")], root=self.new_root())
        self.assertEqual((eight["status"], eight["refused_by_reason"]), ("ingested", {"no_parseable_month": 1}))
        kinds = {"symlink": tarfile.SYMTYPE, "hardlink": tarfile.LNKTYPE, "device": tarfile.CHRTYPE,
                 "fifo": tarfile.FIFOTYPE, "not_regular": tarfile.CONTTYPE}
        for reason, kind in kinds.items():
            with self.subTest(kind=reason):
                info = special("BASIC/2015/Jun/1/27000001/1.100000009.bz2", kind)
                result = self.ingest([self.good(), info])
                self.assert_refused(result, f"BASIC/2015/Jun/1/27000001/1.100000009.bz2:{reason}")
                self.assertFalse((self.root / "raw").exists())
        block = special("BASIC/dev", tarfile.BLKTYPE)
        self.assert_refused(self.ingest([self.good(), block]), "BASIC/dev:device")
        # A pax-encoded GNU sparse file is not regular either.
        sparse = tarfile.TarInfo(path("1.100000009"))
        sparse.size = 10
        sparse.pax_headers = {"GNU.sparse.map": "0,10", "GNU.sparse.size": "10"}
        self.assert_refused(self.ingest([self.good(), sparse]), f"{path('1.100000009')}:not_regular")

    def test_duplicate_case_and_directory_collisions(self):
        name = path("1.100000001")
        self.assert_refused(self.ingest([self.good(), (name, b"other")]), f"{name}:duplicate_name")
        upper = name.replace("BASIC", "basic")
        self.assert_refused(self.ingest([self.good(), (upper, stream(full_market()))]), f"{upper}:case_collision")
        # A file named like another member's directory, and a directory entry colliding with a file by case.
        directory_file = f"BASIC/2015/Jun/1/{EVENT}"
        self.assert_refused(self.ingest([self.good(), (directory_file, b"x")]),
                            f"{directory_file}:file_directory_collision")
        folder = special(name.upper(), tarfile.DIRTYPE)
        self.assert_refused(self.ingest([self.good(), folder]), f"{name.upper()}:case_collision")

    def test_size_and_count_limits(self):
        two = [self.good(), (path("1.100000002"), stream(full_market("1.100000002")))]
        self.assert_refused(self.ingest(two, max_members=1), "archive:too_many_members")
        self.assert_refused(self.ingest(two, max_member_bytes=50), f"{path('1.100000001')}:member_too_large")
        first = len(two[0][1])
        self.assert_refused(self.ingest(two, max_total_bytes=first + 1), "archive:total_size_over_limit")
        self.assert_refused(self.ingest(two, max_archive_bytes=1000), "archive:too_large")
        self.assert_refused(self.ingest(two, max_path_chars=20), f"{path('1.100000001')}:name_too_long")
        # A member refused by its name is never decompressed: a bomb under a too-long name adds no other problem.
        bomb = bz2.compress(b"\0" * (4 << 20))
        self.assertEqual(self.ingest([(path("1.100000001"), bomb)], max_path_chars=20)["problems"],
                         [f"{path('1.100000001')}:name_too_long"])
        plain = len(lines(full_market()))
        self.assert_refused(self.ingest(two, max_member_decompressed_bytes=plain - 1),
                            f"{path('1.100000001')}:decompressed_too_large")
        self.assert_refused(self.ingest(two, max_total_decompressed_bytes=plain + 10),
                            "archive:total_decompressed_over_limit")
        self.assertFalse((self.root / "raw").exists())
        # Limits can only be lowered.
        for change in ({"max_ratio": DEFAULT_LIMITS.max_ratio + 1}, {"max_members": 0}, {"max_ratio": 2.0},
                       {"max_ratio": True}, {"no_such_limit": 1}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                DEFAULT_LIMITS.stricter(**change)
        with self.assertRaises(ValueError):
            archive.Limits(**{**archive.LIMIT_CEILINGS, "max_ratio": archive.LIMIT_CEILINGS["max_ratio"] + 1})
        stricter = DEFAULT_LIMITS.stricter(max_ratio=10)
        with self.assertRaises(ValueError):
            stricter.stricter(max_ratio=11)

    def test_limits_are_inclusive_and_their_ceilings_are_pinned(self):
        self.assertEqual(archive.LIMIT_CEILINGS, {
            "max_archive_bytes": 64 << 30, "max_members": 400_000, "max_member_bytes": 1 << 30,
            "max_total_bytes": 64 << 30, "max_member_decompressed_bytes": 8 << 30,
            "max_total_decompressed_bytes": 1 << 40, "max_ratio": 200, "max_path_chars": 200,
            "max_line_bytes": 16 << 20, "max_extended_header_bytes": 64 << 10})
        self.assertEqual((archive.MAX_COMPONENTS, archive.MAX_EXTENDED_HEADERS, archive.END_OF_ARCHIVE_BYTES),
                         (8, 4, 1024))
        name, data = self.good()
        plain = len(lines(full_market()))
        tar_bytes = len(make_tar(self.tmp / "measure.tar", [(name, data)]).read_bytes())
        ratio = plain / len(data)
        edges = {"max_path_chars": len(name), "max_member_bytes": len(data), "max_total_bytes": len(data),
                 "max_archive_bytes": tar_bytes, "max_member_decompressed_bytes": plain,
                 "max_total_decompressed_bytes": plain, "max_ratio": math.ceil(ratio)}
        for limit, value in edges.items():
            with self.subTest(limit=limit):
                self.assertEqual(self.ingest([(name, data)], root=self.new_root(), **{limit: value})["status"],
                                 "ingested")
                if limit != "max_ratio" or math.floor(ratio) * len(data) < plain:
                    lower = value - 1 if limit != "max_ratio" else math.floor(ratio)
                    self.assertEqual(self.ingest([(name, data)], root=self.new_root(), **{limit: lower})["status"],
                                     "refused")

    def test_ratio_cap_is_inclusive(self):
        data = stream(full_market())
        plain = len(lines(full_market()))
        chunks = iter([data])
        limits = DEFAULT_LIMITS.stricter(max_ratio=1)
        self.assertEqual(archive._decompress_check(lambda n: next(chunks, b""), plain, limits, plain)[0], plain)
        chunks = iter([data])
        with self.assertRaises(archive.ArchiveProblem) as raised:
            archive._decompress_check(lambda n: next(chunks, b""), plain - 1, limits, plain)
        self.assertEqual(str(raised.exception), "compression_ratio_over_limit")

    def test_bz2_bombs_and_broken_members(self):
        bomb = bz2.compress(b"\0" * (4 << 20))
        self.assertLess(len(bomb), 200)
        name = path("1.100000002")
        self.assert_refused(self.ingest([self.good(), (name, bomb)]), f"{name}:compression_ratio_over_limit")
        good = stream(full_market("1.100000002"))
        cases = {"not_bz2": b"plain text, not bz2", "truncated_bz2": good[:-12], "empty_member": b"",
                 "trailing bytes": good + b"junk"}
        for label, data in cases.items():
            with self.subTest(case=label):
                reason = "not_bz2" if label == "trailing bytes" else label
                self.assert_refused(self.ingest([self.good(), (name, data)]), f"{name}:{reason}")
        # A second bz2 stream after the first is accepted (multi-stream files decompress as one).
        doubled = bz2.compress(b'{"op":"mcm","pt":1,"mc":[]}\n') + good
        self.assertEqual(self.ingest([self.good(), (name, doubled)])["status"], "ingested")

    def test_failed_members_spend_the_decompression_allowance(self):
        """R3: the work spent on a member that fails is charged to the archive's allowance, and once the allowance
        is spent nothing more is decompressed, so total work stays within the allowance plus one chunk however many
        members fail. Work is measured on BZ2Decompressor itself: each call's output, or its max_length when it
        raises, because libbz2 checks a block's CRC only after producing the block and a failed call drops that
        output, which yielded bytes cannot show. Astra's case: three 2 MiB members of random bytes under a 1 MiB
        allowance (5,399,886 bytes of work before). Second: three members each cut inside their last bz2 block, so
        each decompresses about 1.8 MB before truncated_bz2, under a 2 MiB allowance. Third: forty 48-byte members
        whose block CRC is corrupt; each failing call has made 1,000,000 bytes, so they used to cost about 40 MB
        against a 1 MiB allowance, charged nothing; now each failed call is charged a whole chunk."""
        real, work, raised = bz2.BZ2Decompressor, [], []

        class Counting:
            def __init__(self):
                self.inner = real()

            def decompress(self, data, max_length=-1):
                try:
                    out = self.inner.decompress(data, max_length)
                except Exception:
                    work.append(max_length)
                    raised.append(max_length)
                    raise
                work.append(len(out))
                return out

            def __getattr__(self, name):
                return getattr(self.inner, name)

        names = [path(f"1.1000000{n:02d}") for n in range(1, 41)]
        noise = bz2.compress(random.Random(7).randbytes(2 << 20))
        cut = bz2.compress(random.Random(8).randbytes(2 << 20))[:-1000]
        corrupt = bytearray(bz2.compress(b"\0" * 1_000_000))
        corrupt[10] ^= 0xFF                                         # the block CRC stored after the block magic
        corrupt = bytes(corrupt)
        self.assertEqual(len(real().decompress(corrupt, 999_999)), 999_999)     # output made before the CRC check
        with self.assertRaises(OSError):
            real().decompress(corrupt, archive.CHUNK_BYTES)
        cases = {"over the allowance": (noise, 3, 1 << 20, ["archive:total_decompressed_over_limit"], 0),
                 "truncated members": (cut, 3, 2 << 20, [f"{names[0]}:truncated_bz2",
                                                         "archive:total_decompressed_over_limit"], 0),
                 "block CRC mismatch": (corrupt, 40, 1 << 20, [f"{names[0]}:not_bz2", f"{names[1]}:not_bz2",
                                                               "archive:total_decompressed_over_limit"], 2)}
        for label, (data, count, allowance, problems, failed_calls) in cases.items():
            with self.subTest(case=label):
                work.clear()
                raised.clear()
                with mock.patch.object(archive.bz2, "BZ2Decompressor", Counting):
                    result = self.ingest([(name, data) for name in names[:count]], root=self.new_root(),
                                         max_total_decompressed_bytes=allowance)
                self.assertEqual(result, {"status": "refused", "problems": problems})
                self.assertEqual(len(raised), failed_calls)
                self.assertLessEqual(max(work), archive.CHUNK_BYTES)
                self.assertLessEqual(sum(work), allowance + archive.CHUNK_BYTES, work)

    def test_pax_gnu_sparse_is_refused_before_tarfile_reads_a_sparse_map(self):
        """R4: a pax GNU sparse 1.0 member (major 1, minor 0, realsize 0) whose data opens with a 50,000-entry map
        (200 KB), under a 1 KiB member limit. tarfile's own hook reads that whole map, after fetching the member's
        header and before any check in _scan, so the refusal must come first. The stdlib hooks are wrapped: plain
        tarfile runs one and builds 50,000 sparse entries (the probe is live), the ingest runs none. Every other
        GNU.sparse keyword, alone or in another version's set, in any case, is refused too before tarfile acts on
        it; libarchive's ordinary pax keywords still pass (RealContainerShapeTests)."""
        hooks = ("_proc_gnusparse_00", "_proc_gnusparse_01", "_proc_gnusparse_10")
        originals, built = {hook: getattr(tarfile.TarInfo, hook) for hook in hooks}, []

        def probe(hook):
            def run(self, member, *args):
                originals[hook](self, member, *args)
                built.append((hook, len(member.sparse)))
            return run

        name = path("1.100000002")

        def sparse_tar(headers, data):
            info = tarfile.TarInfo(name)
            info.pax_headers = headers
            return self.tar([self.good(), (info, data)], name="sparse.tar")

        mapped = sparse_tar({"GNU.sparse.major": "1", "GNU.sparse.minor": "0", "GNU.sparse.realsize": "0"},
                            b"50000\n" + b"0\n0\n" * 50000)
        with mock.patch.multiple(tarfile.TarInfo, **{hook: probe(hook) for hook in hooks}):
            with tarfile.open(mapped, "r|") as tar:
                self.assertEqual([info.issparse() for info in tar], [False, True])
            self.assertEqual(built, [("_proc_gnusparse_10", 50000)])
            built.clear()
            result = ingest_archive(mapped, self.root, DEFAULT_LIMITS.stricter(max_member_bytes=1024))
            self.assertEqual(result, {"status": "refused", "problems": [f"{name}:not_regular"]})
            self.assertEqual(built, [])
            data = stream(full_market("1.100000002"))           # a member that is fine without the keyword
            variants = {
                "0.0": {"GNU.sparse.size": "10", "GNU.sparse.numblocks": "1", "GNU.sparse.offset": "0",
                        "GNU.sparse.numbytes": "10"},
                "0.1": {"GNU.sparse.map": "0,10", "GNU.sparse.size": "10", "GNU.sparse.name": name},
                "1.0": {"GNU.sparse.major": "1", "GNU.sparse.minor": "0", "GNU.sparse.name": name,
                        "GNU.sparse.realsize": "10"},
                "another version": {"GNU.sparse.major": "2", "GNU.sparse.minor": "0"},
                "name": {"GNU.sparse.name": name}, "realsize": {"GNU.sparse.realsize": str(len(data))},
                "size": {"GNU.sparse.size": str(len(data))}, "numblocks": {"GNU.sparse.numblocks": "0"},
                "offset": {"GNU.sparse.offset": "0"}, "minor": {"GNU.sparse.minor": "0"},
                "any case": {"gnu.SPARSE.realsize": str(len(data))},          # stdlib ignores it; refused anyway
            }
            for label, headers in variants.items():
                with self.subTest(variant=label):
                    result = ingest_archive(sparse_tar(headers, data), self.new_root())
                    self.assertEqual(result, {"status": "refused", "problems": [f"{name}:not_regular"]})
                    self.assertEqual(built, [])
            plain = ingest_archive(sparse_tar({"mtime": "1557303870.6957880"}, data), self.new_root())
            self.assertEqual((plain["status"], plain["members"]), ("ingested", 2))

    def test_not_a_tar_and_empty_tar(self):
        junk = self.tmp / "junk.tar"
        junk.write_bytes(b"\x01" * 4096)
        self.assert_refused(ingest_archive(junk, self.root), "archive:not_a_readable_tar")
        self.assert_refused(ingest_archive(self.tmp / "missing.tar", self.root), "archive:not_a_regular_file")
        self.assert_refused(ingest_archive(self.tmp, self.root), "archive:not_a_regular_file")
        self.assert_refused(self.ingest([]), "archive:no_members")

    def test_declared_size_beyond_the_file_is_refused_at_once(self):
        """A 2 KiB file whose header declares up to 2**60 bytes: tarfile would read empty blocks for ever."""
        name, data = self.good()
        fields = {"octal 8 GiB": b"%011o\0" % 0o77777777777,
                  "base-256 2**60": b"\x80" + (1 << 60).to_bytes(11, "big")}
        for label, field in fields.items():
            for kind in (tarfile.REGTYPE, b"Z", tarfile.CONTTYPE):
                with self.subTest(size=label, kind=kind):
                    huge = self.tmp / "huge.tar"
                    huge.write_bytes(header_block(name, size_field=field, kind=kind) + pad(data) + END)
                    started = time.monotonic()
                    self.assert_refused(ingest_archive(huge, self.root), f"{name}:size_beyond_archive_end")
                    self.assertLess(time.monotonic() - started, 5)
        self.assertFalse((self.root / "raw").exists())

    def test_an_archive_must_end_cleanly(self):
        """A cut, corrupted, padded or concatenated TAR must not read as complete with members missing."""
        two = [self.good(), (path("1.100000002"), stream(full_market("1.100000002")))]
        source = self.tar(two, name="two.tar", fmt=tarfile.USTAR_FORMAT)
        whole = source.read_bytes()
        offsets = member_offsets(source)
        second, data2, size2 = offsets[path("1.100000002")]
        end2 = data2 + size2 + (-size2 % 512)
        bad_checksum = bytearray(whole)
        bad_checksum[second + 148:second + 156] = b"9999999\0"
        other = self.tar([self.good("1.100000003")], name="other.tar", fmt=tarfile.USTAR_FORMAT).read_bytes()
        negative = io.BytesIO()
        with tarfile.open(fileobj=negative, mode="w", format=tarfile.PAX_FORMAT) as tar:
            name, data = self.good()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
            info = tarfile.TarInfo(path("1.100000002"))
            info.size, info.pax_headers = len(two[1][1]), {"size": "-1"}   # tarfile swallows its own error here
            tar.addfile(info, io.BytesIO(two[1][1]))
        cases = {"cut at the second header": whole[:second], "cut inside the second header": whole[:second + 100],
                 "no end-of-archive blocks": whole[:end2], "one zero block only": whole[:end2 + 512],
                 "second header checksum corrupted": bytes(bad_checksum),
                 "garbage after the first member": whole[:second] + b"\xff" * 4096,
                 "zero blocks, then a member": whole[:second] + END + whole[second:],
                 "two TARs concatenated": whole + other, "pax size of -1": negative.getvalue()}
        for label, blob in cases.items():
            with self.subTest(case=label):
                cut = self.tmp / "cut.tar"
                cut.write_bytes(blob)
                self.assert_refused(ingest_archive(cut, self.root), "archive:not_ended_by_zero_blocks")
                self.assertFalse((self.root / "raw").exists())
        # The intact file, and the same members with exactly two zero blocks after them, are complete.
        exact = self.tmp / "exact.tar"
        exact.write_bytes(whole[:end2] + END)
        self.assertEqual(ingest_archive(exact, self.root)["status"], "ingested")

    def test_extended_headers_are_bounded_before_tarfile_reads_them(self):
        good = self.good()
        long_name = "BASIC/" + "a" * 70_000 + ".bz2"
        self.assert_refused(self.ingest([good, (long_name, b"x")], tar_options={"fmt": tarfile.GNU_FORMAT}),
                            "archive:extended_header_too_large")
        commented = tarfile.TarInfo(path("1.100000002"))
        commented.pax_headers = {"comment": "A" * 70_000}
        self.assert_refused(self.ingest([good, commented]), "archive:extended_header_too_large")
        commented.pax_headers = {"comment": "A" * 1_000}
        self.assert_refused(self.ingest([good, commented], max_extended_header_bytes=512),
                            "archive:extended_header_too_large")
        # A small pax header is fine (the commented member is then refused only as empty).
        self.assert_refused(self.ingest([good, commented]), f"{path('1.100000002')}:empty_member")
        self.assert_refused(self.ingest([good], tar_options={"pax_headers": {"comment": "global"}}),
                            "archive:pax_global_header")
        # Chained extended headers: tarfile recurses once per header.
        name, data = good
        record = b"20 comment=abcdefgh\n"
        for count, expected in ((4, "ingested"), (5, "refused"), (5000, "refused")):
            with self.subTest(chain=count):
                blob = b"".join(header_block("PaxHeader/x", len(record), tarfile.XHDTYPE) + pad(record)
                                for _ in range(count))
                chained = self.tmp / "chained.tar"
                chained.write_bytes(blob + header_block(name, len(data)) + pad(data) + END)
                result = ingest_archive(chained, self.new_root())
                self.assertEqual(result["status"], expected, result)
                if expected == "refused":
                    self.assertEqual(result["problems"], ["archive:extended_header_chain"])
        # An old GNU sparse header whose extension block is cut: tarfile itself would raise IndexError.
        sparse = self.tmp / "sparse.tar"
        sparse.write_bytes(header_block(name, 10, tarfile.GNUTYPE_SPARSE, fmt=tarfile.GNU_FORMAT,
                                        patch={482: b"\x01"}))
        self.assert_refused(ingest_archive(sparse, self.root), f"{name}:not_regular")

    def test_data_root_must_be_outside_git_and_the_repository(self):
        good = self.tar([self.good()])
        inside = REPO / "no-such-data-root"
        result = ingest_archive(good, inside)
        self.assert_refused(result, "data_root:inside_repository")
        if (REPO / ".git").exists():                       # an exported tree has no .git; the rule above still holds
            self.assertIn("data_root:inside_git_worktree", result["problems"])
        self.assertFalse(inside.exists())
        worktree = self.tmp / "work"
        (worktree / ".git").mkdir(parents=True)
        (worktree / "data").mkdir()
        self.assert_refused(ingest_archive(good, worktree / "data"), "data_root:inside_git_worktree")
        (self.tmp / "gitfile").mkdir()
        (self.tmp / "gitfile" / ".git").write_text("gitdir: elsewhere\n")
        self.assert_refused(ingest_archive(good, self.tmp / "gitfile"), "data_root:inside_git_worktree")
        (self.tmp / "dangling" / "data").mkdir(parents=True)
        (self.tmp / "dangling" / ".git").symlink_to(self.tmp / "nowhere")
        self.assert_refused(ingest_archive(good, self.tmp / "dangling" / "data"), "data_root:inside_git_worktree")
        self.assert_refused(ingest_archive(good, self.tmp / "absent"), "data_root:missing")
        self.assert_refused(ingest_archive(good, good), "data_root:not_a_directory")
        self.assert_refused(ingest_archive(good, ""), "data_root:missing")
        # A Git worktree below the root (raw/ or manifests/) never receives licensed bytes.
        for folder, problem in (("raw", f"raw:{path('1.100000001')}:unsafe_target"), ("manifests", "unsafe_target")):
            with self.subTest(below=folder):
                root = self.new_root()
                (root / folder / ".git").mkdir(parents=True)
                result = ingest_archive(good, root)
                self.assertEqual(result["status"], "refused", result)
                self.assertTrue(any(p.endswith(problem) for p in result["problems"]), result)
                self.assertEqual([p.name for p in (root / folder).iterdir()], [".git"])

    def test_repository_is_compared_by_identity(self):
        """A different spelling of the repository (here another case, on a case-insensitive file system) is still
        the repository."""
        fake_repo = self.tmp / "Repo"
        (fake_repo / "data").mkdir(parents=True)
        variant = Path(str(fake_repo).replace("/Repo", "/REPO")) / "data"
        with mock.patch.object(archive, "REPOSITORY_ROOT", fake_repo):
            self.assertIn("data_root:inside_repository", archive.check_data_root(fake_repo / "data")[1])
            if variant.exists():
                self.assertIn("data_root:inside_repository", archive.check_data_root(variant)[1])
            link = self.tmp / "link"
            link.symlink_to(fake_repo)
            self.assertIn("data_root:inside_repository", archive.check_data_root(link / "data")[1])


class MonthGateTests(Workspace):
    def test_each_refused_month_class_is_listed_and_not_written(self):
        cases = {
            datetime(2026, 3, 4, tzinfo=timezone.utc): "month_sealed_holdout",
            datetime(2026, 1, 1, tzinfo=timezone.utc): "month_sealed_holdout",
            datetime(2026, 8, 31, tzinfo=timezone.utc): "month_sealed_holdout",
            datetime(2026, 9, 2, tzinfo=timezone.utc): "month_exposed_exploratory_only",
            datetime(2026, 10, 12, tzinfo=timezone.utc): "month_not_prospective",
            datetime(2026, 11, 1, tzinfo=timezone.utc): "month_prospective_out_of_scope",
            datetime(2027, 1, 1, tzinfo=timezone.utc): "month_prospective_out_of_scope",
        }
        members, expected = [(path("1.100000001"), stream(full_market()))], {}
        for number, (when, reason) in enumerate(sorted(cases.items()), start=2):
            name = path(f"1.10000000{number}", when, event=str(28000000 + number))
            members.append((name, stream(full_market(f"1.10000000{number}", when))))
            expected[name] = reason
        unparsed = {
            f"BASIC/{EVENT}/1.100000020.bz2": "no_parseable_month",          # the historic API's undated layout
            f"BASIC/2015/Sept/1/{EVENT}/1.100000021.bz2": "no_parseable_month",
            f"GOLD/2015/Jun/1/{EVENT}/1.100000022.bz2": "no_parseable_month",
            f"BASIC/2015/Jun/1/{EVENT}/1.100000023.json": "no_parseable_month",
            f"BASIC/2015/Feb/30/{EVENT}/1.100000024.bz2": "invalid_date_in_path",
            f"BASIC/2015/Jun/1/{EVENT}/readme.bz2": "unrecognised_file_name",
            f"BASIC/2015/Jun/1/{EVENT}/27000002.bz2": "unrecognised_file_name",
        }
        members += [(name, b"never decompressed " + name.encode()) for name in unparsed]
        expected.update(unparsed)
        # Path says 2025-12, content says 2026-02: refused by the content scan.
        smuggled = path("1.100000030", datetime(2025, 12, 31, tzinfo=timezone.utc), event="29000000")
        members.append((smuggled, stream(full_market("1.100000030", datetime(2026, 2, 1, 14, tzinfo=timezone.utc)))))
        expected[smuggled] = "content_month_sealed_holdout"
        result = self.ingest(members)
        self.assertEqual(result["status"], "ingested", result)
        # Seven members were refused before their month was known, so the holdout count is unknown, not zero.
        self.assertEqual((result["members"], result["refused"], result["sealed_holdout_members"],
                          result["month_unknown_members"]), (1, len(expected), None, 7))
        self.assertEqual(sum(count for reason, count in result["refused_by_reason"].items()
                             if reason.endswith("month_sealed_holdout")), 4)
        manifest = json.loads((self.root / "manifests" / f"{result['archive_sha256']}.json").read_bytes())
        self.assertEqual({item["path"]: item["reason"] for item in manifest["refused"]}, expected)
        data = dict(members)
        for item in manifest["refused"]:
            # The evidence section 3 relies on: the exact hash and size of every refused member, content-gated too.
            self.assertEqual((item["sha256"], item["size_bytes"]),
                             (hashlib.sha256(data[item["path"]]).hexdigest(), len(data[item["path"]])))
            self.assertFalse((self.root / "raw" / result["archive_sha256"] / item["path"]).exists())
            self.assertEqual(item["content_months"], ["2026-02"] if item["path"] == smuggled else None)
        self.assertEqual([m["path"] for m in manifest["members"]], [path("1.100000001")])
        self.assertEqual(manifest["month_gate"]["sealed_holdout"], ["2026-01", "2026-08"])
        self.assertEqual(manifest["month_gate"]["refused_from_month"], "2026-01")

    def test_an_archive_that_keeps_nothing_is_refused_with_its_counts(self):
        holdout = full_market("1.400000001", datetime(2026, 3, 1, 14, tzinfo=timezone.utc))
        undated = self.ingest([("BASIC/32000001/1.400000001.bz2", stream(holdout))])
        self.assertEqual(undated["problems"], ["archive:nothing_kept"])
        self.assertEqual((undated["sealed_holdout_members"], undated["month_unknown_members"],
                          undated["refused_by_reason"]), (None, 1, {"no_parseable_month": 1}))
        self.assertEqual(len(undated["archive_sha256"]), 64)
        sealed = self.ingest([(path("1.400000001", datetime(2026, 3, 1, tzinfo=timezone.utc)), stream(holdout))])
        self.assertEqual((sealed["problems"], sealed["sealed_holdout_members"], sealed["month_unknown_members"]),
                         (["archive:nothing_kept"], 1, 0))
        self.assertFalse((self.root / "manifests").exists())
        self.assertFalse((self.root / "raw").exists())

    def test_inspected_month_is_kept_and_flagged_and_2015_is_plain(self):
        april = datetime(2017, 4, 29, 13, 35, tzinfo=timezone.utc)
        june = datetime(2015, 6, 2, 14, tzinfo=timezone.utc)
        result = self.ingest([(path("1.100000001"), stream(full_market())),
                              (path("1.130000001", april, plan="PRO", event="28209874"),
                               stream(full_market("1.130000001", april))),
                              # By path only, and by content only.
                              (path("1.130000002", april, plan="PRO", event="28209875"),
                               stream(full_market("1.130000002", june))),
                              (path("1.130000003", june, plan="PRO", event="28209876"),
                               stream(full_market("1.130000003", april)))])
        manifest = json.loads((self.root / "manifests" / f"{result['archive_sha256']}.json").read_bytes())
        flags = {item["market_id"]: item["inspected_month"] for item in manifest["members"]}
        self.assertEqual(flags, {"1.100000001": False, "1.130000001": True, "1.130000002": True, "1.130000003": True})
        self.assertEqual((manifest["plan"], manifest["plans"]), ("mixed", ["BASIC", "PRO"]))
        self.assertEqual(manifest["months"], ["2015-06", "2017-04"])

    def test_manifest_lists_are_sorted_whatever_the_tar_order(self):
        members = [special("BASIC/2015/Jun/1", tarfile.DIRTYPE), special("BASIC/2015/Jun", tarfile.DIRTYPE)]
        members += [(path(f"1.10000000{n}"), stream(full_market(f"1.10000000{n}"))) for n in (3, 2, 1)]
        members += [(f"BASIC/{EVENT}/1.10000000{n}.bz2", b"x") for n in (9, 8)]
        result = self.ingest(members)
        manifest = json.loads((self.root / "manifests" / f"{result['archive_sha256']}.json").read_bytes())
        self.assertEqual([m["path"] for m in manifest["members"]], [path(f"1.10000000{n}") for n in (1, 2, 3)])
        self.assertEqual([m["path"] for m in manifest["refused"]], [f"BASIC/{EVENT}/1.10000000{n}.bz2" for n in (8, 9)])
        self.assertEqual(manifest["directories"], ["BASIC/2015/Jun", "BASIC/2015/Jun/1"])

    def test_content_scan_crosses_chunk_boundaries(self):
        """With 7-byte chunks every "marketTime" value straddles chunks; the scan's tail must still find it."""
        smuggled = path("1.100000030", datetime(2025, 12, 31, tzinfo=timezone.utc), event="29000000")
        content = stream(full_market("1.100000030", datetime(2026, 2, 1, 14, tzinfo=timezone.utc)))
        with mock.patch.object(archive, "CHUNK_BYTES", 7):
            result = self.ingest([(path("1.100000001"), stream(full_market())), (smuggled, content)])
        self.assertEqual(result["refused_by_reason"], {"content_month_sealed_holdout": 1})

    def test_a_market_time_cut_by_a_chunk_boundary_counts_once_by_its_utc_month(self):
        """N1: the first 1 MiB decompressed chunk ends just after "2026-01" of "2026-01-01T00:00:00+01:00", which is
        2025-12-31T23:00 in UTC. The cut value is carried into the next chunk and read whole, so the member's only
        content month is 2025-12 and it is kept; the literal 2026-01 used to be recorded too and refused it as
        sealed holdout. A value the member itself ends inside still counts by its literal month, and so does one cut
        after its month whose match (200 spaces before the colon here) starts too early to be carried in the tail:
        it would otherwise be lost in both chunks and a sealed-holdout member stored."""
        when, mid = datetime(2025, 12, 31, tzinfo=timezone.utc), "1.100000043"
        opening, closing = b'{"op": "mcm", "pt": 1, "mc": [], "pad": "', b'"}\n'

        def cut_after(head, rest):
            """Content whose first 1 MiB chunk ends right after head (bz2 compresses the random hex pad little)."""
            size = archive.CHUNK_BYTES - len(head) - len(opening) - len(closing)
            return opening + random.Random(3).randbytes(size).hex()[:size].encode() + closing + head + rest

        line = lines([md_change(mid, when, definition("2026-01-01T00:00:00+01:00", [runner(1)]))])
        cut = line.index(b'"marketTime": "2026-01') + len(b'"marketTime": "2026-01')
        original, chunks = archive._decompressed, []

        def recorded(read):
            for chunk in original(read):
                chunks.append(chunk)
                yield chunk

        with mock.patch.object(archive, "_decompressed", recorded):
            result = self.ingest([(path(mid, when, event="29000043"), bz2.compress(cut_after(line[:cut], line[cut:])))])
        self.assertTrue(chunks[0].endswith(b'"marketTime": "2026-01'), chunks[0][-40:])       # the cut is real
        self.assertEqual((result["status"], result["refused_by_reason"]), ("ingested", {}), result)
        manifest = json.loads((self.root / "manifests" / f"{result['archive_sha256']}.json").read_bytes())
        self.assertEqual(manifest["members"][0]["content_months"], ["2025-12"])
        self.assertEqual(archive.content_months(b'"marketTime":"2026-01', final=False), set())
        ended = iter([bz2.compress(b'{"marketTime":"2026-01')])
        self.assertEqual(archive._decompress_check(lambda n: next(ended, b""), 100, DEFAULT_LIMITS, 1 << 20),
                         (22, {"2026-01"}))
        # A match longer than the carried tail, cut after its month: counted by that literal (sealed) month.
        head = b'{"op":"mcm","pt":1,"mc":[{"id":"1.100000001","x":{"marketTime"' + b" " * 200 + b': "2026-03'
        rest = b'-01T12:00:00.000Z"}}]}\n' + lines(full_market())
        chunks.clear()
        with mock.patch.object(archive, "_decompressed", recorded):
            result = self.ingest([(path("1.100000001"), bz2.compress(cut_after(head, rest))),
                                  (path("1.100000002"), stream(full_market("1.100000002")))], root=self.new_root())
        self.assertTrue(chunks[0].endswith(b' : "2026-03'), chunks[0][-40:])
        self.assertEqual((result["status"], result["members"], result["refused_by_reason"]),
                         ("ingested", 1, {"content_month_sealed_holdout": 1}), result)

    def test_every_cut_of_a_market_time_value_counts_its_utc_month_once(self):
        """N1 at every position: _decompress_check's chunks are cut once anywhere in or around a marketTime value
        whose literal month (2026-01) is not its UTC month (2025-12), or twice anywhere inside it; every cut gives
        2025-12 alone. Real chunks are 1 MiB; content_months only cares where the window ends."""
        line = (b'{"op":"mcm","pt":1,"mc":[{"id":"1.1","marketDefinition":{"marketTime": '
                b'"2026-01-01T00:00:00+01:00","x":1}}]}\n')
        start, stop = line.index(b'"marketTime"'), line.index(b',"x"')

        def months(data, *cuts):
            pieces = [data[a:b] for a, b in zip((0, *cuts), (*cuts, len(data)))]
            with mock.patch.object(archive, "_decompressed", lambda read: iter(pieces)):
                return archive._decompress_check(lambda n: b"", len(data), DEFAULT_LIMITS, 1 << 20)

        for first in range(start - 2, stop + 3):
            self.assertEqual(months(line, first), (len(line), {"2025-12"}), first - start)
            for second in range(first + 1, stop + 1):
                self.assertEqual(months(line, first, second), (len(line), {"2025-12"}), (first - start, second - start))
        # The tail's edge: a match cut after its month that fits the 128-byte tail exactly is carried and read
        # whole; one a byte longer is counted by its literal month. With more whitespace still, a cut before the
        # month is complete finds nothing in either chunk: the documented limit (the index is the authoritative gate).
        for spaces, cut, expected in ((106, b'"2026-01', {"2025-12"}), (107, b'"2026-01', {"2026-01"}),
                                      (140, b'"2026-0', set())):
            with self.subTest(spaces=spaces):
                long = b'{"marketTime"' + b" " * spaces + b': "2026-01-01T00:00:00+01:00"}\n'
                self.assertEqual(months(long, long.index(cut) + len(cut)), (len(long), expected))

    def test_any_refused_content_month_refuses_the_member(self):
        """E5: a 31 Dec race revised after the off to 1 Jan is refused whole (its event file too, with the unaffected
        13:00 race in it): a population exclusion task (c) must report, recorded with the content months."""
        off, late = datetime(2025, 12, 31, 15, tzinfo=timezone.utc), datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
        moved = [md_change("1.600000001", off - timedelta(hours=3), definition(iso(off), [runner(1), runner(2)])),
                 md_change("1.600000001", off + timedelta(minutes=5), definition(iso(late), [runner(1), runner(2)]))]
        other = full_market("1.600000002", datetime(2025, 12, 31, 13, tzinfo=timezone.utc))
        event = sorted(moved + other, key=lambda m: m["pt"])
        result = self.ingest([(path("1.600000001", off, event="33000001"), stream(moved)),
                              (path("33000001", off, event="33000001"), stream(event)),
                              (path("1.600000002", off, event="33000001"), stream(other))])
        self.assertEqual((result["members"], result["refused_by_reason"]), (1, {"content_month_sealed_holdout": 2}))
        manifest = json.loads((self.root / "manifests" / f"{result['archive_sha256']}.json").read_bytes())
        self.assertEqual([item["content_months"] for item in manifest["refused"]], [["2025-12", "2026-01"]] * 2)

    def test_months_are_utc_calendar_months(self):
        """2026-01-01T00:30+01:00 is 2025-12-31 in UTC: kept; 2025-12-31T23:30-01:00 is 2026-01 in UTC: refused."""
        december = datetime(2025, 12, 31, tzinfo=timezone.utc)
        early = md_change("1.100000041", december, definition("2026-01-01T00:30:00.000+01:00", [runner(1)]))
        late = md_change("1.100000042", december, definition("2025-12-31T23:30:00.000-01:00", [runner(1)]))
        members = [(path("1.100000041", december, event="29000041"), stream([early])),
                   (path("1.100000042", december, event="29000042"), stream([late]))]
        result = self.ingest(members)
        self.assertEqual((result["members"], result["refused_by_reason"]), (1, {"content_month_sealed_holdout": 1}))
        self.assertEqual((result["sealed_holdout_members"], result["month_unknown_members"]), (1, 0))
        manifest = json.loads((self.root / "manifests" / f"{result['archive_sha256']}.json").read_bytes())
        self.assertEqual(manifest["members"][0]["content_months"], ["2025-12"])
        self.assertEqual(index_archives(self.root)["status"], "indexed")
        checks = self.records(result["archive_sha256"])["1.100000041"]["checks"]
        self.assertEqual((checks["market_time_month"], checks["month_mismatch"], checks["date_mismatch"]),
                         ("2025-12", False, False))
        self.assertEqual(archive.content_months(b'"marketTime":"2026-03'), {"2026-03"})      # cut: literal month
        self.assertEqual(archive.market_month("2026-02-30T00:00:00Z"), "2026-02")             # unparseable: literal

    def test_gate_function_matches_the_contract_constants(self):
        self.assertIsNone(archive.month_gate("2025-12"))
        self.assertIsNone(archive.month_gate("2015-05"))
        self.assertEqual(archive.month_gate("2026-01"), "month_sealed_holdout")
        self.assertEqual(archive.month_gate("2026-09"), "month_exposed_exploratory_only")
        self.assertEqual(archive.month_gate("2026-10"), "month_not_prospective")
        self.assertEqual(archive.month_gate("2030-02"), "month_prospective_out_of_scope")

    def test_a_changed_gate_is_named_and_never_counted(self):
        july = datetime(2015, 7, 1, 14, tzinfo=timezone.utc)
        members = [(path("1.100000001"), stream(full_market())),
                   (path("1.100000002", july, event="27000002"), stream(full_market("1.100000002", july)))]
        sha, _ = self.build(members)
        unrelated = {**archive.EXPOSED_MONTHS, "2014-01": {}}
        with mock.patch.object(archive, "EXPOSED_MONTHS", unrelated):
            report = audit(self.root)
            self.assertEqual(report["status"], "audited", report)
            self.assertEqual(report["archives"][0]["month_gate_current"], False)
            self.assertEqual(index_archives(self.root)["archives"][0]["state"], "unchanged")
        exposed = {**archive.EXPOSED_MONTHS, "2015-06": {}}
        with mock.patch.object(archive, "EXPOSED_MONTHS", exposed):
            self.assert_refused(self.ingest(members, name="build0.tar"), f"manifest:{sha}:gate_changed")
            self.assertEqual(index_archives(self.root)["problems"], [f"index:{sha}:gate_changed"])
            self.assertEqual(audit(self.root)["problems"], [f"index:{sha}:stale_gate"])
        self.assertEqual(audit(self.root)["archives"][0]["month_gate_current"], True)


class RealContainerShapeTests(Workspace):
    def test_libarchive_pax_container_as_downloaded_is_accepted(self):
        """The real 2026-10-14 BASIC download (portal TAR, built by libarchive): PAX format with one pax extended
        header per member holding only atime, ctime, mtime and LIBARCHIVE.creationtime (about 150 bytes), no
        global header, regular files only, the dated BASIC/<yyyy>/<Mon>/<d>/<eventId>/<marketId>.bz2 layout, and
        exactly two zero blocks after the last member (not padded to a 10240-byte record). Content is synthetic."""
        stamps = {"atime": "1557303870.6597580", "ctime": "1557303870.6957880", "mtime": "1557303870.6957880",
                  "LIBARCHIVE.creationtime": "1557303870.6957880"}
        members = [(path("1.100000001"), stream(full_market())),
                   (path("1.100000002"), stream(full_market("1.100000002")))]
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as tar:
            for name, data in members:
                info = tarfile.TarInfo(name)
                info.size = len(data)
                info.pax_headers = dict(stamps)
                tar.addfile(info, io.BytesIO(data))
        raw = buffer.getvalue()
        with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
            last = tar.getmembers()[-1]
        end = last.offset_data + -(-last.size // tarfile.BLOCKSIZE) * tarfile.BLOCKSIZE
        target = self.tmp / "real-shape.tar"
        target.write_bytes(raw[:end] + b"\0" * (2 * tarfile.BLOCKSIZE))
        with tarfile.open(target, "r|") as tar:                       # the shape is what the download showed
            shape = [(member.type, sorted(member.pax_headers)) for member in tar]
            self.assertEqual(tar.pax_headers, {})
        self.assertEqual(shape, [(tarfile.REGTYPE, sorted(stamps))] * 2)
        self.assertEqual(target.stat().st_size % tarfile.RECORDSIZE != 0, True)
        result = ingest_archive(target, self.root, DEFAULT_LIMITS)
        self.assertEqual((result["status"], result["members"], result["written"]), ("ingested", 2, 2), result)


class IdempotencyTests(Workspace):
    def members(self):
        return [(path("1.100000001"), stream(full_market())),
                (path(EVENT), stream(full_market()))]

    def test_reingest_is_idempotent_and_changes_are_refused(self):
        first = self.ingest(self.members())
        self.assertEqual((first["status"], first["written"], first["members"]), ("ingested", 2, 2))
        sha = first["archive_sha256"]
        manifest_file = self.root / "manifests" / f"{sha}.json"
        before = manifest_file.read_bytes()
        raw = self.root / "raw" / sha / path("1.100000001")
        raw_bytes = raw.read_bytes()
        self.assertEqual(raw_bytes, self.members()[0][1])               # stored bz2-compressed, as in the TAR
        again = self.ingest(self.members())
        self.assertEqual((again["status"], again["written"], again["already_present"]), ("already_ingested", 0, 2))
        self.assertEqual(again["manifest_sha256"], first["manifest_sha256"])
        self.assertEqual(manifest_file.read_bytes(), before)
        self.assertEqual(hashlib.sha256(before).hexdigest(), first["manifest_sha256"])
        # The same bytes under another file name, or other limits, would change the manifest.
        self.assert_refused(self.ingest(self.members(), name="renamed.tar"), f"manifest:{sha}:would_change")
        self.assert_refused(self.ingest(self.members(), max_ratio=150), f"manifest:{sha}:would_change")
        manifest_file.write_bytes(b"{}")                                # no month gate in it: not a gate change
        self.assert_refused(self.ingest(self.members()), f"manifest:{sha}:would_change")
        manifest_file.write_bytes(before)
        # A deleted raw file is restored; a changed one is never overwritten, even at the same size.
        raw.unlink()
        restored = self.ingest(self.members())
        self.assertEqual((restored["status"], restored["written"]), ("ingested", 1))
        self.assertEqual(raw.read_bytes(), raw_bytes)
        for changed in (raw_bytes + b"x", raw_bytes[:-1] + bytes([raw_bytes[-1] ^ 1])):
            with self.subTest(same_size=len(changed) == len(raw_bytes)):
                raw.write_bytes(changed)
                self.assert_refused(self.ingest(self.members()),
                                    f"raw:{path('1.100000001')}:existing_hash_mismatch")
                self.assertEqual(raw.read_bytes(), changed)
        raw.write_bytes(raw_bytes)
        # A leftover partial file blocks the ingest until a person removes it, before anything is written.
        event_raw = self.root / "raw" / sha / path(EVENT)
        raw.unlink()
        event_raw.unlink()
        partial = event_raw.with_name(event_raw.name + ".partial")
        partial.write_bytes(b"half")
        self.assert_refused(self.ingest(self.members()), f"raw:{path(EVENT)}:partial_file_present")
        self.assertFalse(raw.exists())
        partial.unlink()
        self.assertEqual(self.ingest(self.members())["written"], 2)

    def test_a_leftover_manifest_partial_refuses_before_any_write(self):
        """N2: a manifests/<sha>.json.partial left by an interrupted run is found with the raw partials, before any
        raw member is written (it used to surface only at the manifest write, after the raw copy), and both kinds
        are listed together. A dangling symlink left as a partial counts too (it used to pass the check and fail
        only at the write, after earlier raw members were copied). A manifest partial refuses even beside the
        identical manifest of a complete ingest, and beside a manifest that would change it is listed with that."""
        tar = self.tar(self.members())
        sha = hashlib.sha256(tar.read_bytes()).hexdigest()
        manifest = self.root / "manifests" / f"{sha}.json"
        partial = manifest.with_name(manifest.name + ".partial")
        partial.parent.mkdir()
        refused = {"status": "refused", "problems": [f"manifest:{sha}:partial_file_present"]}
        partial.symlink_to(self.tmp / "nowhere")
        self.assertEqual(ingest_archive(tar, self.root), refused)
        self.assertFalse((self.root / "raw").exists())
        partial.unlink()
        partial.write_bytes(b"half")
        self.assertEqual(ingest_archive(tar, self.root), refused)
        self.assertFalse((self.root / "raw").exists())
        self.assertEqual(partial.read_bytes(), b"half")
        partial.unlink()
        self.assertEqual(ingest_archive(tar, self.root)["status"], "ingested")
        partial.write_bytes(b"half")
        self.assertEqual(ingest_archive(tar, self.root), refused)
        complete = manifest.read_bytes()
        manifest.write_bytes(b"{}")
        self.assertEqual(ingest_archive(tar, self.root)["problems"],
                         [f"manifest:{sha}:partial_file_present", f"manifest:{sha}:would_change"])
        manifest.write_bytes(complete)
        partial.unlink()
        self.assertEqual(ingest_archive(tar, self.root)["status"], "already_ingested")
        raw = self.root / "raw" / sha / path("1.100000001")
        raw.rename(raw.with_name(raw.name + ".partial"))
        manifest.rename(partial)
        self.assertEqual(ingest_archive(tar, self.root)["problems"],
                         [f"manifest:{sha}:partial_file_present", f"raw:{path('1.100000001')}:partial_file_present"])
        self.assertFalse(raw.exists())
        # A dangling raw partial of the second member: the first, missing, is not written either.
        partial.unlink()
        raw.with_name(raw.name + ".partial").unlink()
        event_raw = self.root / "raw" / sha / path(EVENT)
        event_raw.unlink()
        event_raw.with_name(event_raw.name + ".partial").symlink_to(self.tmp / "nowhere")
        self.assertEqual(ingest_archive(tar, self.root),
                         {"status": "refused", "problems": [f"raw:{path(EVENT)}:partial_file_present"]})
        self.assertFalse(raw.exists() or event_raw.exists())

    def test_a_tar_that_changes_between_the_passes_is_refused(self):
        kept, refused = path("1.100000001"), path("1.100000002", datetime(2026, 3, 1, tzinfo=timezone.utc))
        members = [(kept, stream(full_market())), (refused, b"sealed month bytes")]
        original = archive._scan

        def flip(name):
            def change(tar_path):
                header, data, size = member_offsets(tar_path)[name]
                blob = bytearray(tar_path.read_bytes())
                blob[data + size - 1] ^= 1                    # same size, one bit
                tar_path.write_bytes(bytes(blob))
            return change

        swaps = {"a refused member changed": (flip(refused), "archive:changed_during_ingest"),
                 "a kept member changed": (flip(kept), f"raw:{kept}:changed_during_ingest"),
                 "a kept member removed": (lambda tar_path: make_tar(tar_path, [members[1]]),
                                           "archive:changed_during_ingest"),
                 "the TAR deleted": (lambda tar_path: tar_path.unlink(), "archive:changed_during_ingest")}
        for label, (swap, problem) in swaps.items():
            with self.subTest(swap=label):
                def scan_then_swap(tar_path, limits):
                    result = original(tar_path, limits)
                    swap(tar_path)
                    return result

                root = self.new_root()
                with mock.patch.object(archive, "_scan", scan_then_swap):
                    result = self.ingest(members, root=root)
                self.assertEqual(result, {"status": "refused", "problems": [problem]})
                self.assertFalse((root / "manifests").exists())

    def test_atomic_write_and_safe_paths(self):
        target = self.root / "file.bin"
        with self.assertRaises(archive.ArchiveProblem) as raised:
            archive.write_new(target, [b"abc"], "0" * 64, 3)
        self.assertEqual(str(raised.exception), "changed_during_ingest")
        self.assertEqual(list(self.root.iterdir()), [])
        archive.write_new(target, [b"abc"], hashlib.sha256(b"abc").hexdigest(), 3)
        self.assertEqual(target.read_bytes(), b"abc")
        with self.assertRaises(archive.ArchiveProblem) as raised:
            archive.write_new(target, [b"other"])
        self.assertEqual((str(raised.exception), target.read_bytes()), ("target_exists", b"abc"))
        other = self.root / "other.bin"
        other.with_name("other.bin.partial").write_bytes(b"someone else's")
        with self.assertRaises(archive.ArchiveProblem) as raised:
            archive.write_new(other, [b"abc"])
        self.assertEqual((str(raised.exception), other.with_name("other.bin.partial").read_bytes()),
                         ("partial_file_present", b"someone else's"))
        for parts in (("..", "x"), ("a/b",), ("",), (".",), ("a\\b",)):
            with self.subTest(parts=parts), self.assertRaises(archive.ArchiveProblem):
                archive.safe_path(self.root, *parts)
        with self.assertRaises(archive.ArchiveProblem):
            archive.safe_path(self.root, "file.bin", "below")          # a file where a directory would go
        (self.root / "repo" / ".git").mkdir(parents=True)
        with self.assertRaises(archive.ArchiveProblem):
            archive.safe_path(self.root, "repo", "x")                  # a Git worktree below the root
        with self.assertRaises(archive.ArchiveProblem):
            archive.safe_path(self.root / "repo", "x")                 # or the root itself, checked again

    def test_symlinked_layout_is_refused(self):
        first = self.ingest(self.members())
        sha = first["archive_sha256"]
        outside = self.tmp / "outside"
        outside.mkdir()
        target_dir = self.root / "raw" / sha / "BASIC"
        for item in sorted(target_dir.rglob("*"), reverse=True):
            item.unlink() if item.is_file() else item.rmdir()
        target_dir.rmdir()
        target_dir.symlink_to(outside, target_is_directory=True)
        self.assert_refused(self.ingest(self.members()), f"raw:{path('1.100000001')}:unsafe_target")
        self.assertEqual(list(outside.iterdir()), [])


@unittest.skipIf(IS_ROOT, "root ignores file modes")
class FileSystemFailureTests(Workspace):
    """Permission and file-type failures are refusals with a place, never tracebacks."""

    def setUp(self):
        super().setUp()
        self.sha, _ = self.build([(path("1.100000001"), stream(full_market()))])
        self.raw = self.root / "raw" / self.sha / path("1.100000001")
        self.manifest = self.root / "manifests" / f"{self.sha}.json"

    def reingest(self):
        return ingest_archive(self.tmp / "build0.tar", self.root)

    def test_unreadable_raw_member(self):
        self.chmod(self.raw, 0)
        self.assertEqual(self.reingest()["problems"], [f"raw:{path('1.100000001')}:unreadable"])
        (self.root / "index").rename(self.tmp / "old-index")
        self.assertEqual(index_archives(self.root)["problems"], [f"raw:{path('1.100000001')}:unreadable"])

    def test_unreadable_or_misplaced_manifest(self):
        self.manifest.chmod(0)
        try:
            self.assertEqual(self.reingest()["problems"], [f"manifest:{self.sha}:unreadable"])
            self.assertEqual(index_archives(self.root)["problems"], [f"manifest:{self.sha}:unreadable"])
            self.assertEqual(audit(self.root)["problems"], [f"manifest:{self.sha}:unreadable"])
        finally:
            self.manifest.chmod(0o644)
        self.manifest.rename(self.tmp / "manifest.json")
        self.manifest.mkdir()
        self.assertEqual(self.reingest()["problems"], [f"manifest:{self.sha}:unreadable"])
        self.assertEqual(index_archives(self.root)["problems"], [f"manifest:{self.sha}:unreadable"])

    def test_unreadable_manifests_folder_and_data_root(self):
        self.chmod(self.root / "manifests", 0)
        self.assertEqual(index_archives(self.root), {"status": "refused", "problems": ["manifests:unreadable"]})
        self.assertEqual(audit(self.root), {"status": "refused", "problems": ["manifests:unreadable"]})
        (self.root / "manifests").chmod(0o755)
        self.chmod(self.root, 0)
        for result in (index_archives(self.root), audit(self.root), self.reingest()):
            self.assertEqual(result, {"status": "refused", "problems": ["data_root:unreadable"]})

    def test_an_index_path_that_is_a_directory(self):
        head = self.root / "index" / self.sha / market_index.HEAD_FILE
        head.unlink()
        head.mkdir()
        self.assertEqual(index_archives(self.root)["problems"], [f"index:{self.sha}:write_failed"])

    def test_read_only_data_root(self):
        (self.root / "index").rename(self.tmp / "old-index")
        self.chmod(self.root, 0o555)
        self.assertEqual(index_archives(self.root)["problems"], [f"index:{self.sha}:write_failed"])

    def test_the_cli_still_prints_one_json_line(self):
        self.chmod(self.root / "manifests", 0)
        for command in ("archive-index", "archive-audit"):
            with self.subTest(command=command):
                out = io.StringIO()
                with redirect_stdout(out):
                    code = main([command, "--data-root", str(self.root)])
                self.assertEqual(code, 2)
                self.assertEqual(out.getvalue().count("\n"), 1)
                self.assertEqual(json.loads(out.getvalue())["problems"], ["manifests:unreadable"])


class IndexTests(Workspace):
    def test_decision_time_holds_no_outcome_and_keeps_pre_off_facts_with_pt(self):
        sha, _ = self.build([(path("1.100000001"), stream(full_market()))])
        record = self.records(sha)["1.100000001"]
        self.assertEqual(sorted(record), ["checks", "coverage", "decision_time", "evaluator_only_outcome", "market_id",
                                          "provenance", "schema_version"])
        decision, outcome = record["decision_time"], record["evaluator_only_outcome"]
        self.assertEqual(sorted(decision), ["definition", "first_published_market_time", "removals", "runners",
                                            "view_ends"])
        found = set(walk(decision))
        self.assertEqual(found & set(OUTCOME_TEXT + OUTCOME_KEYS), set())
        cutoff = ms(OFF)
        self.assertEqual(outcome["decision_cutoff"], {"pt": cutoff, "reason": "first_published_scheduled_off"})
        self.assertEqual(decision["view_ends"], {"pt": cutoff, "reason": "first_published_scheduled_off"})
        pts = all_pts(decision)
        self.assertTrue(pts and max(pts) < cutoff)
        # First-published marketTime and its pre-off revision, each with its pt.
        self.assertEqual(decision["first_published_market_time"],
                         {"pt": ms(OFF - timedelta(hours=2)), "value": iso(OFF)})
        self.assertEqual(decision["definition"]["marketTime"],
                         [[ms(OFF - timedelta(hours=2)), iso(OFF)],
                          [ms(OFF - timedelta(minutes=60)), iso(OFF + timedelta(minutes=10))]])
        self.assertEqual(decision["definition"]["numberOfWinners"], [[ms(OFF - timedelta(hours=2)), 1]])
        self.assertEqual(decision["definition"]["inPlay"], [[ms(OFF - timedelta(hours=2)), False]])
        self.assertEqual(decision["definition"]["version"], [[ms(OFF - timedelta(hours=2)), 1]])
        # The pre-off removal is a decision-time fact; the one after the first-published off is evaluator-only.
        self.assertEqual(decision["removals"], [{"id": 3, "pt": ms(OFF - timedelta(minutes=90)), "status": "REMOVED",
                                                 "removal_date": iso(OFF - timedelta(minutes=90)),
                                                 "adjustment_factor": 10.0}])
        self.assertEqual(outcome["removals_after_cutoff"], [{"id": 4, "pt": ms(OFF + timedelta(minutes=1)),
                                                             "status": "REMOVED",
                                                             "removal_date": iso(OFF + timedelta(minutes=1)),
                                                             "adjustment_factor": 10.0}])
        runners = {r["id"]: r for r in decision["runners"]}
        self.assertEqual(runners[3]["status"], [[ms(OFF - timedelta(hours=2)), "ACTIVE"],
                                                [ms(OFF - timedelta(minutes=90)), "REMOVED"]])
        self.assertEqual(runners[4]["status"], [[ms(OFF - timedelta(hours=2)), "ACTIVE"]])
        self.assertEqual(runners[1]["name"], [[ms(OFF - timedelta(hours=2)), "Horse 1"]])
        # Outcomes only on the evaluator side.
        final = {r["id"]: (r["status"], r["bsp"]) for r in outcome["final_runners"]}
        self.assertEqual(final, {1: ("WINNER", 2.6), 2: ("LOSER", 3.1), 3: ("REMOVED", None), 4: ("REMOVED", None)})
        self.assertEqual((outcome["closed_reached"], outcome["final_status"], outcome["winners_observed"],
                          outcome["dead_heat"], outcome["missing_final_bsp_runner_ids"],
                          outcome["unusable_final_bsp_runner_ids"], outcome["results_revised_after_close"]),
                         (True, "CLOSED", 1, False, [], [], False))
        self.assertEqual((outcome["read_complete"], outcome["unreadable_reason"], outcome["complete_before_pt"]),
                         (True, None, None))
        self.assertEqual(outcome["settled_time"], iso(OFF + timedelta(minutes=20)))
        self.assertEqual(outcome["definition_after_cutoff"]["inPlay"], [[ms(OFF + timedelta(minutes=10)), True]])
        self.assertEqual(outcome["outcome_definition"]["bspReconciled"],
                         [[ms(OFF - timedelta(hours=2)), False], [ms(OFF + timedelta(minutes=10)), True]])
        self.assertEqual(record["checks"], {"path_month": "2015-06", "market_time_month": "2015-06",
                                            "month_mismatch": False, "date_mismatch": False,
                                            "event_id_mismatch": False, "first_pt": ms(OFF - timedelta(hours=2)),
                                            "market_file_holds_other_market": False,
                                            "untracked_definition_fields": [], "unrecognised_runner_statuses": []})
        coverage = record["coverage"]
        self.assertTrue({"rc.ltp", "md.runners.bsp", "md.settledTime", "mc.marketDefinition", "mc.rc"}
                        <= set(coverage["whole_file"]))
        self.assertNotIn("mc.img", coverage["whole_file"])
        # Before the cutoff: the LTP and the definition keys, never the BSP or the settled time.
        self.assertIn("rc.ltp", coverage["before_cutoff"])
        self.assertFalse({"md.runners.bsp", "md.settledTime"} & set(coverage["before_cutoff"]))
        self.assertEqual((coverage["valued_before_cutoff"], coverage["valued_whole_file"]),
                         (["rc.ltp"], ["md.runners.bsp", "rc.ltp"]))

    def test_each_outcome_marker_alone_moves_the_cutoff(self):
        """Each marker, published alone 20 minutes before the off, ends decision_time there; the view is the same
        as the opening definition's whatever the marker was."""
        t, mark = iso(OFF), OFF - timedelta(minutes=20)
        markers = {
            "1.100000011": definition(t, [runner(1), runner(2)], status="CLOSED"),        # a void: no runner result
            "1.100000012": definition(t, [runner(1), runner(2)], settledTime=iso(mark)),
            "1.100000013": definition(t, [runner(1), runner(2)], bspReconciled=True),
            "1.100000014": definition(t, [runner(1, bsp=2.0), runner(2)]),
            "1.100000015": definition(t, [runner(1, "PLACED"), runner(2)]),
            "1.100000016": definition(t, [runner(1, "LOSER"), runner(2)]),
            "1.100000017": definition(t, [runner(1, "WINNER"), runner(2)]),
        }
        members = [(path(mid), stream([md_change(mid, OFF - timedelta(hours=2), definition(t, [runner(1), runner(2)])),
                                       md_change(mid, mark, value)]))
                   for mid, value in markers.items()]
        sha, _ = self.build(members)
        records = self.records(sha)
        views = set()
        for mid in markers:
            with self.subTest(market=mid):
                record = records[mid]
                expected = {"pt": ms(mark), "reason": "first_outcome_bearing_definition"}
                self.assertEqual(record["evaluator_only_outcome"]["decision_cutoff"], expected)
                self.assertEqual(record["decision_time"]["view_ends"], expected)
                self.assertTrue(max(all_pts(record["decision_time"])) < ms(mark))
                views.add(json.dumps(record["decision_time"], sort_keys=True))
        self.assertEqual(len(views), 1)
        # Winners are counted only when the final state holds a result (CLOSED, or a listed runner's result status).
        self.assertEqual({mid: records[mid]["evaluator_only_outcome"]["winners_observed"] for mid in markers},
                         {"1.100000011": 0, "1.100000012": None, "1.100000013": None, "1.100000014": None,
                          "1.100000015": 0, "1.100000016": 0, "1.100000017": 1})

    def test_a_market_closed_before_the_decision_time_says_where_its_view_ends(self):
        """E6: suspended 30 minutes before the off, then CLOSED with every runner REMOVED 20 minutes before it."""
        t = iso(OFF)
        messages = [md_change("1.700000001", OFF - timedelta(hours=2), definition(t, [runner(1), runner(2)])),
                    md_change("1.700000001", OFF - timedelta(minutes=30),
                              definition(t, [runner(1), runner(2)], status="SUSPENDED")),
                    md_change("1.700000001", OFF - timedelta(minutes=20),
                              definition(t, [runner(1, "REMOVED"), runner(2, "REMOVED")], status="CLOSED"))]
        sha, _ = self.build([(path("1.700000001"), stream(messages))])
        record = self.records(sha)["1.700000001"]
        decision, outcome = record["decision_time"], record["evaluator_only_outcome"]
        self.assertEqual(decision["view_ends"], {"pt": ms(OFF - timedelta(minutes=20)),
                                                 "reason": "first_outcome_bearing_definition"})
        self.assertEqual(decision["definition"]["status"], [[ms(OFF - timedelta(hours=2)), "OPEN"],
                                                            [ms(OFF - timedelta(minutes=30)), "SUSPENDED"]])
        # Removals published in the definition that sets the cutoff are on the evaluator side.
        self.assertEqual(decision["removals"], [])
        self.assertEqual([item["id"] for item in outcome["removals_after_cutoff"]], [1, 2])
        self.assertEqual((outcome["winners_observed"], outcome["dead_heat"]), (0, False))

    def test_removals_keep_their_values_as_of_the_removal_and_late_runners_stay_out(self):
        t = iso(OFF)
        first_date, later_date = iso(OFF - timedelta(minutes=90)), iso(OFF + timedelta(minutes=5))
        removed = runner(3, "REMOVED", af=10.0, removal=first_date)
        revised = runner(3, "REMOVED", af=12.5, removal=later_date)
        messages = [md_change("1.100000001", OFF - timedelta(hours=2),
                              definition(t, [runner(1), runner(2), runner(3, af=10.0)])),
                    md_change("1.100000001", OFF - timedelta(minutes=90),
                              definition(t, [runner(1), runner(2), removed])),
                    md_change("1.100000001", OFF + timedelta(minutes=5),
                              definition(t, [runner(1), runner(2), revised, runner(5)], in_play=True))]
        sha, _ = self.build([(path("1.100000001"), stream(messages))])
        record = self.records(sha)["1.100000001"]
        self.assertEqual(record["decision_time"]["removals"],
                         [{"id": 3, "pt": ms(OFF - timedelta(minutes=90)), "status": "REMOVED",
                           "removal_date": first_date, "adjustment_factor": 10.0}])
        self.assertEqual([r["id"] for r in record["decision_time"]["runners"]], [1, 2, 3])
        self.assertIn(5, [r["id"] for r in record["evaluator_only_outcome"]["final_runners"]])

    def test_a_market_first_published_after_its_off_has_no_decision_view(self):
        """The explicit empty view, whose end has no pt (the off it would name was published after it); the evaluator
        still sees where this source's view would have ended, and every timeline."""
        late = [md_change("1.100000001", OFF + timedelta(minutes=1), definition(iso(OFF), [runner(1), runner(2)]))]
        sha, _ = self.build([(path("1.100000001"), stream(late))])
        record = self.records(sha)["1.100000001"]
        self.assertEqual(record["decision_time"], NO_VIEW)
        outcome = record["evaluator_only_outcome"]
        self.assertEqual(outcome["decision_cutoff"], {"pt": ms(OFF), "reason": "first_published_scheduled_off"})
        self.assertEqual(outcome["definition_after_cutoff"]["marketTime"], [[ms(OFF + timedelta(minutes=1)), iso(OFF)]])
        self.assertEqual(record["coverage"]["before_cutoff"], [])

    def test_early_outcome_cuts_decision_time_before_the_scheduled_off(self):
        t = iso(OFF)
        early = OFF - timedelta(minutes=5)
        messages = [md_change("1.100000001", OFF - timedelta(hours=1), definition(t, [runner(1), runner(2)])),
                    md_change("1.100000001", early,
                              definition(t, [runner(1, "WINNER"), runner(2, "LOSER")], in_play=True)),
                    md_change("1.100000001", OFF - timedelta(minutes=2),
                              definition(t, [runner(1, "WINNER"), runner(2, "LOSER")], status="CLOSED"))]
        sha, _ = self.build([(path("1.100000001"), stream(messages))])
        record = self.records(sha)["1.100000001"]
        self.assertEqual(record["evaluator_only_outcome"]["decision_cutoff"],
                         {"pt": ms(early), "reason": "first_outcome_bearing_definition"})
        self.assertEqual(record["decision_time"]["view_ends"]["pt"], ms(early))
        self.assertEqual(set(walk(record["decision_time"])) & set(OUTCOME_TEXT + OUTCOME_KEYS), set())
        self.assertEqual(record["decision_time"]["definition"]["inPlay"], [[ms(OFF - timedelta(hours=1)), False]])
        # A missing BSP on a closed market is listed for every runner not removed.
        self.assertEqual(record["evaluator_only_outcome"]["missing_final_bsp_runner_ids"], [1, 2])

    def test_dead_heat_missing_bsp_never_closed_and_month_mismatch(self):
        july = datetime(2015, 7, 1, 0, 30, tzinfo=timezone.utc)
        never = [md_change("1.100000004", OFF - timedelta(hours=1), definition(iso(OFF), [runner(1), runner(2)])),
                 md_change("1.100000004", OFF + timedelta(minutes=1),
                           definition(iso(OFF), [runner(1), runner(2)], in_play=True))]
        members = [
            (path("1.100000002"), stream(closed_market("1.100000002", ["WINNER", "WINNER", "LOSER"], [2.0, 3.0, 4.0]))),
            (path("1.100000003"), stream(closed_market("1.100000003", ["WINNER", "LOSER"], [None, 3.0]))),
            (path("1.100000004"), stream(never)),
            (path("1.100000005", datetime(2015, 6, 30, tzinfo=timezone.utc), event="27000005"),
             stream(closed_market("1.100000005", ["WINNER", "LOSER"], [2.0, 3.0], off=july))),
        ]
        sha, _ = self.build(members)
        records = self.records(sha)
        outcome = {mid: r["evaluator_only_outcome"] for mid, r in records.items()}
        self.assertEqual((outcome["1.100000002"]["dead_heat"], outcome["1.100000002"]["winners_observed"]), (True, 2))
        self.assertEqual(outcome["1.100000003"]["missing_final_bsp_runner_ids"], [1])
        self.assertEqual((outcome["1.100000004"]["closed_reached"], outcome["1.100000004"]["dead_heat"],
                          outcome["1.100000004"]["winners_observed"],
                          outcome["1.100000004"]["results_revised_after_close"]), (False, None, None, None))
        self.assertEqual(outcome["1.100000004"]["missing_final_bsp_runner_ids"], [1, 2])
        checks = records["1.100000005"]["checks"]
        self.assertEqual((checks["month_mismatch"], checks["market_time_month"], checks["date_mismatch"],
                          checks["event_id_mismatch"]), (True, "2015-07", True, True))
        for record in records.values():
            self.assertEqual(set(walk(record["decision_time"])) & set(OUTCOME_TEXT + OUTCOME_KEYS), set())

    def test_dead_heat_rules_by_market_type(self):
        def market(mid, statuses, **extra):
            return (path(mid), stream(closed_market(mid, statuses, [2.0] * len(statuses), **extra)))

        each_way = {"marketType": "EACH_WAY", "numberOfWinners": 3}
        cases = {
            "1.100000021": (["WINNER", "WINNER", "PLACED", "LOSER"], each_way, True),           # dead heat for the win
            "1.100000022": (["WINNER", "PLACED", "PLACED", "PLACED", "LOSER"], each_way, True),  # for third
            "1.100000023": (["WINNER", "PLACED", "PLACED", "LOSER"], each_way, False),
            "1.100000024": (["WINNER", "WINNER", "WINNER", "WINNER", "LOSER"], {"marketType": "PLACE",
                                                                                "numberOfWinners": 3}, True),
            "1.100000025": (["WINNER", "WINNER", "LOSER"], {"marketType": "OTHER_PLACE", "numberOfWinners": 2}, False),
            "1.100000026": (["WINNER", "LOSER"], {"numberOfWinners": 0}, None),     # variable number of winners
            "1.100000027": (["WINNER", "LOSER"], {"marketType": "ASIAN_HANDICAP"}, None),
        }
        sha, _ = self.build([market(mid, statuses, **extra) for mid, (statuses, extra, _) in cases.items()])
        records = self.records(sha)
        self.assertEqual({mid: records[mid]["evaluator_only_outcome"]["dead_heat"] for mid in cases},
                         {mid: expected for mid, (_, _, expected) in cases.items()})

    def test_final_bsp_and_runner_status_classes(self):
        statuses = ["WINNER", "LOSER", "REMOVED_VACANT", "HIDDEN", "MYSTERY", "LOSER"]
        bsps = ["NaN", "Infinity", None, None, 3.0, 1.0]
        sha, _ = self.build([(path("1.100000001"), stream(closed_market("1.100000001", statuses, bsps)))])
        record = self.records(sha)["1.100000001"]
        outcome = record["evaluator_only_outcome"]
        self.assertEqual({r["id"]: r["bsp"] for r in outcome["final_runners"]},
                         {1: None, 2: None, 3: None, 4: None, 5: 3.0, 6: None})
        self.assertEqual((outcome["unusable_final_bsp_runner_ids"], outcome["missing_final_bsp_runner_ids"]),
                         ([1, 2, 6], [1, 2, 6]))
        self.assertEqual([(item["id"], item["status"]) for item in outcome["removals_after_cutoff"]],
                         [(3, "REMOVED_VACANT")])
        self.assertEqual(record["checks"]["unrecognised_runner_statuses"], ["MYSTERY"])
        # The raw values stay in the evaluator's bsp timelines.
        bsp_timelines = {r["id"]: r.get("bsp") for r in outcome["runners_after_cutoff"]}
        self.assertEqual(bsp_timelines[1], [[ms(OFF + timedelta(minutes=5)), "NaN"]])

    def test_results_revised_after_close(self):
        revised = closed_market("1.100000001", ["WINNER", "LOSER"], [2.0, 3.0])
        revised.append(md_change("1.100000001", OFF + timedelta(minutes=30),
                                 definition(iso(OFF), [runner(1, "WINNER", bsp=2.1), runner(2, "LOSER", bsp=3.0)],
                                            status="CLOSED", in_play=True, bspReconciled=True)))
        sha, _ = self.build([(path("1.100000001"), stream(revised)),
                             (path("1.100000002"), stream(closed_market("1.100000002", ["WINNER", "LOSER"],
                                                                        [2.0, 3.0])))])
        records = self.records(sha)
        self.assertEqual([records[m]["evaluator_only_outcome"]["results_revised_after_close"]
                          for m in ("1.100000001", "1.100000002")], [True, False])

    def test_definition_fields_and_identity_checks(self):
        t = iso(OFF)
        messages = [md_change("1.100000001", OFF - timedelta(hours=1),
                              definition(t, [runner(1)], raceType="Flat", version=7, marketBaseRate=5)),
                    md_change("1.100000001", OFF - timedelta(minutes=30), definition(t, [runner(1)], version=8))]
        sha, _ = self.build([(path("1.100000001", event="27000009"), stream(messages))])
        record = self.records(sha)["1.100000001"]
        self.assertEqual(record["decision_time"]["definition"]["version"],
                         [[ms(OFF - timedelta(hours=1)), 7], [ms(OFF - timedelta(minutes=30)), 8]])
        self.assertEqual(record["checks"]["untracked_definition_fields"], ["raceType"])
        # A timeline records a change of type even when the values compare equal (5 then 5.0).
        self.assertEqual(record["decision_time"]["definition"]["marketBaseRate"],
                         [[ms(OFF - timedelta(hours=1)), 5], [ms(OFF - timedelta(minutes=30)), 5.0]])
        self.assertEqual((record["checks"]["event_id_mismatch"], record["checks"]["date_mismatch"]), (True, False))
        self.assertIn("md.raceType", record["coverage"]["before_cutoff"])

    def test_coverage_before_the_cutoff_and_with_usable_values(self):
        """An LTP of 0 or an SP projection of "NaN" before the off is presence, not a usable value; an LTP traded
        only in play is not a decision-time capability at all."""
        t = iso(OFF)
        messages = [md_change("1.100000001", OFF - timedelta(hours=1), definition(t, [runner(1), runner(2)])),
                    mcm(OFF - timedelta(minutes=30), [{"id": "1.100000001",
                                                        "rc": [{"id": 1, "ltp": 0, "spn": "NaN", "atb": [[2.0, 5]]}]}]),
                    mcm(OFF + timedelta(minutes=1), [{"id": "1.100000001", "rc": [{"id": 1, "ltp": 2.5, "spf": 2.4}]}])]
        sha, _ = self.build([(path("1.100000001"), stream(messages))])
        coverage = self.records(sha)["1.100000001"]["coverage"]
        self.assertTrue({"rc.ltp", "rc.spn", "rc.atb"} <= set(coverage["before_cutoff"]))
        self.assertNotIn("rc.spf", coverage["before_cutoff"])
        self.assertEqual((coverage["valued_before_cutoff"], coverage["valued_whole_file"]), ([], ["rc.ltp", "rc.spf"]))

    def test_event_and_market_files_merge_into_one_record(self):
        other = full_market("1.100000002")
        event = sorted(full_market() + other, key=lambda m: m["pt"])
        sha, indexed = self.build([(path("1.100000001"), stream(full_market())), (path(EVENT), stream(event))])
        records = self.records(sha)
        self.assertEqual(sorted(records), ["1.100000001", "1.100000002"])
        primary = records["1.100000001"]["provenance"]
        self.assertEqual((primary["file_type"], primary["member_path"]), ("market", path("1.100000001")))
        self.assertEqual([(s["member_path"], s["agrees"], s["agrees_decision_time"], s["read_complete"])
                          for s in primary["other_sources"]], [(path(EVENT), True, True, True)])
        self.assertEqual(records["1.100000002"]["provenance"]["file_type"], "event")
        self.assertEqual(indexed["archives"][0]["markets"], 2)

    def test_disagreeing_sources_and_a_market_file_holding_another_market(self):
        """Sources that disagree are listed, never consulted. With no market file named after a market, the event
        file of a directory where it was seen is primary when it exists, holding the market or not (by path only);
        a market held only by a file named after another market, in a directory with no event file, has that file
        as primary and says so in checks."""
        venue = full_market()
        venue[0]["mc"][0]["marketDefinition"]["venue"] = "Other Park"          # differs before the cutoff
        late = full_market()[:-1]                                            # differs only after it
        held = full_market("1.100000003")                                    # named after 1.100000002
        event = sorted(venue + full_market("1.100000003"), key=lambda m: m["pt"])
        sha, _ = self.build([(path("1.100000001"), stream(full_market())), (path(EVENT), stream(event)),
                             (path("1.100000002"), stream(held)),
                             (path("1.100000005"), stream(full_market("1.100000006"))),
                             (path("1.100000007", event="27000009"), stream(full_market("1.100000008"))),
                             (path("1.100000001", plan="PRO"), stream(full_market())),
                             (path(EVENT, plan="PRO"), stream(late))])
        records = {(r["provenance"]["plan"], r["market_id"]): r
                   for r in map(json.loads, (self.root / "index" / sha / market_index.MARKETS_FILE)
                                .read_bytes().splitlines())}
        basic = records[("BASIC", "1.100000001")]["provenance"]["other_sources"]
        self.assertEqual([(s["agrees"], s["agrees_decision_time"]) for s in basic], [(False, False)])
        pro = records[("PRO", "1.100000001")]["provenance"]["other_sources"]
        self.assertEqual([(s["agrees"], s["agrees_decision_time"]) for s in pro], [(False, True)])
        # The event file outranks a market file named after another market.
        held_record = records[("BASIC", "1.100000003")]
        self.assertEqual(held_record["provenance"]["member_path"], path(EVENT))
        self.assertEqual([s["member_path"] for s in held_record["provenance"]["other_sources"]],
                         [path("1.100000002")])
        self.assertFalse(held_record["checks"]["market_file_holds_other_market"])
        # 1.100000006 is only in the file named after 1.100000005, but the event file of that directory exists, so it
        # is primary although it never mentions the market: the explicit empty view, nothing resolved.
        absent = records[("BASIC", "1.100000006")]
        self.assertEqual((absent["provenance"]["member_path"], absent["decision_time"]), (path(EVENT), NO_VIEW))
        self.assertEqual([(s["member_path"], s["agrees"], s["agrees_decision_time"])
                          for s in absent["provenance"]["other_sources"]], [(path("1.100000005"), False, False)])
        self.assertEqual((absent["evaluator_only_outcome"]["winners_observed"],
                          absent["evaluator_only_outcome"]["final_runners"]), (None, []))
        only = records[("BASIC", "1.100000008")]
        self.assertEqual((only["provenance"]["member_path"], only["checks"]["market_file_holds_other_market"]),
                         (path("1.100000007", event="27000009"), True))

    def test_the_primary_source_never_depends_on_later_readability(self):
        """R1, Astra's pair: market file M and event file E disagree 10 minutes before the off (M: runner 1 ACTIVE,
        E: REMOVED) and both end with a heartbeat 10 minutes after it. One line M cannot read after that heartbeat
        used to make E primary (read whole), changing the decision view; now the primary is chosen by name only and
        both TARs give the same decision_time, M's; only the settlement side differs (read_complete). A failure
        before the cutoff ends M's own view where it stopped; E is still not consulted. E cut after the off is
        listed as not read whole. M cut inside its first batch keeps no marketTime: the explicit empty view (the
        evaluator side says where M stopped). M that fails before any message for its market holds no record of it
        and is still primary, since it exists: the same empty view, nothing resolved; E's record is only listed (it
        used to be primary, with M listed with agrees null)."""
        mid = "1.100000001"

        def source(status):
            return [md_change(mid, OFF - timedelta(minutes=60), definition(iso(OFF), [runner(1), runner(2)])),
                    md_change(mid, OFF - timedelta(minutes=10), definition(iso(OFF), [runner(1, status), runner(2)])),
                    {"op": "mcm", "clk": "2", "pt": ms(OFF + timedelta(minutes=10)), "mc": []}]

        market_file, event_file = source("ACTIVE"), source("REMOVED")
        records = {}
        for label, data in (("intact", stream(market_file)),
                            ("cut after the off", bz2.compress(lines(market_file) + b"not json\n")),
                            ("cut before the off", bz2.compress(lines(market_file[:2]) + b"not json\n"))):
            root = self.new_root()
            sha, _ = self.build([(path(mid), data), (path(EVENT), stream(event_file))], root=root)
            records[label] = self.records(sha, root)[mid]
            self.assertEqual(records[label]["provenance"]["member_path"], path(mid), label)
        intact, late = records["intact"], records["cut after the off"]
        self.assertEqual(late["decision_time"], intact["decision_time"])
        decision, active = late["decision_time"], [[ms(OFF - timedelta(minutes=60)), "ACTIVE"]]
        self.assertEqual(({r["id"]: r["status"] for r in decision["runners"]}, decision["removals"],
                          decision["view_ends"]),
                         ({1: active, 2: active}, [], {"pt": ms(OFF), "reason": "first_published_scheduled_off"}))
        outcomes = [record["evaluator_only_outcome"] for record in (intact, late)]
        self.assertEqual([(outcome["read_complete"], outcome["complete_before_pt"]) for outcome in outcomes],
                         [(True, None), (False, ms(OFF + timedelta(minutes=10)))])
        # E stays an audit-only other source either way: its disagreement is recorded, never used.
        for record in (intact, late):
            self.assertEqual([(s["member_path"], s["read_complete"], s["agrees_decision_time"])
                              for s in record["provenance"]["other_sources"]], [(path(EVENT), True, False)])
        early = records["cut before the off"]["decision_time"]
        self.assertEqual(early["view_ends"], {"pt": ms(OFF - timedelta(minutes=10)),
                                              "reason": "member_unreadable_from_pt"})
        self.assertEqual(early["runners"], decision["runners"])
        # The other way round: E cut after the off is still only an other source, now one not read whole.
        root = self.new_root()
        sha, _ = self.build([(path(mid), stream(market_file)),
                             (path(EVENT), bz2.compress(lines(event_file) + b"not json\n"))], root=root)
        record = self.records(sha, root)[mid]
        self.assertEqual((record["provenance"]["member_path"], record["decision_time"]),
                         (path(mid), intact["decision_time"]))
        self.assertEqual([(s["member_path"], s["read_complete"], s["agrees_decision_time"])
                          for s in record["provenance"]["other_sources"]], [(path(EVENT), False, False)])
        # M cut after its first batch: no first-published marketTime survives, so the explicit empty view.
        root = self.new_root()
        sha, _ = self.build([(path(mid), bz2.compress(lines(market_file[:1]) + b"not json\n"
                                                      + lines(market_file[1:]))),
                             (path(EVENT), stream(event_file))], root=root)
        record, first = self.records(sha, root)[mid], ms(OFF - timedelta(minutes=60))
        self.assertEqual((record["provenance"]["member_path"], record["decision_time"]), (path(mid), NO_VIEW))
        self.assertEqual(record["evaluator_only_outcome"]["decision_cutoff"],
                         {"pt": first, "reason": "member_unreadable_from_pt"})
        # M unreadable before any message for its market: it holds no record of it but exists, so it is primary
        # with the explicit empty view and an outcome with nothing resolved; E is listed, never used.
        root = self.new_root()
        sha, _ = self.build([(path(mid), bz2.compress(b"not json\n" + lines(market_file))),
                             (path(EVENT), stream(event_file))], root=root)
        record = self.records(sha, root)[mid]
        self.assertEqual((record["provenance"]["member_path"], record["decision_time"]), (path(mid), NO_VIEW))
        outcome = record["evaluator_only_outcome"]
        self.assertEqual((outcome["read_complete"], outcome["unreadable_reason"], outcome["complete_before_pt"],
                          outcome["decision_cutoff"], outcome["winners_observed"], outcome["final_status"]),
                         (False, "not_json", None, {"pt": None, "reason": "member_unreadable_from_pt"}, None, None))
        self.assertEqual([(s["member_path"], s["file_type"], s["read_complete"], s["agrees"], s["agrees_decision_time"])
                          for s in record["provenance"]["other_sources"]], [(path(EVENT), "event", True, False, False)])
        self.assertEqual([(f["path"], f["status"], f["markets"]) for f in self.head(sha, root)["files"]],
                         [(path(mid), "unreadable", []), (path(EVENT), "indexed", [mid])])
        report = audit(root)["totals"]
        self.assertEqual((report["multiple_sources"], report["sources_disagree"],
                          report["sources_disagree_decision_time"]), (1, 1, 1))

    def test_a_failing_message_whose_pt_was_read_ends_the_view_at_that_pt(self):
        """R1 after review: M's last definition is 10 minutes before the off, and the next line, 10 minutes after
        it, is read as far as its pt and then fails (a bad runner change, img 1, segmentType, a bad market id,
        duplicate runner ids). Every message published before that pt was applied, so complete_before_pt is that pt
        and decision_time equals the intact file's; it used to end at the last pt applied, 10 minutes before the
        off, and leave a decision 5 minutes before the off with no view. The same failure 5 minutes before the off
        ends the view there and keeps the batch 10 minutes before it. A line whose pt cannot be read (not JSON)
        still ends the view at the last pt applied, as its time is unknown."""
        mid = "1.100000001"
        opening = [md_change(mid, OFF - timedelta(minutes=60), definition(iso(OFF), [runner(1), runner(2)])),
                   md_change(mid, OFF - timedelta(minutes=10),
                             definition(iso(OFF), [runner(1), runner(2, "REMOVED")]))]
        root = self.new_root()
        sha, _ = self.build([(path(mid), stream(opening))], root=root)
        intact = self.records(sha, root)[mid]["decision_time"]

        def bad(pt):
            return {"malformed_runner_change": {"op": "mcm", "pt": pt, "mc": [{"id": mid, "rc": 5}]},
                    "malformed_image_flag": {"op": "mcm", "pt": pt, "mc": [{"id": mid, "img": 1}]},
                    "segmented": {"op": "mcm", "pt": pt, "segmentType": "SEG_START", "mc": []},
                    "malformed_market_id": {"op": "mcm", "pt": pt, "mc": [{"id": "x"}]},
                    "malformed_runner": {"op": "mcm", "pt": pt, "mc": [{"id": mid, "marketDefinition": definition(
                        iso(OFF), [runner(1), runner(1)])}]}}

        late, soon = ms(OFF + timedelta(minutes=10)), ms(OFF - timedelta(minutes=5))
        for pt, view_ends in ((late, intact["view_ends"]), (soon, {"pt": soon, "reason": "member_unreadable_from_pt"})):
            for reason, message in bad(pt).items():
                with self.subTest(reason=reason, after_the_off=pt == late):
                    root = self.new_root()
                    sha, _ = self.build([(path(mid), bz2.compress(lines(opening + [message])))], root=root)
                    record = self.records(sha, root)[mid]
                    self.assertEqual(record["decision_time"], {**intact, "view_ends": view_ends})
                    outcome = record["evaluator_only_outcome"]
                    self.assertEqual((outcome["read_complete"], outcome["unreadable_reason"],
                                      outcome["complete_before_pt"]), (False, reason, pt))
                    self.assertEqual([(f["reason"], f["complete_before_pt"]) for f in self.head(sha, root)["files"]],
                                     [(reason, pt)])
        root = self.new_root()
        sha, _ = self.build([(path(mid), bz2.compress(lines(opening) + b"not json\n"))], root=root)
        self.assertEqual(self.records(sha, root)[mid]["decision_time"]["view_ends"],
                         {"pt": ms(OFF - timedelta(minutes=10)), "reason": "member_unreadable_from_pt"})

    def test_an_image_replaces_the_market_state_at_its_pt(self):
        """R2, plan section 5 img_replaces_market_state. Astra's first probe: OPEN with two ACTIVE runners 10 minutes
        before the off, then an image with no definition 6 minutes before it. The image empties the published state
        at its pt (every definition field null, every runner null and unlisted) and keeps what came before; with img
        false the change merges as before. An image whose definition has fewer runners makes the omitted runner no
        longer current at its pt (a plain definition only marks it unlisted). A non-boolean img is unreadable."""
        mid = "1.100000001"
        opened, reset = ms(OFF - timedelta(minutes=10)), ms(OFF - timedelta(minutes=6))
        opening = md_change(mid, OFF - timedelta(minutes=10), definition(iso(OFF), [runner(1), runner(2)]))
        views = {}
        for img in (False, True):
            image = mcm(OFF - timedelta(minutes=6), [{"id": mid, "img": img, "rc": [{"id": 1, "ltp": 2.0}]}])
            root = self.new_root()
            sha, _ = self.build([(path(mid), stream([opening, image]))], root=root)
            views[img] = self.records(sha, root)[mid]["decision_time"]
        self.assertEqual(views[False]["definition"]["status"], [[opened, "OPEN"]])
        self.assertEqual(views[False]["runners"][0]["status"], [[opened, "ACTIVE"]])
        self.assertEqual(views[True]["definition"]["status"], [[opened, "OPEN"], [reset, None]])
        self.assertEqual({key: timeline[-1] for key, timeline in views[True]["definition"].items()},
                         dict.fromkeys(views[True]["definition"], [reset, None]))
        for state in views[True]["runners"]:
            self.assertEqual((state["status"], state["listed"], state["name"]),
                             ([[opened, "ACTIVE"], [reset, None]], [[opened, True], [reset, False]],
                              [[opened, f"Horse {state['id']}"], [reset, None]]))
        self.assertEqual({key: views[True][key] for key in ("first_published_market_time", "removals", "view_ends")},
                         {key: views[False][key] for key in ("first_published_market_time", "removals", "view_ends")})
        # An image whose definition leaves runner 3 out, against a plain definition leaving it out.
        listed, fewer = ms(OFF - timedelta(minutes=60)), ms(OFF - timedelta(minutes=30))
        three = md_change(mid, OFF - timedelta(minutes=60), definition(iso(OFF), [runner(1), runner(2), runner(3)]))
        for img, status in ((True, [[listed, "ACTIVE"], [fewer, None]]), (False, [[listed, "ACTIVE"]])):
            with self.subTest(img=img):
                two = mcm(OFF - timedelta(minutes=30), [{"id": mid, "img": img,
                                                         "marketDefinition": definition(iso(OFF),
                                                                                        [runner(1), runner(2)])}])
                root = self.new_root()
                sha, _ = self.build([(path(mid), stream([three, two]))], root=root)
                runners = {r["id"]: r for r in self.records(sha, root)[mid]["decision_time"]["runners"]}
                self.assertEqual((runners[3]["status"], runners[3]["listed"]),
                                 (status, [[listed, True], [fewer, False]]))
                self.assertEqual([runners[rid]["status"] for rid in (1, 2)], [[[listed, "ACTIVE"]]] * 2)
        for flag in (b"1", b'"true"', b"[]"):
            with self.subTest(img=flag):
                line = b'{"op":"mcm","pt":1,"mc":[{"id":"1.100000002","img":%s}]}\n' % flag
                root = self.new_root()
                sha, _ = self.build([(path("1.100000002"), bz2.compress(line))], root=root)
                self.assertEqual([f["reason"] for f in self.head(sha, root)["files"]], ["malformed_image_flag"])

    def test_runners_left_out_of_the_last_definition_are_not_current_results(self):
        """R2, Astra's second probe: CLOSED with runner 1 WINNER at BSP 2.0 and runner 2 LOSER at 3.0, then an image
        definition carrying only runner 2, now WINNER at 3.0. Runner 1's old WINNER and BSP stay in its timelines
        but are not a current result: one winner, no dead heat (the audit counts none), results revised after
        CLOSED, no missing BSP for runner 1. A plain definition leaving runner 1 out ends the same way. A runner
        reset by an image and removed again is one removal, and a marketTime reset and republished is no revision."""
        def settled(market_id, img):
            closed = [runner(1, "WINNER", bsp=2.0), runner(2, "LOSER", bsp=3.0)]
            image = definition(iso(OFF), [runner(2, "WINNER", bsp=3.0)], status="CLOSED", in_play=True,
                               bspReconciled=True)
            return [md_change(market_id, OFF - timedelta(hours=1), definition(iso(OFF), [runner(1), runner(2)])),
                    md_change(market_id, OFF + timedelta(minutes=5),
                              definition(iso(OFF), closed, status="CLOSED", in_play=True, bspReconciled=True)),
                    mcm(OFF + timedelta(minutes=10), [{"id": market_id, "img": img, "marketDefinition": image}])]

        removed = [md_change("1.100000003", OFF - timedelta(hours=1), definition(iso(OFF), [runner(1), runner(2)])),
                   md_change("1.100000003", OFF - timedelta(minutes=40),
                             definition(iso(OFF), [runner(1), runner(2, "REMOVED")])),
                   mcm(OFF - timedelta(minutes=30), [{"id": "1.100000003", "img": True}]),
                   mcm(OFF - timedelta(minutes=20), [{"id": "1.100000003", "img": True, "marketDefinition":
                                                      definition(iso(OFF), [runner(1), runner(2, "REMOVED")])}])]
        sha, _ = self.build([(path("1.100000001"), stream(settled("1.100000001", True))),
                             (path("1.100000002"), stream(settled("1.100000002", False))),
                             (path("1.100000003"), stream(removed))])
        records, closed_at, reset = self.records(sha), ms(OFF + timedelta(minutes=5)), ms(OFF + timedelta(minutes=10))
        for mid in ("1.100000001", "1.100000002"):
            with self.subTest(market=mid):
                outcome = records[mid]["evaluator_only_outcome"]
                self.assertEqual(outcome["final_runners"], [{"id": 1, "status": None, "bsp": None, "listed": False},
                                                            {"id": 2, "status": "WINNER", "bsp": 3.0, "listed": True}])
                self.assertEqual((outcome["winners_observed"], outcome["dead_heat"],
                                  outcome["results_revised_after_close"], outcome["missing_final_bsp_runner_ids"],
                                  outcome["unusable_final_bsp_runner_ids"]), (1, False, True, [], []))
        history = {r["id"]: r for r in records["1.100000001"]["evaluator_only_outcome"]["runners_after_cutoff"]}
        self.assertEqual((history[1]["status"], history[1]["bsp"]),
                         ([[closed_at, "WINNER"], [reset, None]], [[closed_at, 2.0], [reset, None]]))
        plain = {r["id"]: r for r in records["1.100000002"]["evaluator_only_outcome"]["runners_after_cutoff"]}
        self.assertEqual(plain[1]["status"], [[closed_at, "WINNER"]])           # history kept, not a current result
        decision = records["1.100000003"]["decision_time"]
        self.assertEqual([(item["id"], item["pt"]) for item in decision["removals"]],
                         [(2, ms(OFF - timedelta(minutes=40)))])
        self.assertEqual([value for _, value in decision["definition"]["marketTime"]], [iso(OFF), None, iso(OFF)])
        totals = audit(self.root)["totals"]
        self.assertEqual((totals["dead_heats"], totals["results_revised_after_close"], totals["market_time_revised"],
                          totals["removals_before_cutoff"]), (0, 2, 0, 1))

    def test_a_result_reset_reopened_or_unlisted_after_close_is_not_current(self):
        """R2 after review, each after a CLOSED definition with a settled time (runner 1 WINNER at 2.0, runner 2
        LOSER at 3.0). An image with no definition resets every field, the settlement fields too: settled_time is
        null and both outcome_definition timelines end null; no runner is listed, so there is no winner count (null,
        not 0) and no dead heat to judge. An image whose definition reopens the market: no winner count or dead heat
        either (both used to read the history: 0 winners, dead heat false). A plain CLOSED definition that only
        drops runner 2, statuses and bsp unchanged, is a revision of the result."""
        closed_at, later = OFF + timedelta(minutes=5), OFF + timedelta(minutes=10)
        settled = [runner(1, "WINNER", bsp=2.0), runner(2, "LOSER", bsp=3.0)]

        def closed(runners):
            return definition(iso(OFF), runners, status="CLOSED", in_play=True, bspReconciled=True,
                              settledTime=iso(closed_at))

        last = {"1.100000001": {"img": True},
                "1.100000002": {"img": True, "marketDefinition": definition(iso(OFF), [runner(1), runner(2)],
                                                                            in_play=True)},
                "1.100000003": {"marketDefinition": closed(settled[:1])}}
        sha, _ = self.build([(path(mid), stream([
            md_change(mid, OFF - timedelta(hours=1), definition(iso(OFF), [runner(1), runner(2)])),
            md_change(mid, closed_at, closed(settled)), mcm(later, [{"id": mid, **change}])]))
            for mid, change in last.items()])
        outcomes = {mid: record["evaluator_only_outcome"] for mid, record in self.records(sha).items()}
        keys = ("final_status", "closed_reached", "settled_time", "winners_observed", "dead_heat",
                "results_revised_after_close")
        self.assertEqual({mid: tuple(outcome[key] for key in keys) for mid, outcome in outcomes.items()},
                         {"1.100000001": (None, True, None, None, None, True),
                          "1.100000002": ("OPEN", True, None, None, None, True),
                          "1.100000003": ("CLOSED", True, iso(closed_at), 1, False, True)})
        opened = ms(OFF - timedelta(hours=1))
        self.assertEqual(outcomes["1.100000001"]["outcome_definition"],
                         {"bspReconciled": [[opened, False], [ms(closed_at), True], [ms(later), None]],
                          "settledTime": [[ms(closed_at), iso(closed_at)], [ms(later), None]]})
        self.assertEqual(outcomes["1.100000003"]["final_runners"],
                         [{"id": 1, "status": "WINNER", "bsp": 2.0, "listed": True},
                          {"id": 2, "status": None, "bsp": None, "listed": False}])
        totals = audit(self.root)["totals"]
        self.assertEqual((totals["dead_heats"], totals["closed_dead_heat_not_assessable"],
                          totals["results_revised_after_close"]), (0, 2, 3))

    def test_tiers_of_one_market_stay_separate_records(self):
        sha, indexed = self.build([(path(EVENT), stream(full_market())),
                                   (path(EVENT, plan="PRO"), stream(full_market()))])
        lines_ = (self.root / "index" / sha / market_index.MARKETS_FILE).read_bytes().splitlines()
        keys = [(r["provenance"]["plan"], r["market_id"], r["provenance"]["other_sources"]) for r in map(json.loads,
                                                                                                         lines_)]
        self.assertEqual(keys, [("BASIC", "1.100000001", []), ("PRO", "1.100000001", [])])
        self.assertEqual(indexed["archives"][0]["markets"], 2)

    def test_the_stream_rules_and_unreadable_members(self):
        good = md_change("1.100000002", OFF - timedelta(hours=1), definition(iso(OFF), [runner(1)]))
        backwards = [good, md_change("1.100000002", OFF - timedelta(hours=2), definition(iso(OFF), [runner(1)]))]
        duplicate = md_change("1.100000010", OFF, definition(iso(OFF), [runner(1), runner(1)]))
        cases = {"1.100000002": (stream(backwards), "invalid_or_decreasing_pt"),
                 "1.100000003": (bz2.compress(b'{"op":"mcm","pt":1,"mc":[]}\n{"op": broken\n'), "not_json"),
                 "1.100000004": (bz2.compress(b'{"op":"mcm","pt":1,"segmentType":"SEG_START","mc":[]}\n'),
                                 "segmented"),
                 "1.100000005": (bz2.compress(b'{"op":"mcm","pt":1,"mc":[{"id":"1.100000005",'
                                              b'"rc":[{"id":1,"ltp":NaN}]}]}\n'), "not_json"),
                 "1.100000006": (bz2.compress(b'{"op":"mcm","pt":1,"mc":[{"id":"x"}]}\n'), "malformed_market_id"),
                 "1.100000007": (bz2.compress(b"[1, 2]\n"), "malformed_message"),
                 "1.100000008": (bz2.compress(b'{"op":"mcm","pt":"1","mc":[]}\n'), "invalid_or_decreasing_pt"),
                 "1.100000009": (bz2.compress(b'{"op":"mcm","pt":1.5,"mc":[]}\n'), "invalid_or_decreasing_pt"),
                 "1.100000010": (stream([duplicate]), "malformed_runner"),
                 "1.100000011": (bz2.compress(b'{"op":"mcm","pt":1,"mc":[{"id":"1.100000011","rc":{"id":1}}]}\n'),
                                 "malformed_runner_change"),
                 "1.100000012": (bz2.compress(b'{"op":"mcm","pt":1,"mc":[{"id":"1.100000012",'
                                              b'"marketDefinition":[]}]}\n'), "malformed_market_definition"),
                 "1.100000013": (bz2.compress(lines([good]).replace(b"5.0", b"1e999")), "not_json")}
        # A line that is not an mcm message is skipped, and the member is read whole.
        skipped = b'{"op":"connection","connectionId":"x"}\n' + lines(full_market("1.100000014"))
        members = [(path("1.100000001"), stream(full_market())), (path("1.100000014"), bz2.compress(skipped))]
        members += [(path(mid), data) for mid, (data, _) in cases.items()]
        sha, indexed = self.build(members)
        head = self.head(sha)
        reasons = {item["path"]: item["reason"] for item in head["files"] if item["status"] == "unreadable"}
        self.assertEqual(reasons, {path(mid): reason for mid, (_, reason) in cases.items()})
        self.assertEqual(indexed["archives"][0]["files_unreadable"], len(cases))
        # Only a market seen in a message applied whole gets a record; it says it was not read whole.
        records = self.records(sha)
        self.assertEqual(sorted(records), ["1.100000001", "1.100000002", "1.100000014"])
        partial = records["1.100000002"]["evaluator_only_outcome"]
        self.assertEqual((partial["read_complete"], partial["unreadable_reason"], partial["complete_before_pt"]),
                         (False, "invalid_or_decreasing_pt", ms(OFF - timedelta(hours=1))))
        self.assertEqual(records["1.100000002"]["decision_time"]["definition"], {})     # its only batch is cut
        self.assertTrue(records["1.100000014"]["evaluator_only_outcome"]["read_complete"])
        # Line and member size bounds, under stricter limits on another root.
        root = self.new_root()
        sha2, _ = self.build([(path("1.100000001"), stream(full_market()))], root=root)
        longest = max(len(json.dumps(m)) for m in full_market()) + 1
        total = len(lines(full_market()))
        for limits, reason in (({"max_line_bytes": longest - 1}, "line_too_long"),
                               ({"max_member_decompressed_bytes": total - 1}, "decompressed_too_large")):
            with self.subTest(reason=reason):
                for item in (root / "index" / sha2).iterdir():
                    item.unlink()
                result = index_archives(root, limits=DEFAULT_LIMITS.stricter(**limits))
                self.assertEqual(result["status"], "indexed", result)
                self.assertEqual([f["reason"] for f in self.head(sha2, root)["files"]], [reason])

    def test_a_bad_line_keeps_the_markets_read_before_it(self):
        """E4: a bad line after the off of an event file keeps both races, decision_time intact; a bad line before
        the off ends decision_time at the last batch read whole."""
        both = sorted(full_market("1.500000001") + full_market("1.500000002"), key=lambda m: m["pt"])
        bad = (b'{"op":"mcm","pt":%d,"mc":[{"id":"1.500000001","rc":[{"id":1,"ltp":NaN}]}]}\n'
               % ms(OFF + timedelta(hours=1)))
        clean_root = self.new_root()
        clean_sha, _ = self.build([(path(EVENT), stream(both))], root=clean_root)
        sha, _ = self.build([(path(EVENT), bz2.compress(lines(both) + bad))])
        clean, broken = self.records(clean_sha, clean_root), self.records(sha)
        self.assertEqual(sorted(broken), ["1.500000001", "1.500000002"])
        last = ms(OFF + timedelta(minutes=20))
        self.assertEqual(self.head(sha)["files"][0]["complete_before_pt"], last)
        for mid in broken:
            with self.subTest(market=mid):
                self.assertEqual(broken[mid]["decision_time"], clean[mid]["decision_time"])
                outcome = broken[mid]["evaluator_only_outcome"]
                self.assertEqual((outcome["read_complete"], outcome["complete_before_pt"], outcome["closed_reached"]),
                                 (False, last, False))       # the batch at the last pt read may be incomplete
        early = lines(full_market("1.500000003")[:3]) + b"not json\n"            # read whole up to off-60m
        root = self.new_root()
        sha3, _ = self.build([(path("1.500000003"), bz2.compress(early))], root=root)
        record = self.records(sha3, root)["1.500000003"]
        cut = ms(OFF - timedelta(minutes=60))
        self.assertEqual(record["decision_time"]["view_ends"], {"pt": cut, "reason": "member_unreadable_from_pt"})
        self.assertTrue(max(all_pts(record["decision_time"])) < cut)
        self.assertEqual(len(record["decision_time"]["removals"]), 1)                 # the removal at off-90m
        # Definitions without a marketTime give no decision view, read whole or cut: the one explicit empty view
        # either way (it used to end null read whole and at the cut otherwise, so a later failure changed it); the
        # evaluator side still says where the member stopped.
        bare = [md_change("1.500000004", OFF - timedelta(minutes=60), drop(definition(iso(OFF), [runner(1)]),
                                                                             "marketTime")),
                md_change("1.500000004", OFF - timedelta(minutes=30), drop(definition(iso(OFF), [runner(1, "REMOVED")]),
                                                                             "marketTime"))]
        for data, cutoff in ((stream(bare), {"pt": None, "reason": "no_first_published_market_time"}),
                             (bz2.compress(lines(bare) + b"not json\n"),
                              {"pt": ms(OFF - timedelta(minutes=30)), "reason": "member_unreadable_from_pt"})):
            with self.subTest(read_whole=cutoff["pt"] is None):
                root = self.new_root()
                sha4, _ = self.build([(path("1.500000004"), data)], root=root)
                record = self.records(sha4, root)["1.500000004"]
                self.assertEqual(record["decision_time"], NO_VIEW)
                self.assertEqual(record["evaluator_only_outcome"]["decision_cutoff"], cutoff)

    def test_index_is_deterministic_hashed_and_refuses_tampering(self):
        sha, indexed = self.build([(path("1.100000001"), stream(full_market())), (path(EVENT), stream(full_market()))])
        summary = indexed["archives"][0]
        head_file = self.root / "index" / sha / market_index.HEAD_FILE
        markets_file = self.root / "index" / sha / market_index.MARKETS_FILE
        self.assertEqual(hashlib.sha256(head_file.read_bytes()).hexdigest(), summary["index_sha256"])
        head = json.loads(head_file.read_bytes())
        self.assertEqual(head["markets_sha256"], hashlib.sha256(markets_file.read_bytes()).hexdigest())
        self.assertEqual(sorted(p.name for p in head_file.parent.iterdir()),
                         [market_index.HEAD_FILE, market_index.MARKETS_FILE])         # no spill or partial left
        again = index_archives(self.root, sha)
        self.assertEqual(again["archives"][0]["state"], "unchanged")
        self.assertEqual(again["archives"][0]["index_sha256"], summary["index_sha256"])
        # A fresh data root gives the same bytes.
        other_root = self.tmp / "other"
        other_root.mkdir()
        ingest_archive(self.tmp / "build0.tar", other_root)
        index_archives(other_root)
        self.assertEqual((other_root / "index" / sha / market_index.MARKETS_FILE).read_bytes(),
                         markets_file.read_bytes())
        # Tampering: an edited index is never replaced; an edited raw member refuses the index.
        original = markets_file.read_bytes()
        markets_file.write_bytes(original.replace(b"Synthetic Park", b"Synthetic Fake"))
        self.assertEqual(index_archives(self.root)["problems"], [f"index:{sha}:index_would_change"])
        self.assertEqual(audit(self.root)["problems"], [f"index:{sha}:markets_do_not_match_head"])
        markets_file.write_bytes(original)
        head_bytes = head_file.read_bytes()
        head_file.write_bytes(archive.manifest_bytes({**head, "markets_file": "../../../etc/passwd"}))
        self.assertEqual(audit(self.root)["problems"], [f"index:{sha}:head_malformed"])
        head_file.write_bytes(archive.manifest_bytes({**head, "manifest_sha256": "0" * 64}))
        self.assertEqual(audit(self.root)["problems"], [f"index:{sha}:stale_for_manifest"])
        for files in ([{**head["files"][0], "status": ["indexed"]}], [{**head["files"][0], "status": "other"}]):
            head_file.write_bytes(archive.manifest_bytes({**head, "files": files}))
            self.assertEqual(audit(self.root)["problems"], [f"index:{sha}:head_malformed"])
        head_file.write_bytes(archive.manifest_bytes({**head, "refused_markets": ["1.100000001"]}))
        self.assertEqual(audit(self.root)["problems"], [f"index:{sha}:head_malformed"])
        head_file.write_bytes(json.dumps(head, indent=1).encode())
        self.assertEqual(audit(self.root)["problems"], [f"index:{sha}:head_not_canonical_v1"])
        head_file.write_bytes(archive.manifest_bytes({**head, "markets": head["markets"] + 1}))
        self.assertEqual(audit(self.root)["problems"], [f"index:{sha}:markets_do_not_match_head"])
        head_file.write_bytes(head_bytes)
        self.assertEqual(audit(self.root)["status"], "audited")
        # A leftover spill refuses the index until a person removes it.
        spill = head_file.parent / market_index.SPILL_FILE
        spill.write_bytes(b"")
        self.assertEqual(index_archives(self.root)["problems"], [f"index:{sha}:partial_file_present"])
        spill.unlink()
        raw = self.root / "raw" / sha / path("1.100000001")
        raw_bytes = raw.read_bytes()
        raw.write_bytes(raw_bytes[:-1] + bytes([raw_bytes[-1] ^ 1]))
        self.assertEqual(index_archives(self.root)["problems"], [f"raw:{path('1.100000001')}:member_hash_mismatch"])
        raw.unlink()
        self.assertEqual(index_archives(self.root)["problems"], [f"raw:{path('1.100000001')}:missing"])
        raw.write_bytes(raw_bytes)
        # An edited manifest (even a canonical one) is refused before any member is read.
        manifest_file = self.root / "manifests" / f"{sha}.json"
        manifest_bytes = manifest_file.read_bytes()
        manifest = json.loads(manifest_bytes)
        for edit, reason in ((lambda m: m["members"][0].update(month="2015-05"), "member_inconsistent"),
                             (lambda m: m["members"][0].update(sha256="x" * 64), "member_inconsistent"),
                             (lambda m: m.update(kind="other"), "unsupported"),
                             (lambda m: m.update(refused=[{"path": "x"}]), "malformed"),
                             (lambda m: m.pop("refused"), "malformed"),
                             (lambda m: m.update(directories=[1]), "malformed")):
            with self.subTest(reason=reason):
                changed = json.loads(manifest_bytes)
                edit(changed)
                manifest_file.write_bytes(archive.manifest_bytes(changed))
                self.assertEqual(index_archives(self.root)["problems"], [f"manifest:{sha}:{reason}"])
        manifest_file.write_bytes(b"{}")
        self.assertEqual(index_archives(self.root)["problems"], [f"manifest:{sha}:not_canonical"])
        manifest_file.write_bytes(manifest_bytes)
        (self.root / "manifests" / f"{'e' * 64}.json").write_bytes(manifest_bytes)
        self.assertEqual(index_archives(self.root, "e" * 64)["problems"],
                         [f"manifest:{'e' * 64}:names_another_archive"])
        self.assertEqual(index_archives(self.root, "f" * 64)["problems"], [f"manifest:{'f' * 64}:missing"])
        self.assertEqual(index_archives(self.root, "nothex")["problems"], ["archive:malformed_sha256"])
        self.assertEqual(manifest["archive"]["sha256"], sha)

    def test_the_index_applies_the_month_gate_to_member_paths(self):
        """A canonical manifest naming a member in a sealed month (as one written under an older gate would): that
        member is listed as refused and never read."""
        root = self.new_root()
        result = ingest_archive(self.tar([(path("1.100000001"), stream(full_market()))], "gate.tar"), root)
        sha = result["archive_sha256"]
        manifest_file = root / "manifests" / f"{sha}.json"
        manifest = json.loads(manifest_file.read_bytes())
        sealed = dict(manifest["members"][0], path=path("1.100000001", datetime(2026, 3, 1, tzinfo=timezone.utc)),
                      month="2026-03", date="2026-03-01")
        manifest["members"].append(sealed)
        manifest_file.write_bytes(archive.manifest_bytes(manifest))
        self.assertEqual(index_archives(root)["status"], "indexed")
        files = {item["path"]: (item["status"], item["reason"]) for item in self.head(sha, root)["files"]}
        self.assertEqual(files[sealed["path"]], ("refused", "month_sealed_holdout"))

    def test_market_time_in_a_refused_month_is_not_indexed(self):
        """Defence in depth behind the ingest gate: a content month the ingest scan cannot see (an escaped key) is
        refused at index time by the parsed marketTime."""
        when = datetime(2026, 4, 2, 14, tzinfo=timezone.utc)
        hidden = json.dumps(md_change("1.100000002", when - timedelta(hours=1),
                                      definition(iso(when), [runner(1)]))).replace('"marketTime"', '"market\\u0054ime"')
        sha, indexed = self.build([(path("1.100000001"), stream(full_market())),
                                   (path("1.100000002"), bz2.compress(hidden.encode() + b"\n"))])
        self.assertEqual(sorted(self.records(sha)), ["1.100000001"])
        self.assertEqual(self.head(sha)["refused_markets"], [{"market_id": "1.100000002",
                                                              "member_path": path("1.100000002"),
                                                              "reason": "market_time_month_sealed_holdout"}])
        self.assertEqual(indexed["archives"][0]["markets_refused"], 1)
        self.assertEqual(audit(self.root)["refused_markets"], [{"archive_sha256": sha, "market_id": "1.100000002",
                                                                "member_path": path("1.100000002"),
                                                                "reason": "market_time_month_sealed_holdout"}])


def post_off_lines(rng, mid, start, count):
    """count random lines (bytes) for the property test below, each published from pt start on: heartbeats,
    definitions (any status, runner results, bsp, img, sometimes no marketTime), images without a definition, runner
    changes, another market's definition, and lines the reader fails on (not JSON, img 1, segmented, a pt from before
    the off). Every marketTime is at or before the off, so no append announces a later off: that would be a new
    decision after the append, not a change to an earlier one."""
    out, pt = [], start
    for _ in range(count):
        pt += rng.randrange(600001)
        # Mostly definitions, which could move a primary chosen by content; a failing line ends the member.
        kind = rng.choices(range(10), weights=(2, 5, 5, 2, 2, 2, 1, 1, 1, 1))[0]
        if kind == 6:
            out.append(b"not json\n")
            continue
        if kind in (1, 2):
            statuses = [rng.choice(("ACTIVE", "REMOVED", "WINNER", "LOSER")) for _ in range(rng.randrange(1, 4))]
            value = definition(iso(OFF - timedelta(minutes=rng.choice((0, 30)))),
                               [runner(i + 1, status, bsp=rng.choice((None, 2.0, "NaN")))
                                for i, status in enumerate(statuses)],
                               status=rng.choice(("OPEN", "SUSPENDED", "CLOSED")), in_play=rng.random() < 0.5)
            value = drop(value, "marketTime") if rng.random() < 0.2 else value
            message = {"op": "mcm", "pt": pt, "mc": [{"id": mid, "marketDefinition": value, "img": kind == 2}]}
        else:
            message = {0: heartbeat(pt), 3: {"op": "mcm", "pt": pt, "mc": [{"id": mid, "img": True}]},
                       4: {"op": "mcm", "pt": pt, "mc": [{"id": mid, "rc": [{"id": 1, "ltp": 2.0}]}]},
                       5: {"op": "mcm", "pt": pt, "mc": [{"id": "1.100000099",
                                                          "marketDefinition": definition(iso(OFF), [runner(1)])}]},
                       7: {"op": "mcm", "pt": pt, "mc": [{"id": mid, "img": 1}]},
                       8: {"op": "mcm", "pt": ms(OFF) - 1, "mc": []},
                       9: {"op": "mcm", "pt": pt, "segmentType": "SEG_START", "mc": []}}[kind]
        out.append(json.dumps(message).encode() + b"\n")
    return b"".join(out)


class PrimarySourceTests(Workspace):
    """R1 after Astra's second review: the decision source of a market is chosen from member paths, or, with no file
    named after the market, from its earliest mention, so nothing published after a decision (whether a source holds
    the market, what it says, whether it can be read) can move that decision to another source or erase its view.
    Only a market that no member mentions by the decision (no view then) can have its source moved by a later
    mention, to one that is still later; and a member refused at ingest is not read (docs/backtest-plan.md 4.1)."""

    MID, OTHER = "1.100000001", "1.100000002"

    def opening(self, mid=None, second="ACTIVE"):
        """The normal definition 10 minutes before the off: GB WIN, OPEN, BSP, event 27000001, one winner, runners 1
        and 2 (runner 2 with status second)."""
        return md_change(mid or self.MID, OFF - timedelta(minutes=10),
                         definition(iso(OFF), [runner(1), runner(2, second)]))

    def results(self, mid=None):
        return md_change(mid or self.MID, OFF + timedelta(minutes=20),
                         definition(iso(OFF), [runner(1, "WINNER", bsp=2.0), runner(2, "LOSER", bsp=3.0)],
                                    status="CLOSED", in_play=True, bspReconciled=True))

    def build_all(self, tars):
        """Ingest every TAR (a list of (path, bytes)) into one new root and index once; (the records of MID, one per
        TAR in order, None where the market has none; the root; the archive sha256s). Equal TARs share one archive
        (the manifest names the TAR file, so a second name would be refused as would_change)."""
        root, shas, built = self.new_root(), [], {}
        for members in tars:
            key = tuple(members)
            if key not in built:
                result = ingest_archive(self.tar(members, f"tar{self.roots}-{len(built)}.tar"), root)
                self.assertEqual(result["status"], "ingested", result)
                built[key] = result["archive_sha256"]
            shas.append(built[key])
        self.assertEqual(index_archives(root)["status"], "indexed")
        return [self.records(sha, root).get(self.MID) for sha in shas], root, shas

    def test_a_named_market_file_is_primary_before_it_holds_anything(self):
        """Astra's fully readable pair (OFF 1433167200000, market 1.100000001, event 27000001). M holds only
        heartbeats; E holds the normal definition 10 minutes before the off and ends with a heartbeat after it. A
        second TAR only appends M's first definition after the off, the same as E's: 10 minutes after it with one
        heartbeat 10 minutes before (root's probe), or 20 minutes after it with heartbeats 10 minutes either side
        (the independent reviewer's). The primary used to be the first source holding a record, so the append moved
        it from E (first-published marketTime and two ACTIVE runners from 10 minutes before the off) to M (nothing):
        a message published after the off erased the earlier view. M exists, so it is primary in both TARs with the
        same explicit empty view and nothing resolved; E is only listed, read whole and disagreeing."""
        off = ms(OFF)
        self.assertEqual(off, 1433167200000)
        event_file = stream([self.opening(), heartbeat(off + 600000)])
        for beats, late in (([off - 600000], off + 600000), ([off - 600000, off + 600000], off + 1200000)):
            with self.subTest(appended_at=late - off):
                first = [heartbeat(pt) for pt in beats]
                appended = first + [{**self.opening(), "pt": late}]
                records, root, shas = self.build_all([[(path(self.MID), stream(market_file)), (path(EVENT), event_file)]
                                                      for market_file in (first, appended)])
                before, after = records
                self.assertEqual((before["decision_time"], after["decision_time"]), (NO_VIEW, NO_VIEW))
                for record, final_status in zip(records, (None, "OPEN")):
                    self.assertEqual(record["provenance"]["member_path"], path(self.MID))
                    self.assertEqual([(s["member_path"], s["read_complete"], s["agrees_decision_time"])
                                      for s in record["provenance"]["other_sources"]], [(path(EVENT), True, False)])
                    outcome = record["evaluator_only_outcome"]
                    self.assertEqual((outcome["read_complete"], outcome["final_status"], outcome["closed_reached"],
                                      outcome["winners_observed"], outcome["dead_heat"],
                                      outcome["results_revised_after_close"]), (True, final_status, False, None, None,
                                                                                None))
                self.assertEqual(before["evaluator_only_outcome"]["decision_cutoff"],
                                 {"pt": None, "reason": "no_first_published_market_time"})
                self.assertEqual((before["evaluator_only_outcome"]["final_runners"], before["checks"]["first_pt"]),
                                 ([], None))
                self.assertEqual(after["evaluator_only_outcome"]["definition_after_cutoff"]["marketTime"],
                                 [[late, iso(OFF)]])
                for sha, held in zip(shas, ([], [self.MID])):
                    self.assertEqual({f["path"]: (f["status"], f["markets"]) for f in self.head(sha, root)["files"]},
                                     {path(self.MID): ("indexed", held), path(EVENT): ("indexed", [self.MID])})
                # E's earlier view, which used to be the primary's, is in the index only as an audit hash.
                totals = audit(root)["totals"]
                self.assertEqual((totals["multiple_sources"], totals["sources_disagree_decision_time"]), (1, 1))

    def test_a_named_market_files_view_is_kept_whatever_the_event_file_says_later(self):
        """M publishes the market 10 minutes before the off with two ACTIVE runners; E differs then (runner 2
        REMOVED). M's view is the decision view with or without E, and nothing E publishes or loses after the off
        changes it: results, a marketTime revision with a runner added, an image without a definition, a line it
        cannot read, or E cut back to its definition."""
        market_file = stream([self.opening(), heartbeat(ms(OFF + timedelta(minutes=10))), self.results()])
        event_base = [self.opening(second="REMOVED"), heartbeat(ms(OFF + timedelta(minutes=10)))]
        later = [self.results(), md_change(self.MID, OFF + timedelta(minutes=20),
                                           definition(iso(OFF + timedelta(minutes=30)),
                                                      [runner(1), runner(2), runner(3)])),
                 mcm(OFF + timedelta(minutes=20), [{"id": self.MID, "img": True}])]
        events = [None, stream(event_base), *(stream(event_base + [message]) for message in later),
                  bz2.compress(lines(event_base) + b"not json\n"), stream(event_base[:1])]
        records, _, _ = self.build_all([[(path(self.MID), market_file)] + ([(path(EVENT), data)] if data else [])
                                        for data in events])
        view = records[0]["decision_time"]
        self.assertEqual((view["first_published_market_time"]["pt"], view["removals"],
                          [r["status"] for r in view["runners"]]),
                         (ms(OFF - timedelta(minutes=10)), [],
                          [[[ms(OFF - timedelta(minutes=10)), "ACTIVE"]]] * 2))
        for n, record in enumerate(records):
            with self.subTest(event_variant=n):
                self.assertEqual((record["provenance"]["member_path"], record["decision_time"]), (path(self.MID), view))
                self.assertEqual(record["evaluator_only_outcome"]["winners_observed"], 1)

    def test_with_no_named_market_file_the_event_files_view_survives_later_appends(self):
        """No member is named after the market. E holds it 10 minutes before the off and ends with a heartbeat after
        it, so E is primary (the event file of the directory of its earliest mention); appending to E after the off
        (results, a revision to a later off, an image, another market, a line it cannot read) never changes the
        view. A file named after another market in the same directory that first mentions the market after the off
        changes nothing either. And the other way round: when the market is held before the off only by a file
        named after another market, E (existing, in that directory) is still primary although it never mentions
        it, with the explicit empty view; E's own later definition of the market used to make E primary only then,
        erasing the other file's earlier view."""
        tail = heartbeat(ms(OFF + timedelta(minutes=10)))
        base = [self.opening(), self.opening(self.OTHER), tail]
        other_file = [self.opening(self.OTHER), tail]
        appended = [self.results(), md_change(self.MID, OFF + timedelta(minutes=20),
                                              definition(iso(OFF + timedelta(minutes=30)), [runner(1), runner(2)])),
                    mcm(OFF + timedelta(minutes=20), [{"id": self.MID, "img": True}]), self.results(self.OTHER)]
        tars = [[(path(EVENT), stream(base)), (path(self.OTHER), stream(other_file))]]
        tars += [[(path(EVENT), stream(base + [message])), (path(self.OTHER), stream(other_file))]
                 for message in appended]
        tars += [[(path(EVENT), bz2.compress(lines(base) + b"not json\n")), (path(self.OTHER), stream(other_file))],
                 [(path(EVENT), stream(base)), (path(self.OTHER), stream(other_file + [self.results()]))],
                 [(path(EVENT), stream(base))]]
        records, _, _ = self.build_all(tars)
        view = records[0]["decision_time"]
        self.assertEqual((view["first_published_market_time"]["pt"], [r["id"] for r in view["runners"]]),
                         (ms(OFF - timedelta(minutes=10)), [1, 2]))
        for n, record in enumerate(records):
            with self.subTest(variant=n):
                self.assertEqual((record["provenance"]["member_path"], record["decision_time"]), (path(EVENT), view))
        # Held before the off only by the file named after OTHER; E exists in that directory and never mentions it.
        held = [self.opening(self.OTHER), self.opening(), tail]
        event_only_other = [self.opening(self.OTHER), tail]
        records, _, _ = self.build_all([[(path(EVENT), stream(event_file)), (path(self.OTHER), stream(held))]
                                        for event_file in (event_only_other,
                                                           event_only_other + [{**self.opening(), "pt": ms(
                                                               OFF + timedelta(minutes=20))}])])
        for record in records:
            self.assertEqual((record["provenance"]["member_path"], record["decision_time"]), (path(EVENT), NO_VIEW))
            self.assertEqual([(s["member_path"], s["agrees_decision_time"])
                              for s in record["provenance"]["other_sources"]], [(path(self.OTHER), False)])

    def test_a_refused_primary_refuses_its_market_instead_of_falling_back(self):
        """M and E both publish the market 10 minutes before the off, but only M revises its marketTime after the off
        into a sealed month (2026-02). M is refused by its content at ingest, and the market used to keep E's record
        (the event file standing in for a refused market file, so a month only M showed could not refuse it). M
        still exists, so it is the primary: the market has no record, and E's record of it is listed as refused
        because its primary was. The same with the revision hidden from the ingest scan by an escaped key, refused
        here by marketTime instead. A market only E holds is indexed as before."""
        sealed = datetime(2026, 2, 1, 14, tzinfo=timezone.utc)
        revision = json.dumps(md_change(self.MID, OFF + timedelta(minutes=20),
                                        definition(iso(sealed), [runner(1), runner(2)]))).encode() + b"\n"
        third = "1.100000003"
        event_file = stream(sorted([self.opening(), self.opening(third), heartbeat(ms(OFF + timedelta(minutes=10)))],
                                   key=lambda m: m["pt"]))
        for label, tail, refused in (
                ("content scan", revision, [(path(EVENT), "primary_source_refused")]),
                ("index", revision.replace(b'"marketTime"', b'"market\\u0054ime"'),
                 [(path(EVENT), "primary_source_refused"), (path(self.MID), "market_time_month_sealed_holdout")])):
            with self.subTest(refused_by=label):
                root = self.new_root()
                result = ingest_archive(self.tar([(path(self.MID), bz2.compress(lines([self.opening()]) + tail)),
                                                  (path(EVENT), event_file)], f"refused-{label[0]}.tar"), root)
                self.assertEqual(result["refused_by_reason"],
                                 {"content_month_sealed_holdout": 1} if label == "content scan" else {})
                self.assertEqual(index_archives(root)["status"], "indexed")
                sha = result["archive_sha256"]
                records = self.records(sha, root)
                self.assertEqual(sorted(records), [third])
                self.assertEqual(records[third]["provenance"]["member_path"], path(EVENT))
                self.assertEqual([(item["market_id"], item["member_path"], item["reason"])
                                  for item in self.head(sha, root)["refused_markets"]],
                                 sorted((self.MID, place, reason) for place, reason in refused))
                self.assertEqual(audit(root)["status"], "audited")

    def test_other_sources_are_ranked_and_event_files_taken_by_path(self):
        """MID has two files named after it (events 27000001 and 27000009): the first by path holds it and is
        primary; the second holds only heartbeats and is listed with nothing to compare. Its other sources are ranked
        by name (that file), then event file, then a file named after another market, whatever their paths. A market
        no file is named after, held by the event files of both events, has the first of them by path."""
        third, second_event = "1.100000003", "27000009"
        beats = [heartbeat(ms(OFF - timedelta(minutes=10))), heartbeat(ms(OFF + timedelta(minutes=10)))]
        sha, _ = self.build([
            (path(self.MID), stream([self.opening()])),
            (path(self.MID, event=second_event), stream(beats)),
            (path(self.OTHER), stream([self.opening(), self.opening(self.OTHER)])),
            (path(EVENT), stream([self.opening(), self.opening(third)])),
            (path(second_event, event=second_event), stream([self.opening(third)]))])
        records = self.records(sha)
        self.assertEqual(records[self.MID]["provenance"]["member_path"], path(self.MID))
        self.assertEqual([(s["member_path"], s["agrees"], s["agrees_decision_time"])
                          for s in records[self.MID]["provenance"]["other_sources"]],
                         [(path(self.MID, event=second_event), None, None), (path(EVENT), True, True),
                          (path(self.OTHER), True, True)])
        self.assertEqual((records[third]["provenance"]["member_path"],
                          [s["member_path"] for s in records[third]["provenance"]["other_sources"]]),
                         (path(EVENT), [path(second_event, event=second_event)]))

    def test_market_ids_differing_in_leading_zeros_are_two_markets(self):
        """They sort together but are two ids, so each has its own primary: the file named after 1.100000001, and the
        event file for 1.0100000001 (no file is named after it). They used to share one record."""
        zero = "1.0100000001"
        sha, _ = self.build([(path(self.MID), stream([self.opening()])),
                             (path(EVENT), stream([self.opening(), self.opening(zero)]))])
        records = self.records(sha)
        self.assertEqual({mid: record["provenance"]["member_path"] for mid, record in records.items()},
                         {self.MID: path(self.MID), zero: path(EVENT)})

    def assert_held_view_survives(self, pairs, holder):
        """pairs: {label: (base TAR, changed TAR)}, each change made only after the off. In both TARs the primary is
        holder with the view it published 10 minutes before the off (runners 1 and 2)."""
        records, _, _ = self.build_all([tar for pair in pairs.values() for tar in pair])
        for n, label in enumerate(pairs):
            before, after = records[2 * n:2 * n + 2]
            with self.subTest(case=label):
                for record in (before, after):
                    self.assertEqual(record["provenance"]["member_path"], holder[label])
                    view = record["decision_time"]
                    self.assertEqual((view["first_published_market_time"]["pt"], [r["id"] for r in view["runners"]]),
                                     (ms(OFF - timedelta(minutes=10)), [1, 2]))
                self.assertEqual(after["decision_time"], before["decision_time"])

    def test_with_no_named_market_file_the_earliest_mention_picks_the_source(self):
        """R1 after the verification of Astra's second review. With no file named after the market, the primary was
        the first event file of any directory where the market was seen, else the first member that saw it, however
        late: a mention published after the off, or the loss of one, moved the source and erased or made a view. Now
        it is the member with the earliest mention (least pt, then path), or its directory's event file. The market
        is held 10 minutes before the off by one member and each pair changes another only after the off:
        A1, an event-file-only TAR (the layout of the three sample TARs): E0 (event 27000001, first by path, holding
        another market) appends the market's definition 30 minutes after the off, while E1 (27000002) holds it;
        A3, no event file: the file named after 1.100000000 (first by path) appends it, while the file named after
        1.100000002 holds it; A5, a cut: E0 mentions it 15 minutes after the off and is cut after its first line
        published after the off (a heartbeat), which removes the mention. The same shapes as the verifier's probe."""
        tail = [heartbeat(ms(OFF + timedelta(minutes=10))), heartbeat(ms(OFF + timedelta(minutes=20)))]
        third, low = "1.100000003", "1.100000000"
        e0, e1 = path(EVENT), path("27000002", event="27000002")
        late = {**self.opening(), "pt": ms(OFF + timedelta(minutes=30))}
        e0_lines = [self.opening(third)] + tail
        e1_file = stream([self.opening(), self.opening("1.100000005")] + tail)
        mentions = [self.opening(third), tail[0], {**self.opening(), "pt": ms(OFF + timedelta(minutes=15))}, tail[1]]
        holder_lines = [self.opening(self.OTHER), self.opening()] + tail
        pairs = {
            "A1": ([(e0, stream(e0_lines)), (e1, e1_file)], [(e0, stream(e0_lines + [late])), (e1, e1_file)]),
            "A3": ([(path(low), stream([self.opening(low)] + tail)), (path(self.OTHER), stream(holder_lines))],
                   [(path(low), stream([self.opening(low)] + tail + [late])),
                    (path(self.OTHER), stream(holder_lines))]),
            "A5": ([(e0, stream(mentions)), (e1, e1_file)], [(e0, stream(mentions[:2])), (e1, e1_file)]),
        }
        self.assert_held_view_survives(pairs, {"A1": e1, "A3": path(self.OTHER), "A5": e1})

    def test_a_late_mention_in_another_directory_never_makes_its_event_file_primary(self):
        """A4, the case that tells the three rules apart. E1 (event 27000002) holds the market 10 minutes before the
        off; E0 (27000001) exists and never mentions it; a file named after another market in E0's directory
        appends the market's definition 30 minutes after the off. 0db64db kept E1 (the first holder); the rule that
        replaced it (an event file of any directory where the market was seen) made E0 primary with the empty view
        although neither event file changed; the earliest mention keeps E1."""
        tail = [heartbeat(ms(OFF + timedelta(minutes=10))), heartbeat(ms(OFF + timedelta(minutes=20)))]
        e0, e1 = path(EVENT), path("27000002", event="27000002")
        e0_file = stream([self.opening("1.100000003")] + tail)
        e1_file = stream([self.opening(), self.opening("1.100000005")] + tail)
        misfiled = [self.opening(self.OTHER)] + tail
        late = {**self.opening(), "pt": ms(OFF + timedelta(minutes=30))}
        pairs = {"A4": ([(e0, e0_file), (path(self.OTHER), stream(misfiled)), (e1, e1_file)],
                        [(e0, e0_file), (path(self.OTHER), stream(misfiled + [late])), (e1, e1_file)])}
        self.assert_held_view_survives(pairs, {"A4": e1})

    def test_the_first_named_file_by_path_stays_primary_whatever_follows(self):
        """Two files named after the market (events 27000001 and 27000009). The first by path is primary whether it
        holds the market before the off or not, and whatever it publishes or loses after the off: (a) heartbeats
        only, then the same with the market's first definition appended after the off; (b) holding the market before
        the off, then the same with a line it cannot read after the off. The second file (holding the market before
        the off with runner 2 REMOVED) is never chosen, so neither holding the market (verifier mutant M21) nor being
        read whole (M11) picks among named files."""
        second = path(self.MID, event="27000009")
        tail = heartbeat(ms(OFF + timedelta(minutes=10)))
        other_named = stream([self.opening(second="REMOVED"), tail])
        beats = [heartbeat(ms(OFF - timedelta(minutes=10))), tail]
        late_definition = {**self.opening(), "pt": ms(OFF + timedelta(minutes=20))}
        holder = [self.opening(), tail]
        variants = {"a-base": stream(beats), "a-append": stream(beats + [late_definition]),
                    "b-base": stream(holder), "b-cut": bz2.compress(lines(holder) + b"not json\n")}
        records, _, _ = self.build_all([[(path(self.MID), data), (second, other_named)] for data in variants.values()])
        got = dict(zip(variants, records))
        for label, record in got.items():
            with self.subTest(variant=label):
                self.assertEqual(record["provenance"]["member_path"], path(self.MID))
        self.assertEqual((got["a-base"]["decision_time"], got["a-append"]["decision_time"]), (NO_VIEW, NO_VIEW))
        self.assertEqual(got["b-cut"]["decision_time"], got["b-base"]["decision_time"])
        self.assertEqual([r["status"] for r in got["b-base"]["decision_time"]["runners"]],
                         [[[ms(OFF - timedelta(minutes=10)), "ACTIVE"]]] * 2)

    def test_a_market_whose_only_records_are_gate_refused_gets_no_record(self):
        """The file named after the market holds only heartbeats; the event file holds the market 10 minutes before
        the off, then revises it into a sealed month (2026-02) 20 minutes after the off, the key escaped so the
        ingest scan misses it. The event file's record is refused by marketTime, so the market has no record at all,
        not even the empty primary's no_view one (that would put a sealed-holdout market into the index; verifier
        mutant M15): a record needs a member that saw the market and a record of it that passed the gate."""
        root = self.new_root()
        sealed = datetime(2026, 2, 1, 14, tzinfo=timezone.utc)
        revision = json.dumps(md_change(self.MID, OFF + timedelta(minutes=20),
                                        definition(iso(sealed), [runner(1), runner(2)]))).encode() + b"\n"
        beats = [heartbeat(ms(OFF - timedelta(minutes=10))), heartbeat(ms(OFF + timedelta(minutes=10)))]
        event_file = bz2.compress(lines([self.opening()]) + revision.replace(b'"marketTime"', b'"market\\u0054ime"'))
        result = ingest_archive(self.tar([(path(self.MID), stream(beats)), (path(EVENT), event_file)], "gate.tar"),
                                root)
        self.assertEqual((result["status"], result["refused_by_reason"]), ("ingested", {}), result)
        self.assertEqual(index_archives(root)["status"], "indexed")
        sha = result["archive_sha256"]
        self.assertEqual(self.records(sha, root), {})
        self.assertEqual([(item["market_id"], item["member_path"], item["reason"])
                          for item in self.head(sha, root)["refused_markets"]],
                         [(self.MID, path(EVENT), "market_time_month_sealed_holdout")])

    def test_an_earliest_mention_refused_by_the_gate_still_picks_the_source(self):
        """No file is named after the market and no event file is in its directory. The file named after OTHER
        mentions it first (10 minutes before the off); the file named after 1.100000000, first by path, 5 minutes
        later. Base: the earliest mention is primary with its view (not the first by path). The same TAR with OTHER's
        file revising the market's marketTime into a sealed month (2026-02) 20 minutes after the off, the key escaped
        so the ingest scan misses it: that record is refused by marketTime, but its mention still counts as the
        earliest, so the market is refused (primary_source_refused) rather than handed to the later mention's view.
        Ignoring refused records when taking the earliest mention would let a message published after the off move
        the source, and put a market one source places in the sealed holdout into the index."""
        low = "1.100000000"
        sealed = datetime(2026, 2, 1, 14, tzinfo=timezone.utc)
        revision = json.dumps(md_change(self.MID, OFF + timedelta(minutes=20),
                                        definition(iso(sealed), [runner(1), runner(2)]))).encode() + b"\n"
        first = lines([self.opening(self.OTHER), self.opening(), heartbeat(ms(OFF + timedelta(minutes=10)))])
        later = stream([self.opening(low), {**self.opening(), "pt": ms(OFF - timedelta(minutes=5))},
                        heartbeat(ms(OFF + timedelta(minutes=10)))])
        root = self.new_root()
        shas = []
        for n, data in enumerate((first, first + revision.replace(b'"marketTime"', b'"market\\u0054ime"'))):
            result = ingest_archive(self.tar([(path(low), later), (path(self.OTHER), bz2.compress(data))],
                                             f"earliest-gate-{n}.tar"), root)
            self.assertEqual((result["status"], result["refused_by_reason"]), ("ingested", {}), result)
            shas.append(result["archive_sha256"])
        self.assertEqual(index_archives(root)["status"], "indexed")
        base, revised = (self.records(sha, root) for sha in shas)
        self.assertEqual(base[self.MID]["provenance"]["member_path"], path(self.OTHER))
        self.assertEqual(base[self.MID]["decision_time"]["first_published_market_time"]["pt"],
                         ms(OFF - timedelta(minutes=10)))
        self.assertEqual(sorted(revised), [low, self.OTHER])
        self.assertEqual([(item["market_id"], item["member_path"], item["reason"])
                          for item in self.head(shas[1], root)["refused_markets"]],
                         [(self.MID, path(low), "primary_source_refused"),
                          (self.MID, path(self.OTHER), "market_time_month_sealed_holdout")])

    def test_an_event_file_refused_at_ingest_is_still_the_primary(self):
        """No file is named after the market; it is held before the off only by the file named after OTHER, in a
        directory whose event file is refused at ingest by its content (another market revised into a sealed month
        after the off). That event file exists, so it is the market's primary: no record, and OTHER's record of it is
        listed as primary_source_refused. Taking event files only from kept members (verifier mutant M08) would let
        that content, published after the off, hand the market to OTHER's view."""
        root = self.new_root()
        third = "1.100000003"
        sealed = datetime(2026, 2, 1, 14, tzinfo=timezone.utc)
        sealed_line = json.dumps(md_change(third, OFF + timedelta(minutes=20),
                                           definition(iso(sealed), [runner(1)]))).encode() + b"\n"
        event_file = bz2.compress(lines([self.opening(third)]) + sealed_line)
        held = stream([self.opening(self.OTHER), self.opening(), heartbeat(ms(OFF + timedelta(minutes=10)))])
        result = ingest_archive(self.tar([(path(EVENT), event_file), (path(self.OTHER), held)], "refused-e.tar"), root)
        self.assertEqual(result["refused_by_reason"], {"content_month_sealed_holdout": 1}, result)
        self.assertEqual(index_archives(root)["status"], "indexed")
        sha = result["archive_sha256"]
        self.assertEqual(sorted(self.records(sha, root)), [self.OTHER])
        self.assertEqual([(item["market_id"], item["member_path"], item["reason"])
                          for item in self.head(sha, root)["refused_markets"]],
                         [(self.MID, path(self.OTHER), "primary_source_refused")])

    def test_post_decision_appends_and_cuts_never_change_decision_time(self):
        """Property-style, fixed seed (from hashlib): seven base TARs, each with every member ending in lines published
        after the off. One event directory: A, Astra's M of heartbeats with E; B, M holding the market before the off
        with a disagreeing E; C, E holding it with a file named after another market in the same directory; D, the
        market held before the off only by that other file while E (existing) never mentions it. Two event
        directories, no file named after the market: E, event files only, the first by path (E0) mentioning the
        market only after the off while E1 holds it; F, E0 never mentioning it and a file named after another market
        in E0's directory, E1 holding it. No event file: G, two files named after other markets, the second by path
        holding it. Each variant replaces one member: for every member, the market's definition appended after the
        off, and a cut at the end of its first line published after the off; and 24 random variants per base, a
        random cut at or after that point, random appends after the off (post_off_lines), or both. decision_time and
        the primary never change. A cut inside that first line is the documented exception (a line whose pt cannot
        be read ends a view at the last pt applied) and is not drawn."""
        rng = random.Random(int.from_bytes(hashlib.sha256(b"backtest (b) R2 primary source").digest()[:8], "big"))
        t10, t20, t30, t40 = (ms(OFF + timedelta(minutes=m)) for m in (10, 20, 30, 40))
        holder = [self.opening(), heartbeat(t10), self.results(), heartbeat(t30)]
        disagreeing = [self.opening(second="REMOVED"), heartbeat(t10), self.results(), heartbeat(t30)]
        both = [self.opening(), self.opening(self.OTHER), heartbeat(t10), self.results(), heartbeat(t30)]
        other_only = [self.opening(self.OTHER), heartbeat(t10), self.results(self.OTHER), heartbeat(t30)]
        third, low = "1.100000003", "1.100000000"
        e1 = path("27000002", event="27000002")
        bases = {
            "A": {path(self.MID): [heartbeat(ms(OFF) - 600000), heartbeat(t10), heartbeat(t20)], path(EVENT): holder},
            "B": {path(self.MID): holder, path(EVENT): disagreeing},
            "C": {path(EVENT): both, path(self.OTHER): [self.opening(self.OTHER), heartbeat(t10), heartbeat(t20)]},
            "D": {path(EVENT): other_only, path(self.OTHER): [self.opening(self.OTHER), self.opening(), heartbeat(t10),
                                                              heartbeat(t20)]},
            "E": {path(EVENT): [self.opening(third), heartbeat(t10), {**self.opening(), "pt": t20}, heartbeat(t30)],
                  e1: holder},
            "F": {path(EVENT): [self.opening(third), heartbeat(t10), heartbeat(t20)],
                  path(self.OTHER): [self.opening(self.OTHER), heartbeat(t10), heartbeat(t20)], e1: holder},
            "G": {path(low): [self.opening(low), heartbeat(t10), heartbeat(t20)],
                  path(self.OTHER): [self.opening(self.OTHER), self.opening(), heartbeat(t10), self.results(),
                                     heartbeat(t30)]},
        }
        expected_primary = {"A": path(self.MID), "B": path(self.MID), "C": path(EVENT), "D": path(EVENT), "E": e1,
                            "F": e1, "G": path(self.OTHER)}
        late = lines([{**self.opening(), "pt": t40}])
        for name, members in bases.items():
            tars, labels = [[(place, stream(messages)) for place, messages in members.items()]], ["base"]

            def variant(place, data):
                return [(other, bz2.compress(data) if other == place else stream(messages))
                        for other, messages in members.items()]

            def first_cut(place):
                first_after = next(i for i, m in enumerate(members[place]) if m["pt"] >= ms(OFF))
                return len(lines(members[place][:first_after + 1]))

            for place in sorted(members):
                data = lines(members[place])
                tars += [variant(place, data + late), variant(place, data[:first_cut(place)])]
                labels += [f"append the market to {place}", f"cut {place} after its first line after the off"]
            for n in range(24):
                place = rng.choice(sorted(members))
                data = lines(members[place])
                safe = first_cut(place)
                operation = rng.choice(("cut", "append", "both"))
                cut = rng.randrange(safe, len(data) + 1) if operation != "append" else len(data)
                extra = post_off_lines(rng, self.MID, t30, rng.randrange(1, 5)) if operation != "cut" else b""
                tars.append(variant(place, data[:cut] + extra))
                labels.append(f"{n}: {operation} {place} at {cut}/{len(data)} + {extra[:300]!r}")
            records, _, _ = self.build_all(tars)
            base = records[0]
            self.assertEqual(base["provenance"]["member_path"], expected_primary[name])
            self.assertEqual(base["decision_time"] == NO_VIEW, name in ("A", "D"))
            for label, record in zip(labels, records):
                with self.subTest(base=name, variant=label):
                    self.assertEqual((record["provenance"]["member_path"], record["decision_time"]),
                                     (base["provenance"]["member_path"], base["decision_time"]))


class AuditTests(Workspace):
    def test_counts_fields_and_no_outcome_statistics(self):
        july = datetime(2015, 7, 1, 0, 30, tzinfo=timezone.utc)
        pro = full_market("1.100000009")
        pro.insert(4, mcm(OFF - timedelta(minutes=40), [{"id": "1.100000009", "con": True, "img": True,
                                                         "rc": [{"id": 1, "atb": [[2.5, 10]], "spn": 2.4}]}]))
        members = [
            (path("1.100000001"), stream(full_market())),
            (path("1.100000002"), stream(closed_market("1.100000002", ["WINNER", "WINNER", "LOSER"], [2.0, 3.0, 4.0]))),
            (path("1.100000003"), stream(closed_market("1.100000003", ["WINNER", "LOSER"], [None, 3.0]))),
            (path("1.100000004"), stream(closed_market("1.100000004", ["WINNER", "LOSER"], [2.0, 3.0],
                                                       countryCode="IE", marketType="PLACE"))),
            (path("1.100000005", datetime(2015, 6, 30, tzinfo=timezone.utc), event="27000005"),
             stream(closed_market("1.100000005", ["WINNER", "LOSER"], [2.0, 3.0], off=july))),
            (path("1.100000006"), bz2.compress(b"not json\n")),               # intact bz2, unreadable stream
            (path("1.100000009", plan="PRO"), stream(pro)),
        ]
        sha_basic = self.ingest(members)["archive_sha256"]
        second = [(path("1.100000001"), stream(full_market())),
                  (path("1.100000010", datetime(2026, 3, 1, tzinfo=timezone.utc), event="29000010"), b"sealed")]
        sha_second = self.ingest(second, name="second.tar")["archive_sha256"]
        self.assertEqual(audit(self.root)["archives_not_indexed"], sorted([sha_basic, sha_second]))
        self.assertEqual(index_archives(self.root)["status"], "indexed")
        report = audit(self.root)
        self.assertEqual(report["status"], "audited", report)
        groups = {(g["tier"], g["month"], g["country"], g["market_type"]): g for g in report["groups"]}
        self.assertEqual(sorted(groups), [("BASIC", "2015-06", "GB", "WIN"), ("BASIC", "2015-06", "IE", "PLACE"),
                                          ("BASIC", "2015-07", "GB", "WIN"), ("PRO", "2015-06", "GB", "WIN")])
        win = groups[("BASIC", "2015-06", "GB", "WIN")]
        self.assertEqual((win["markets"], win["dead_heats"], win["markets_missing_final_bsp"],
                          win["markets_missing_final_bsp_in_bsp_markets"], win["markets_with_removals"],
                          win["removals_before_cutoff"], win["removals_after_cutoff"], win["markets_never_closed"],
                          win["market_time_revised"], win["bsp_market_true"], win["month_mismatches"]),
                         (3, 1, 1, 1, 1, 1, 1, 0, 1, 3, 0))
        self.assertEqual((win["fields_present"]["rc.ltp"], win["fields_present"]["mc.marketDefinition"]), (1, 3))
        self.assertEqual(win["fields_absent"], ["mc.con", "mc.img", "mc.tv", "rc.atb", "rc.atl", "rc.batb", "rc.batl",
                                                "rc.spb", "rc.spf", "rc.spl", "rc.spn", "rc.trd", "rc.tv"])
        self.assertEqual(win["fields_absent_before_cutoff"],
                         ["mc.con", "mc.img", "mc.tv", "md.runners.bsp", "md.settledTime", "rc.atb", "rc.atl",
                          "rc.batb", "rc.batl", "rc.spb", "rc.spf", "rc.spl", "rc.spn", "rc.trd", "rc.tv"])
        self.assertEqual(win["fields_valued_before_cutoff"], {"rc.ltp": 1})
        july_group = groups[("BASIC", "2015-07", "GB", "WIN")]
        self.assertEqual((july_group["month_mismatches"], july_group["date_mismatches"],
                          july_group["event_id_mismatches"]), (1, 1, 1))
        pro_group = groups[("PRO", "2015-06", "GB", "WIN")]
        self.assertEqual({name: pro_group["fields_present_before_cutoff"].get(name)
                          for name in ("rc.atb", "rc.spn", "mc.img", "mc.con")},
                         {"rc.atb": 1, "rc.spn": 1, "mc.img": 1, "mc.con": 1})
        self.assertEqual(pro_group["fields_valued_before_cutoff"], {"rc.ltp": 1, "rc.spn": 1})
        self.assertNotIn("rc.atb", pro_group["fields_absent"])
        self.assertEqual(report["unreadable_files"], [{"archive_sha256": sha_basic, "path": path("1.100000006"),
                                                       "reason": "not_json", "complete_before_pt": None,
                                                       "markets": 0}])
        self.assertEqual(report["markets_in_more_than_one_archive"], 1)
        by_sha = {item["archive_sha256"]: item for item in report["archives"]}
        self.assertEqual(by_sha[sha_second]["refused_by_reason"], {"month_sealed_holdout": 1})
        self.assertTrue(by_sha[sha_second]["month_gate_current"])
        self.assertEqual(report["totals"]["markets"], 6)
        # Counts only: no float anywhere, and no BSP, price or winner value leaks into the report.
        self.assertTrue(all(isinstance(n, int) for n in numbers(report)))
        text = json.dumps(report)
        for leaked in ("2.6", "3.1", "WINNER", "LOSER", "win_rate", "roi"):
            self.assertNotIn(leaked, text)
        self.assertEqual(audit(self.root), report)

    def test_every_count_is_exercised(self):
        t = iso(OFF)
        july = datetime(2015, 7, 1, 0, 30, tzinfo=timezone.utc)
        no_bsp_flag = [md_change("1.200000006", OFF - timedelta(hours=1),
                                 drop(definition(t, [runner(1)]), "bspMarket")),
                       md_change("1.200000006", OFF + timedelta(minutes=5),
                                 drop(definition(t, [runner(1, "WINNER", bsp=2.0)], status="CLOSED"), "bspMarket"))]
        revised = closed_market("1.200000008", ["WINNER", "LOSER"], [2.0, 3.0])
        revised.append(md_change("1.200000008", OFF + timedelta(minutes=30),
                                 definition(t, [runner(1, "WINNER", bsp=2.1), runner(2, "LOSER", bsp=3.0)],
                                            status="CLOSED")))
        moved = [md_change("1.200000012", OFF - timedelta(hours=1), definition(t, [runner(1)])),
                 md_change("1.200000012", OFF - timedelta(minutes=30), definition(t, [runner(1)], countryCode="IE"))]
        venue = full_market("1.200000001")
        venue[0]["mc"][0]["marketDefinition"]["venue"] = "Other Park"
        never = [md_change("1.200000004", OFF - timedelta(hours=1), definition(t, [runner(1)]))]
        hidden = json.dumps(md_change("1.200000013", OFF, definition("2026-04-02T14:00:00.000Z", [runner(1)])))
        members = [
            (path("1.200000001"), stream(full_market("1.200000001"))), (path(EVENT), stream(venue)),
            (path("1.200000002"), stream(closed_market("1.200000002", ["WINNER", "WINNER", "LOSER"], [2.0] * 3))),
            (path("1.200000003"), stream(closed_market("1.200000003", ["WINNER", "LOSER"], [2.0] * 2,
                                                       numberOfWinners=0))),
            (path("1.200000004"), stream(never)),
            (path("1.200000005"), stream(closed_market("1.200000005", ["WINNER", "LOSER"], [None, None],
                                                       bspMarket=False))),
            (path("1.200000006"), stream(no_bsp_flag)),
            (path("1.200000007"), stream(closed_market("1.200000007", ["WINNER", "LOSER"], ["NaN", 3.0]))),
            (path("1.200000008"), stream(revised)),
            (path("1.200000009"), stream(closed_market("1.200000009", ["WINNER", "MYSTERY"], [2.0, 3.0]))),
            (path("1.200000010", datetime(2015, 6, 30, tzinfo=timezone.utc), event="27000005"),
             stream(closed_market("1.200000010", ["WINNER", "LOSER"], [2.0, 3.0], off=july))),
            (path("1.200000011"), bz2.compress(lines(full_market("1.200000011")) + b"bad line\n")),
            (path("1.200000012"), stream(moved)),
            (path("1.200000013"), bz2.compress(hidden.replace('"marketTime"', '"market\\u0054ime"').encode() + b"\n")),
            (path("1.200000014"), bz2.compress(lines(closed_market("1.200000014", ["WINNER", "LOSER"], [2.0, None])
                                                     + [heartbeat(ms(OFF + timedelta(minutes=10)))]) + b"bad\n")),
        ]
        sha, _ = self.build(members)
        report = audit(self.root)
        self.assertEqual(report["status"], "audited", report)
        self.assertEqual([name for name in coverage_audit.COUNTS if not report["totals"][name]], [])
        groups = {(g["tier"], g["month"], g["country"], g["market_type"]): g for g in report["groups"]}
        self.assertNotIn(("BASIC", "2015-06", "IE", "WIN"), groups)            # grouped by the first countryCode
        win = groups[("BASIC", "2015-06", "GB", "WIN")]
        expected = {"bsp_market_false": 1, "bsp_market_missing": 1, "closed_dead_heat_not_assessable": 1,
                    "dead_heats": 1, "markets_never_closed": 3, "markets_read_incomplete": 2,
                    "markets_never_closed_read_incomplete": 1, "markets_missing_final_bsp_read_incomplete": 1,
                    "markets_with_unusable_final_bsp": 1, "markets_with_unrecognised_runner_status": 1,
                    "results_revised_after_close": 1, "multiple_sources": 1, "sources_disagree": 1,
                    "sources_disagree_decision_time": 1}
        self.assertEqual({name: win[name] for name in expected}, expected)
        self.assertEqual([item["market_id"] for item in report["refused_markets"]], ["1.200000013"])
        self.assertEqual(report["unreadable_files"], [{"archive_sha256": sha, "path": path("1.200000011"),
                                                       "reason": "not_json", "markets": 1,
                                                       "complete_before_pt": ms(OFF + timedelta(minutes=20))},
                                                      {"archive_sha256": sha, "path": path("1.200000014"),
                                                       "reason": "not_json", "markets": 1,
                                                       "complete_before_pt": ms(OFF + timedelta(minutes=10))}])
        self.assertEqual(sha, report["archives"][0]["archive_sha256"])

    def test_read_incomplete_overlap_counts(self):
        """Astra's design A (2026-10-15): two overlap annotations beside the headline counts, never instead of
        them. markets_never_closed_read_incomplete = never CLOSED and read incomplete;
        markets_missing_final_bsp_read_incomplete = missing a final BSP and read incomplete, with no further
        condition (a market seen CLOSED with a BSP gap keeps it, and is annotated when its member fails later).
        The index drops the batch at the last pt applied before a failure whose pt is unknown, so each failure
        here follows a later message (a heartbeat or prices) that keeps what came before it."""
        t, bad, later = iso(OFF), b"bad line\n", OFF + timedelta(minutes=30)

        def failing(messages, after):          # a later heartbeat, then a line the index cannot read
            return bz2.compress(lines(messages + [heartbeat(ms(after))]) + bad)

        names = ("markets_never_closed", "markets_never_closed_read_incomplete", "markets_missing_final_bsp",
                 "markets_missing_final_bsp_read_incomplete", "markets_read_incomplete")

        def opening(mid, runners=2, off=OFF):
            return [md_change(mid, off - timedelta(hours=1), definition(iso(off), [runner(i + 1)
                                                                                 for i in range(runners)]))]

        event = [*opening("1.300000006"), *opening("1.300000007", off=later),
                 closed_market("1.300000006", ["WINNER", "LOSER"], [2.0, 3.0])[1]]
        settles = OFF + timedelta(minutes=10)
        controls = {
            "complete, never CLOSED": ([(path("1.300000001"), stream(opening("1.300000001")))], (1, 0, 1, 0, 0)),
            "read incomplete before CLOSED": (
                [(path("1.300000002"), failing(opening("1.300000002"), OFF - timedelta(minutes=30)))],
                (1, 1, 1, 1, 1)),
            "CLOSED with a BSP gap, then a later failure": (
                [(path("1.300000003"), failing(closed_market("1.300000003", ["WINNER", "LOSER"], [2.0, None]),
                                               settles))], (0, 0, 1, 1, 1)),
            "CLOSED with every BSP, then a later failure": (
                [(path("1.300000004"), failing(closed_market("1.300000004", ["WINNER", "LOSER"], [2.0, 3.0]),
                                               settles))], (0, 0, 0, 0, 1)),
            "a primary with no runner, read incomplete": (
                [(path("1.300000005"), failing([md_change("1.300000005", OFF - timedelta(hours=1),
                                                          definition(t, []))], OFF - timedelta(minutes=30)))],
                (1, 1, 0, 0, 1)),
            "an event file: an earlier closed market, a later unsettled one": (
                [(path(EVENT), failing(event, settles))], (1, 1, 1, 1, 2)),
            # An empty primary: a market file holding none of its market (the event file has it, settled), so the
            # record has no view and no runner; no missing BSP is made up for it.
            "an empty primary read whole": (
                [(path("1.300000008"), stream([heartbeat(ms(OFF - timedelta(hours=2)))])),
                 (path(EVENT), stream(closed_market("1.300000008", ["WINNER", "LOSER"], [2.0, 3.0])))],
                (1, 0, 0, 0, 0)),
            "an empty primary read incomplete": (
                [(path("1.300000009"), bz2.compress(lines([heartbeat(ms(OFF - timedelta(hours=2)))]) + bad)),
                 (path(EVENT), stream(closed_market("1.300000009", ["WINNER", "LOSER"], [2.0, 3.0])))],
                (1, 1, 0, 0, 1)),
            # The BSP subset follows its parent, whatever bspMarket says.
            "a BSP gap in a market that is not a BSP market, then a later failure": (
                [(path("1.300000010"), failing(closed_market("1.300000010", ["WINNER", "LOSER"], [2.0, None],
                                                             bspMarket=False), settles))], (0, 0, 1, 1, 1)),
            "a BSP gap in a market without bspMarket, then a later failure": (
                [(path("1.300000011"), failing([md_change("1.300000011", OFF - timedelta(hours=1),
                                                          drop(definition(t, [runner(1), runner(2)]), "bspMarket")),
                                                md_change("1.300000011", OFF + timedelta(minutes=5),
                                                          drop(definition(t, [runner(1, "WINNER", bsp=2.0),
                                                                              runner(2, "LOSER")], status="CLOSED"),
                                                               "bspMarket"))], settles))], (0, 0, 1, 1, 1)),
        }
        for name, (members, expected) in controls.items():
            with self.subTest(control=name):
                root = self.new_root()
                self.build(members, root=root)
                report = audit(root)
                self.assertEqual(report["status"], "audited", report)
                self.assertEqual(tuple(report["totals"][count] for count in names), expected)
                if "not a BSP market" in name or "without bspMarket" in name:
                    self.assertEqual(report["totals"]["markets_missing_final_bsp_in_bsp_markets"], 0)
                for group in report["groups"] + [report["totals"]]:
                    for subset, parent in (("markets_never_closed_read_incomplete", "markets_never_closed"),
                                           ("markets_missing_final_bsp_read_incomplete",
                                            "markets_missing_final_bsp")):
                        self.assertLessEqual(group[subset], group[parent])
                        self.assertLessEqual(group[subset], group["markets_read_incomplete"])

    def test_index_collapses_repeated_messages_before_archive_overlap_audit(self):
        """The index emits one market record for repeated messages in a member."""
        mid = "1.310000001"
        messages = closed_market(mid, ["WINNER", "LOSER"], [2.0, 3.0])
        repeated = [message for message in messages for _ in range(3)]
        sha, _ = self.build([(path(mid), stream(repeated))])
        report = audit(self.root)
        self.assertEqual(report["status"], "audited", report)
        self.assertEqual(self.head(sha)["markets"], 1)
        self.assertEqual((report["totals"]["markets"], report["markets_in_more_than_one_archive"]), (1, 0))

    def test_a_market_repeated_in_many_archives_counts_once(self):
        def closed(mid, plan="BASIC"):
            return path(mid, plan=plan), stream(closed_market(mid, ["WINNER", "LOSER"], [2.0, 3.0]))

        cases = {"one market in three archives": ([["A"], ["A"], ["A"]], 1),
                 "two markets, each in three archives": ([["A", "B"], ["A", "B"], ["A", "B"]], 2),
                 "one in three and one in two": ([["A", "B"], ["A", "B"], ["A"]], 2),
                 "one in two": ([["A"], ["A"], []], 1),
                 "the same id only across tiers is not a repeat": ([["A"], ["PRO:A"], []], 0),
                 "the same id in another tier is another market": ([["A"], ["A", "PRO:A"], []], 1),
                 "the same id repeated in two tiers is two markets": ([["A", "PRO:A"], ["A", "PRO:A"], []], 2)}
        ids = {"A": "1.310000001", "B": "1.310000002"}
        for name, (archives, expected) in cases.items():
            with self.subTest(case=name):
                root = self.new_root()
                for n, held in enumerate(archives):
                    members = [closed(f"1.31000010{n}")]          # each TAR also holds a market of its own
                    for item in held:
                        plan, _, key = item.rpartition(":")
                        members.append(closed(ids[key], plan or "BASIC"))
                    self.assertEqual(self.ingest(members, name=f"repeat{self.roots}-{n}.tar", root=root)["status"],
                                     "ingested")
                self.assertEqual(index_archives(root)["status"], "indexed")
                report = audit(root)
                self.assertEqual(report["markets_in_more_than_one_archive"], expected, report)
                copies = sum(item["markets"] for item in report["archives"]) - report["totals"]["markets"]
                self.assertEqual(copies, sum(len(held) for held in archives)
                                 - len({item for held in archives for item in held}))

    def test_a_repeated_market_is_counted_from_the_first_archive_by_sha256(self):
        """Repeats are counted once, from the copy in the archive whose sha256 sorts first, whichever is ingested
        first: here one copy saw CLOSED and the other did not."""
        mid = "1.320000001"
        copies = {"closed": closed_market(mid, ["WINNER", "LOSER"], [2.0, 3.0]),
                  "never closed": closed_market(mid, ["WINNER", "LOSER"], [2.0, 3.0])[:1]}
        root, shas = self.new_root(), {}
        for n, (name, messages) in enumerate(copies.items()):
            members = [(path(mid), stream(messages)),
                       (path(f"1.32000001{n}"),
                        stream(closed_market(f"1.32000001{n}", ["WINNER", "LOSER"], [2.0, 3.0])))]
            shas[name] = self.ingest(members, name=f"first-{n}.tar", root=root)["archive_sha256"]
        self.assertEqual(index_archives(root)["status"], "indexed")
        report = audit(root)
        first = min(shas, key=shas.get)
        self.assertEqual((report["markets_in_more_than_one_archive"], report["totals"]["markets"],
                          report["totals"]["markets_never_closed"]), (1, 3, 1 if first == "never closed" else 0))

    def test_empty_root(self):
        self.assertEqual(audit(self.root)["status"], "empty")
        self.assertEqual(index_archives(self.root), {"status": "empty", "archives": []})


class CliTests(Workspace):
    def run_main(self, argv):
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(argv)
        text = out.getvalue()
        result = json.loads(text)
        self.assertEqual(text, json.dumps(result, sort_keys=True) + "\n")     # one line, keys sorted
        return code, result

    def test_exit_codes(self):
        good = self.tar([(path("1.100000001"), stream(full_market()))])
        code, result = self.run_main(["archive-ingest", "--tar", str(good), "--data-root", str(self.root)])
        self.assertEqual((code, result["status"]), (0, "ingested"))
        code, again = self.run_main(["archive-ingest", "--tar", str(good), "--data-root", str(self.root)])
        self.assertEqual((code, again["status"]), (0, "already_ingested"))
        code, indexed = self.run_main(["archive-index", "--data-root", str(self.root)])
        self.assertEqual((code, indexed["status"]), (0, "indexed"))
        code, one = self.run_main(["archive-index", "--data-root", str(self.root),
                                   "--archive", result["archive_sha256"]])
        self.assertEqual((code, one["archives"][0]["state"]), (0, "unchanged"))
        code, report = self.run_main(["archive-audit", "--data-root", str(self.root)])
        self.assertEqual((code, report["status"], report["totals"]["markets"]), (0, "audited", 1))
        bad = self.tar([("/" + path("1.100000001"), stream(full_market()))], name="bad.tar")
        code, refused = self.run_main(["archive-ingest", "--tar", str(bad), "--data-root", str(self.root)])
        self.assertEqual((code, refused["status"]), (2, "refused"))
        for argv in (["archive-ingest", "--tar", str(good), "--data-root", str(REPO / "no-such-root")],
                     ["archive-index", "--data-root", str(self.tmp / "absent")],
                     ["archive-index", "--data-root", str(self.root), "--archive", "abc"],
                     ["archive-audit", "--data-root", str(REPO)]):
            with self.subTest(argv=argv):
                code, refused = self.run_main(argv)
                self.assertEqual((code, refused["status"]), (2, "refused"))
        self.assertFalse((REPO / "no-such-root").exists())
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            main(["archive-ingest", "--tar", str(good)])
        self.assertEqual(raised.exception.code, 2)
        contract_path = REPO / "research" / "experiments" / "backtest-p0-bsp-v1.json"
        repo_status = json.loads(contract_path.read_bytes())["status"]
        self.assertIn(repo_status, ("draft", "frozen"))
        code, contract = self.run_main(["validate-contract", "--contract", str(contract_path)])
        self.assertEqual((code, contract["status"], contract["problems"]), (0, f"valid_{repo_status}", []))


class TarfileBoundaryTests(unittest.TestCase):
    """tarfile is on the package-wide backtest allowlist (tests/test_betfair_backtest_boundaries.py); this pins that
    only archive.py imports it, and only as "r|" read streams with no extraction or creation API."""

    FORBIDDEN_ATTRIBUTES = {"TarFile", "addfile", "bz2open", "chmod", "chown", "data_filter", "extract", "extractall",
                            "extraction_filter", "fully_trusted_filter", "gettarinfo", "gzopen", "makedev", "makedir",
                            "makefifo", "makefile", "makelink", "makelink_with_filter", "tar_filter", "taropen",
                            "utime", "xzopen", "zstopen"}

    def test_only_archive_imports_tarfile(self):
        importers = set()
        for source in sorted((REPO / "betangel_research").rglob("*.py")):
            for node in ast.walk(ast.parse(source.read_bytes())):
                names = ([alias.name for alias in node.names] if isinstance(node, ast.Import)
                         else [node.module or ""] if isinstance(node, ast.ImportFrom) and node.level == 0 else [])
                if any(name.split(".")[0] == "tarfile" for name in names):
                    importers.add(source.relative_to(REPO).as_posix())
        self.assertEqual(importers, {"betangel_research/betfair/backtest/archive.py"})

    def test_archive_only_opens_read_streams(self):
        tree = ast.parse((REPO / "betangel_research/betfair/backtest/archive.py").read_bytes())
        opens, calls = [], {id(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
        for node in ast.walk(tree):
            self.assertFalse(isinstance(node, ast.ImportFrom) and node.module == "tarfile", "use import tarfile")
            if isinstance(node, ast.Attribute):
                self.assertNotIn(node.attr, self.FORBIDDEN_ATTRIBUTES, ast.unparse(node))
                if ast.unparse(node) == "tarfile.open":
                    self.assertIn(id(node), calls, "tarfile.open is only ever called directly")
            if isinstance(node, ast.Call) and ast.unparse(node.func) == "tarfile.open":
                modes = [kw.value for kw in node.keywords if kw.arg == "mode"]
                self.assertEqual(node.args, [])
                self.assertEqual([ast.literal_eval(mode) for mode in modes], ["r|"])
                opens.append(node)
        self.assertEqual(len(opens), 1)


if __name__ == "__main__":
    unittest.main()
