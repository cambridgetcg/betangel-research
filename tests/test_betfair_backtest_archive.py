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
    """members: (name, bytes) for a regular file, or a TarInfo (no data) for any other type."""
    with tarfile.open(target, "w", format=fmt, **options) as tar:
        for member in members:
            if isinstance(member, tarfile.TarInfo):
                tar.addfile(member, io.BytesIO(b"\0" * member.size) if member.size else None)
                continue
            name, data = member
            info = tarfile.TarInfo(name)
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
        late = [md_change("1.100000001", OFF + timedelta(minutes=1), definition(iso(OFF), [runner(1), runner(2)]))]
        sha, _ = self.build([(path("1.100000001"), stream(late))])
        decision = self.records(sha)["1.100000001"]["decision_time"]
        self.assertEqual(decision, {"definition": {}, "first_published_market_time": None, "removals": [],
                                    "runners": [], "view_ends": {"pt": ms(OFF),
                                                                 "reason": "first_published_scheduled_off"}})

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
        venue = full_market()
        venue[0]["mc"][0]["marketDefinition"]["venue"] = "Other Park"          # differs before the cutoff
        late = full_market()[:-1]                                            # differs only after it
        held = full_market("1.100000003")                                    # named after 1.100000002
        event = sorted(venue + full_market("1.100000003"), key=lambda m: m["pt"])
        sha, _ = self.build([(path("1.100000001"), stream(full_market())), (path(EVENT), stream(event)),
                             (path("1.100000002"), stream(held)),
                             (path("1.100000005"), stream(full_market("1.100000006"))),
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
        only = records[("BASIC", "1.100000006")]
        self.assertEqual((only["provenance"]["member_path"], only["checks"]["market_file_holds_other_market"]),
                         (path("1.100000005"), True))

    def test_a_source_read_whole_is_primary_over_a_partial_one(self):
        event = sorted(full_market() + full_market("1.100000002"), key=lambda m: m["pt"])
        sha, _ = self.build([(path("1.100000001"), bz2.compress(lines(full_market()) + b"bad\n")),
                             (path(EVENT), stream(event))])
        provenance = self.records(sha)["1.100000001"]["provenance"]
        self.assertEqual(provenance["member_path"], path(EVENT))
        self.assertEqual([(s["member_path"], s["read_complete"], s["agrees_decision_time"])
                          for s in provenance["other_sources"]], [(path("1.100000001"), False, True)])

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
        ]
        sha, _ = self.build(members)
        report = audit(self.root)
        self.assertEqual(report["status"], "audited", report)
        self.assertEqual([name for name in coverage_audit.COUNTS if not report["totals"][name]], [])
        groups = {(g["tier"], g["month"], g["country"], g["market_type"]): g for g in report["groups"]}
        self.assertNotIn(("BASIC", "2015-06", "IE", "WIN"), groups)            # grouped by the first countryCode
        win = groups[("BASIC", "2015-06", "GB", "WIN")]
        expected = {"bsp_market_false": 1, "bsp_market_missing": 1, "closed_dead_heat_not_assessable": 1,
                    "dead_heats": 1, "markets_never_closed": 3, "markets_read_incomplete": 1,
                    "markets_with_unusable_final_bsp": 1, "markets_with_unrecognised_runner_status": 1,
                    "results_revised_after_close": 1, "multiple_sources": 1, "sources_disagree": 1,
                    "sources_disagree_decision_time": 1}
        self.assertEqual({name: win[name] for name in expected}, expected)
        self.assertEqual([item["market_id"] for item in report["refused_markets"]], ["1.200000013"])
        self.assertEqual(report["unreadable_files"], [{"archive_sha256": sha, "path": path("1.200000011"),
                                                       "reason": "not_json", "markets": 1,
                                                       "complete_before_pt": ms(OFF + timedelta(minutes=20))}])
        self.assertEqual(sha, report["archives"][0]["archive_sha256"])

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
        code, contract = self.run_main(["validate-contract", "--contract",
                                        str(REPO / "research" / "experiments" / "backtest-p0-bsp-v1.json")])
        self.assertEqual((code, contract["status"]), (0, "valid_draft"))


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
