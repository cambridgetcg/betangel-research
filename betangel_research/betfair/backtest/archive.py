"""Safe, bounded ingest of one Betfair Historical Data TAR into an explicit data root (manifest schema_version 1).

What a TAR is (docs/backtest-plan.md section 2.8): the portal FAQ says purchases download as ".tar files" holding
".json format" data, files by marketId and by eventId (an event file interleaves every market of the event in
published-time order), and the Betfair Data Scientists' tutorials open the TAR with tarfile.TarFile (no outer
compression) and bz2-decompress each member. No Betfair text available here gives the path layout inside the TAR;
the only path Betfair documents is the historic API's undated "<PLAN>/<eventId>/<marketId>.bz2". This module
accepts exactly MEMBER_LAYOUT, "<PLAN>/<YYYY>/<Mon>/<D>/<eventId>/<marketId or eventId>.bz2", a layout reported by
users of the portal and not verified here: a member under any other layout has no month this module will believe,
so it is refused (no_parseable_month), never guessed; the first real TAR's manifest shows which it is.

Safety, all before anything is written: the data root must be given explicitly, exist as a listable directory and
lie outside every Git worktree (a ".git" entry, even a dangling symlink, in it or any ancestor) and outside this
repository (compared by file identity, so a firmlink or different-case spelling of the repository is caught too)
(check_data_root). The archive is read twice as a stream (tarfile "r|": headers in order, no seeking, no extraction
API is ever called; tests/test_betfair_backtest_archive.py pins that). Pass 1 hashes the whole file, validates
every header and decompress-checks every member that would be kept; pass 2 copies the kept members and recomputes
every hash, so a file that changed between the passes is refused. Any of these refuses the whole archive (status
refused, every problem listed, nothing written): an absolute name, a ".." component, a name outside SAFE_COMPONENT
(ASCII letters, digits, ".", "_" and "-", starting with a letter or digit, so no Unicode normalisation or
case-folding surprise on macOS), a symlink, hardlink, device, FIFO or any non-regular member (contiguous and sparse
files included: every GNU.sparse pax keyword is refused before tarfile reads a sparse map, see _bounded_tarinfo), a
duplicate name, two names equal ignoring case, a file whose name is a directory of another, too many members, a
member or total size over the limit, a header whose declared size runs past the end of the file (checked before
tarfile is allowed to skip to the next header, so a tiny file declaring 2**60 bytes cannot spin), an extended
header (pax or GNU long name) over max_extended_header_bytes, more than MAX_EXTENDED_HEADERS of them chained before
one member, or any pax global header (checked by _bounded_tarinfo before tarfile reads them into memory), an
archive that does not end cleanly (the last member must be followed by nothing but zero bytes, at least two
512-byte zero blocks as POSIX writes them: a file cut or corrupted at or after a header, garbage after the members,
or two TARs concatenated would otherwise read as a complete archive with members missing), and a member that is
empty, not bz2, truncated, has trailing bytes after its last bz2 stream, decompresses beyond the size limit or
beyond max_ratio times its compressed size (a bz2 bomb; checked while streaming through BZ2Decompressor with
max_length, so at most CHUNK_BYTES of decompressed data is held and none is written), or takes the archive past
max_total_decompressed_bytes. That allowance is charged with the work spent on every member checked, those that
fail included (a decompress call that fails inside libbz2, a block CRC mismatch say, drops up to CHUNK_BYTES it
already produced, so it is charged a whole chunk), and once it is spent no further member is decompressed, so the
decompression work of one ingest stays within the allowance plus one CHUNK_BYTES chunk however many members fail.
Limits are LIMIT_CEILINGS, each justified there; Limits.stricter may lower a value, never raise one.

Month gate (contract.py's constants; every month here is the UTC calendar month): a member's month comes from its path
only. Members dated in SEALED_HOLDOUT, EXPOSED_MONTHS, the month of a NOT_PROSPECTIVE_DATES date, or any month from
REFUSED_FROM_MONTH on (prospective use needs a frozen contract and is out of scope) are refused, as are members
without a parseable month; they are hashed but never decompressed, written or read. As a second line, a kept member's
decompressed bytes are scanned for "marketTime" values (content_months, nothing else is parsed; the UTC month of the
value, or its literal YYYY-MM when it does not parse or the member ends inside it; a value cut by a decompression
chunk boundary is carried into the next chunk and read whole, unless its match starts too early to be carried in the
tail, when its literal month counts): a member whose content names a refused month is refused too
(content_month_<reason>, its content_months recorded), whatever its path says. This scan is a best effort over
unescaped bytes: a key or value written with JSON escapes passes it, and so can a key whose value follows it after
more whitespace than the carried tail holds when a chunk boundary falls before the value's month, so such a member's
raw bytes may be stored; market_index's parsed check is the authoritative gate (it refuses that market at index time).
INSPECTED_MONTHS are kept and flagged. Month-gate refusals are listed in the manifest with their reasons; they do not
refuse the archive, but an archive that keeps no member at all is refused (archive:nothing_kept, its counts still
reported) so no manifest records an empty ingest.

Layout under the data root: raw/<archive sha256>/<member path> (bz2 bytes exactly as in the TAR, never decompressed on
disk) and manifests/<archive sha256>.json (canonical JSON, see build_manifest). Each file is written to
"<name>.partial" opened with exclusive create and renamed into place after its hash is checked; a leftover .partial
(an interrupted or concurrent run; a dangling symlink counts) refuses the ingest until a person removes it, even when
the archive is already ingested: the manifest's and every raw member's are checked, and listed together, before any
raw member is written (a manifest that would change is refused first, with the manifest's partial but before raw
partials are looked at). There is no fsync (os is off the backtest allowlist), so a crash can leave a renamed file
incomplete: every later ingest and index re-hashes and refuses a mismatch rather than trusting it. Re-ingesting the
same archive is idempotent: the same manifest bytes, existing raw files verified, missing ones restored; a manifest
that would differ (another file name, other limits, a changed gate: gate_changed) or an existing raw file with another
hash is refused, never overwritten. Every file-system failure becomes a refusal with its place, never a traceback.

Trust assumption: the data root and its parents are writable only by the operator, and no two tools write it at
once. Paths are checked (safe_path) and then opened by name; without os (dir_fd, O_NOFOLLOW) another process could
swap a checked directory for a symlink in between. The target is checked again after each rename, which catches a
swap that stays in place but cannot undo a write that already landed elsewhere.

Standard library only. tarfile is on the package-wide backtest allowlist, and tests/test_betfair_backtest_archive.py
pins that only this module imports it and only opens "r|" read streams (its own pwd/grp imports serve extraction
and archive creation, which are never called here).
"""

from dataclasses import asdict, dataclass, fields, replace
from datetime import date, datetime, timedelta, timezone
import bz2
import hashlib
import json
from pathlib import Path
import re
import tarfile

from ...temporal import timestamp_ms
from .contract import EXPOSED_MONTHS, INSPECTED_MONTHS, NOT_PROSPECTIVE_DATES, SEALED_HOLDOUT, canonical_json

MANIFEST_SCHEMA_VERSION = 1
MANIFEST_KIND = "betfair_historical_archive_manifest"
MEMBER_LAYOUT = "<PLAN>/<YYYY>/<Mon>/<D>/<eventId>/<marketId or eventId>.bz2"
PLANS = ("BASIC", "ADVANCED", "PRO")
MONTH_NAMES = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
CHUNK_BYTES = 1 << 20
MAX_COMPONENTS = 8
# A GNU long name plus a long link plus a pax header is three; tarfile recurses once per chained header, so a long
# chain (5,000 headers in a 5 MB file) would otherwise end in RecursionError.
MAX_EXTENDED_HEADERS = 4
# POSIX ends an archive with two 512-byte zero blocks; Python's tarfile, bsdtar, GNU tar and Go all write them.
END_OF_ARCHIVE_BYTES = 1024
# Every month from the sealed holdout's first month on is sealed, exposed, not prospective or prospective.
REFUSED_FROM_MONTH = SEALED_HOLDOUT[0]
NOT_PROSPECTIVE_MONTHS = tuple(sorted({day[:7] for day in NOT_PROSPECTIVE_DATES}))

# Ceilings, each with its reason; a caller may only go lower (Limits.stricter). Measured references: the 2017
# public samples (one meeting each, docs/data-audit.md) are BASIC 79,576 -> 1,609,190 bytes (ratio 20.2),
# ADVANCED 8,487,377 -> 84,791,246 (10.0) and PRO 16,812,379 -> 206,028,882 (12.3); the portal lists 25,719 files
# per tier for all-country horse racing in 2026-09 and PRO at about 6.5 GB for that month (docs section 2.1).
LIMIT_CEILINGS = {
    # A month of all-country PRO is about 6.5 GB; the lab disk is about 256 GB and holds the TAR and its raw copy.
    "max_archive_bytes": 64 << 30,
    # Twelve months of 25,719 files plus their directory entries; tarfile keeps one header object per member.
    "max_members": 400_000,
    # About 60 times the PRO sample, which is a whole meeting's event file.
    "max_member_bytes": 1 << 30,
    # The members can never exceed the archive; stated separately so the check is explicit.
    "max_total_bytes": 64 << 30,
    # About 40 times the PRO sample decompressed; the index reads members line by line within this.
    "max_member_decompressed_bytes": 8 << 30,
    # Bounds the decompress-check's CPU time for one archive (hours at bz2 speed, not days).
    "max_total_decompressed_bytes": 1 << 40,
    # Ten times the highest ratio measured (20.2); a bz2 bomb reaches 10**5 and more (20 MiB of zeros -> 49 bytes).
    "max_ratio": 200,
    # Real member paths are about 45 characters (PRO/2017/Apr/29/28209874/1.130870137.bz2).
    "max_path_chars": 200,
    # One stream line: the longest line in the three samples is 59,418 bytes; a PRO image line stays far below this.
    "max_line_bytes": 16 << 20,
    # tarfile reads a pax header or GNU long name whole before any check runs; names here are at most
    # max_path_chars, so a legitimate one is a few hundred bytes.
    "max_extended_header_bytes": 64 << 10,
}
SAFE_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_LAYOUT_RE = re.compile("(" + "|".join(PLANS) + r")/([0-9]{4})/(" + "|".join(MONTH_NAMES) + r")/([0-9]{1,2})/"
                        r"([0-9]{1,15})/([A-Za-z0-9._-]+)\.bz2")
MARKET_ID_RE = re.compile(r"[0-9]{1,3}\.[0-9]{1,15}")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
# The value's literal YYYY-MM, then up to 40 more unescaped characters and the closing quote when they are there.
_MARKET_TIME_RE = re.compile(rb'"marketTime"\s*:\s*"(([0-9]{4})-(0[1-9]|1[0-2])[^"\\]{0,40})(")?')
_MARKET_TIME_TAIL = 128
_LITERAL_MONTH_RE = re.compile(r"[0-9]{4}-(0[1-9]|1[0-2])")
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
# The repository holding this file (betangel_research/betfair/backtest/archive.py): no data root may lie inside it.
REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class Limits:
    """Ingest and read bounds; every value is a positive int no higher than its LIMIT_CEILINGS entry."""

    max_archive_bytes: int
    max_members: int
    max_member_bytes: int
    max_total_bytes: int
    max_member_decompressed_bytes: int
    max_total_decompressed_bytes: int
    max_ratio: int
    max_path_chars: int
    max_line_bytes: int
    max_extended_header_bytes: int

    def __post_init__(self):
        for item in fields(self):
            value = getattr(self, item.name)
            if type(value) is not int or not 1 <= value <= LIMIT_CEILINGS[item.name]:
                raise ValueError(f"limit_not_allowed:{item.name}")

    def stricter(self, **changes):
        """A copy with some limits lowered; ValueError(limit_not_stricter:<name>) for a raise or a non-int."""
        names = {item.name for item in fields(self)}
        for name, value in changes.items():
            if name not in names:
                raise ValueError(f"unknown_limit:{name}")
            if type(value) is not int or not 1 <= value <= getattr(self, name):
                raise ValueError(f"limit_not_stricter:{name}")
        return replace(self, **changes)


DEFAULT_LIMITS = Limits(**LIMIT_CEILINGS)


class ArchiveProblem(Exception):
    """A member or path problem that refuses the whole operation; str() is the reason code."""


def utc_moment(value):
    """The UTC datetime of an ISO-8601 text with a zone (temporal.timestamp_ms), or None."""
    try:
        return _EPOCH + timedelta(milliseconds=timestamp_ms(value))
    except (ValueError, TypeError, OverflowError):
        return None


def utc_month(value):
    """YYYY-MM of the UTC calendar month of an ISO-8601 text with a zone, or None."""
    moment = utc_moment(value)
    return None if moment is None else f"{moment.year:04d}-{moment.month:02d}"


def market_month(value):
    """The month of a marketTime value: its UTC month, or its literal leading YYYY-MM when it does not parse (so a
    malformed value still meets the gate), else None."""
    month = utc_month(value)
    if month is None and isinstance(value, str) and _LITERAL_MONTH_RE.match(value):
        month = value[:7]
    return month


def month_gate(month):
    """None when a YYYY-MM month may be ingested, else the refusal reason (contract.py's constants)."""
    if SEALED_HOLDOUT[0] <= month <= SEALED_HOLDOUT[1]:
        return "month_sealed_holdout"
    if month in EXPOSED_MONTHS:
        return "month_exposed_exploratory_only"
    if month in NOT_PROSPECTIVE_MONTHS:
        return "month_not_prospective"
    if month >= REFUSED_FROM_MONTH:
        return "month_prospective_out_of_scope"
    return None


def gate_snapshot():
    """The month gate as recorded in every manifest, so a changed gate changes the manifest."""
    return {"exposed_months": sorted(EXPOSED_MONTHS), "inspected_months": list(INSPECTED_MONTHS),
            "not_prospective_months": list(NOT_PROSPECTIVE_MONTHS), "refused_from_month": REFUSED_FROM_MONTH,
            "sealed_holdout": list(SEALED_HOLDOUT)}


def name_problem(name, limits=DEFAULT_LIMITS):
    """None for a safe relative member name, else absolute_path, parent_component, name_too_long or unsafe_name."""
    if not isinstance(name, str) or not name:
        return "unsafe_name"
    if name.startswith(("/", "\\")) or re.match(r"[A-Za-z]:", name):
        return "absolute_path"
    if ".." in re.split(r"[/\\]", name):
        return "parent_component"
    parts = name.split("/")
    if len(name) > limits.max_path_chars or len(parts) > MAX_COMPONENTS:
        return "name_too_long"
    if not all(SAFE_COMPONENT.fullmatch(part) for part in parts):
        return "unsafe_name"
    return None


def parse_member_path(name):
    """({plan, month, date, event_id, file_type, market_id}, None) for a MEMBER_LAYOUT name, else (None, reason):
    no_parseable_month (another layout), invalid_date_in_path, or unrecognised_file_name (neither the market id
    of a market file nor the event id of an event file)."""
    match = _LAYOUT_RE.fullmatch(name)
    if not match:
        return None, "no_parseable_month"
    plan, year, month_name, day, event_id, stem = match.groups()
    try:
        when = date(int(year), MONTH_NAMES.index(month_name) + 1, int(day))
    except ValueError:
        return None, "invalid_date_in_path"
    if stem == event_id:
        file_type, market_id = "event", None
    elif MARKET_ID_RE.fullmatch(stem):
        file_type, market_id = "market", stem
    else:
        return None, "unrecognised_file_name"
    return {"plan": plan, "month": when.isoformat()[:7], "date": when.isoformat(), "event_id": event_id,
            "file_type": file_type, "market_id": market_id}, None


def holds_git(place):
    """True when place has a ".git" entry of any kind, a dangling symlink included."""
    entry = place / ".git"
    return entry.is_symlink() or entry.exists()


def check_data_root(data_root):
    """(resolved Path, problems): the root must be given, exist as a directory that can be listed, and lie outside
    this repository (by file identity) and every Git worktree (no ".git" entry in it or any ancestor). Nothing is
    created."""
    if data_root is None or not str(data_root).strip():
        return None, ["data_root:missing"]
    problems = []
    try:
        resolved = Path(data_root).resolve()
        places = (resolved, *resolved.parents)
        if any(place == REPOSITORY_ROOT or (place.exists() and place.samefile(REPOSITORY_ROOT)) for place in places):
            problems.append("data_root:inside_repository")
        if any(holds_git(place) for place in places):
            problems.append("data_root:inside_git_worktree")
        if not resolved.exists():
            problems.append("data_root:missing")
        elif not resolved.is_dir():
            problems.append("data_root:not_a_directory")
        else:
            next(resolved.iterdir(), None)
    except (OSError, RuntimeError):
        return None, ["data_root:unreadable"]
    return resolved, problems


def _label(name):
    """A member name for a problem code: printable ASCII, at most 120 characters."""
    text = name.encode("ascii", "backslashreplace").decode("ascii") if isinstance(name, str) else "<no name>"
    return text if len(text) <= 120 else text[:117] + "..."


def _member_kind(info):
    """file, directory, or the archive-fatal reason for any other member type."""
    if info.issym():
        return "symlink"
    if info.islnk():
        return "hardlink"
    if info.ischr() or info.isblk():
        return "device"
    if info.isfifo():
        return "fifo"
    if info.isdir():
        return "directory"
    if info.type in (tarfile.REGTYPE, tarfile.AREGTYPE) and not info.issparse():
        return "file"
    return "not_regular"


_EXTENDED_TYPES = (tarfile.XHDTYPE, tarfile.SOLARIS_XHDTYPE, tarfile.GNUTYPE_LONGNAME, tarfile.GNUTYPE_LONGLINK)


def _bounded_tarinfo(limits):
    """A TarInfo class whose _proc_member (tarfile's documented subclass hook, run on every header before tarfile
    reads what follows it) refuses, as ArchiveProblem: a pax or GNU long-name header over max_extended_header_bytes
    (tarfile would read it whole into memory), more than MAX_EXTENDED_HEADERS chained before one member (tarfile
    recurses per header), any pax global header (it would silently change how every later member is read) and an
    old-style GNU sparse header (tarfile reads its extension blocks first and raises IndexError on a cut one).

    Every pax GNU sparse variant is refused (<name>:not_regular) before tarfile acts on it: _proc_pax hands a pax
    header's GNU.sparse keys to a sparse hook (_proc_gnusparse_00 for GNU.sparse.size, _01 for GNU.sparse.map, _10
    for major 1 minor 0, which reads the map from the data that follows with no bound but the file) after fetching
    the next header and before the member reaches any check in _scan, so these overrides raise in place of the
    hooks; any other GNU.sparse keyword (name, realsize, numblocks, offset, numbytes, another version) is refused in
    _apply_pax_info before it can rename or resize the member, whatever the key's case. Ordinary pax keywords
    (libarchive's atime, ctime, mtime and LIBARCHIVE.creationtime) pass unchanged. Unlike _proc_member, these four
    are private, undocumented tarfile methods: their names and call sites were checked on Python 3.14.6 and 3.11.15;
    the tests fail if one is renamed, but a hook a later Python adds under a new name would not be caught."""
    depth = 0

    class BoundedTarInfo(tarfile.TarInfo):
        def _proc_member(self, tar):
            nonlocal depth
            if self.type == tarfile.XGLTYPE:
                raise ArchiveProblem("archive:pax_global_header")
            if self.type == tarfile.GNUTYPE_SPARSE:
                raise ArchiveProblem(f"{_label(self.name)}:not_regular")
            if self.type in _EXTENDED_TYPES:
                if self.size > limits.max_extended_header_bytes:
                    raise ArchiveProblem("archive:extended_header_too_large")
                if depth >= MAX_EXTENDED_HEADERS:
                    raise ArchiveProblem("archive:extended_header_chain")
            depth += 1
            try:
                return super()._proc_member(tar)
            finally:
                depth -= 1

        def _proc_gnusparse_00(self, member, headers, *_):
            name = headers.get("path", member.name) if isinstance(headers, dict) else member.name
            raise ArchiveProblem(f"{_label(name)}:not_regular")

        _proc_gnusparse_01 = _proc_gnusparse_10 = _proc_gnusparse_00

        def _apply_pax_info(self, pax_headers, encoding, errors):
            if any(key.casefold().startswith("gnu.sparse.") for key in pax_headers):
                raise ArchiveProblem(f"{_label(pax_headers.get('path', self.name))}:not_regular")
            return super()._apply_pax_info(pax_headers, encoding, errors)

    return BoundedTarInfo


def _open_stream(reader, limits):
    return tarfile.open(fileobj=reader, mode="r|", tarinfo=_bounded_tarinfo(limits))


def _size_problem(info, file_bytes):
    """True when a header's declared data runs past the end of the file (or is negative)."""
    return info.size < 0 or info.offset_data + info.size > file_bytes


class HashingReader:
    """A read() view of a binary stream that hashes and counts every byte handed out, and remembers the offset just
    past the last byte that is not zero (data_end)."""

    def __init__(self, stream):
        self.stream = stream
        self.digest = hashlib.sha256()
        self.size = 0
        self.data_end = 0

    def read(self, size=-1):
        data = self.stream.read(size)
        self.digest.update(data)
        if data:
            if data[-1]:
                self.data_end = self.size + len(data)
            else:
                kept = len(data.rstrip(b"\0"))
                if kept:
                    self.data_end = self.size + kept
        self.size += len(data)
        return data

    def drain(self):
        while self.read(CHUNK_BYTES):
            pass
        return self.digest.hexdigest(), self.size


def _decompressed(read):
    """Each decompressed chunk (at most CHUNK_BYTES) of a possibly multi-stream bz2 source read by read(n).

    ArchiveProblem: empty_member, not_bz2 (including bytes after the last stream that do not start another one) or
    truncated_bz2 (input ends inside a stream). A not_bz2 from a failed decompress call carries discarded =
    CHUNK_BYTES: libbz2 checks a block's CRC only after producing the block, and the failed call drops what it
    produced, up to its max_length, so that work is never yielded but must still be charged."""
    pending = read(CHUNK_BYTES)
    if not pending:
        raise ArchiveProblem("empty_member")
    decompressor = bz2.BZ2Decompressor()
    while True:
        try:
            out = decompressor.decompress(pending, CHUNK_BYTES)
        except (OSError, ValueError, EOFError):
            problem = ArchiveProblem("not_bz2")
            problem.discarded = CHUNK_BYTES
            raise problem from None
        if out:
            yield out
        if decompressor.eof:
            pending = decompressor.unused_data or read(CHUNK_BYTES)
            if not pending:
                return
            decompressor = bz2.BZ2Decompressor()
        elif decompressor.needs_input:
            pending = read(CHUNK_BYTES)
            if not pending:
                raise ArchiveProblem("truncated_bz2")
        else:
            pending = b""


def content_months(window, final=True):
    """The months the "marketTime" values in a byte window name: the UTC month of a complete value that parses,
    else the literal YYYY-MM it starts with (a value escaped, too long, or cut where the data ends).

    With final False more data follows the window, so a value that runs to its end may simply continue in the next
    chunk: when it starts in the window's last _MARKET_TIME_TAIL bytes (the tail the caller carries into the next
    window) it is left out and read whole next time, so "2026-01-01T00:00:00+01:00" cut after "2026-01" counts once,
    as UTC 2025-12; one starting earlier cannot be carried and counts by its literal month, as at the end."""
    months, end = set(), len(window)
    for match in _MARKET_TIME_RE.finditer(window):
        value, year, month, closed = match.groups()
        if closed:
            months.add(market_month(value.decode("ascii", "replace")))
        elif final or match.end() < end or match.start() < end - _MARKET_TIME_TAIL:
            months.add(f"{year.decode()}-{month.decode()}")
    return months


def _decompress_check(read, compressed_size, limits, budget):
    """(decompressed bytes, content months) of one member, refusing a bomb as soon as it shows.

    ArchiveProblem: decompressed_too_large, compression_ratio_over_limit, total_decompressed_over_limit (budget,
    the archive's remaining allowance, ran out) or a _decompressed reason. Whatever the reason, the error's
    decompressed attribute is the work already spent on the member (bytes decompressed, the chunk that tripped a
    limit included, plus CHUNK_BYTES for a decompress call that failed and dropped its output), which _scan charges
    to the archive's allowance exactly like a member that passed."""
    ratio_cap = limits.max_ratio * compressed_size
    total, months, tail = 0, set(), b""
    try:
        for chunk in _decompressed(read):
            total += len(chunk)
            if total > ratio_cap:
                raise ArchiveProblem("compression_ratio_over_limit")
            if total > limits.max_member_decompressed_bytes:
                raise ArchiveProblem("decompressed_too_large")
            if total > budget:
                raise ArchiveProblem("total_decompressed_over_limit")
            window = tail + chunk
            months |= content_months(window, final=False)
            tail = window[-_MARKET_TIME_TAIL:]
    except ArchiveProblem as error:
        error.decompressed = total + getattr(error, "discarded", 0)
        raise
    return total, months | content_months(tail)


def _hash_rest(read):
    digest, size = hashlib.sha256(), 0
    while True:
        data = read(CHUNK_BYTES)
        if not data:
            return digest.hexdigest(), size
        digest.update(data)
        size += len(data)


def _content_gate(months):
    for month in sorted(months):
        reason = month_gate(month)
        if reason:
            return f"content_{reason}"
    return None


def _scan(tar_path, limits):
    """Pass 1: (archive sha256, archive size, kept members, refused members, directories, problems)."""
    kept, refused, directories, problems = [], [], [], set()
    names, folded = set(), set()
    total_bytes = total_decompressed = count = end = 0
    with open(tar_path, "rb") as handle:
        file_bytes = Path(tar_path).stat().st_size
        reader = HashingReader(handle)
        try:
            with _open_stream(reader, limits) as tar:
                for info in tar:
                    # Where the next header starts, recorded per member: when tarfile swallows an error in a later
                    # header (a pax size of -1, say) it may already have moved its offset past that header.
                    end = tar.offset
                    count += 1
                    if count > limits.max_members:
                        problems.add("archive:too_many_members")
                        break
                    name, label = info.name, _label(info.name)
                    if _size_problem(info, file_bytes):        # before tarfile may skip ahead over the data
                        problems.add(f"{label}:size_beyond_archive_end")
                        break
                    problem, kind = name_problem(name, limits), _member_kind(info)
                    if problem:
                        problems.add(f"{label}:{problem}")
                    if kind not in ("file", "directory"):
                        problems.add(f"{label}:{kind}")
                        continue
                    if name in names:
                        problems.add(f"{label}:duplicate_name")
                        continue
                    if name.casefold() in folded:
                        problems.add(f"{label}:case_collision")
                        continue
                    names.add(name)
                    folded.add(name.casefold())
                    if problem:
                        continue
                    if kind == "directory":
                        directories.append(name)
                        continue
                    if info.size > limits.max_member_bytes:
                        problems.add(f"{label}:member_too_large")
                        continue
                    total_bytes += info.size
                    if total_bytes > limits.max_total_bytes:
                        problems.add("archive:total_size_over_limit")
                        continue
                    parsed, reason = parse_member_path(name)
                    reason = reason or month_gate(parsed["month"])
                    member = tar.extractfile(info)
                    if reason:                                  # hashed only: never decompressed or written
                        sha, size = _hash_rest(member.read)
                        refused.append({"path": name, "size_bytes": size, "sha256": sha, "reason": reason,
                                        "month": parsed["month"] if parsed else None, "content_months": None})
                        continue
                    if total_decompressed > limits.max_total_decompressed_bytes:
                        # The allowance is spent (failed members' work counts too, a failed decompress call as a
                        # whole chunk): nothing more is decompressed, so the work stays within the allowance plus
                        # the one chunk that went over it.
                        problems.add("archive:total_decompressed_over_limit")
                        continue
                    member_reader = HashingReader(member)
                    budget = limits.max_total_decompressed_bytes - total_decompressed
                    try:
                        decompressed, months = _decompress_check(member_reader.read, info.size, limits, budget)
                        sha, size = member_reader.drain()
                    except ArchiveProblem as error:
                        total_decompressed += getattr(error, "decompressed", 0)
                        code = str(error)
                        problems.add("archive:total_decompressed_over_limit" if code == "total_decompressed_over_limit"
                                     else f"{label}:{code}")
                        continue
                    total_decompressed += decompressed
                    content_reason = _content_gate(months)
                    if content_reason:
                        refused.append({"path": name, "size_bytes": size, "sha256": sha, "reason": content_reason,
                                        "month": parsed["month"], "content_months": sorted(months)})
                        continue
                    inspected = parsed["month"] in INSPECTED_MONTHS or bool(months & set(INSPECTED_MONTHS))
                    kept.append({**parsed, "path": name, "size_bytes": size, "sha256": sha,
                                 "decompressed_bytes": decompressed, "content_months": sorted(months),
                                 "inspected_month": inspected})
        except ArchiveProblem as error:
            problems.add(str(error))
        except (tarfile.TarError, EOFError, ValueError, OSError):
            problems.add("archive:not_a_readable_tar")
        archive_sha, archive_size = (None, reader.size) if problems else reader.drain()
    if not problems and (reader.data_end > end or archive_size - end < END_OF_ARCHIVE_BYTES):
        problems.add("archive:not_ended_by_zero_blocks")
    if count == 0 and not problems:
        problems.add("archive:no_members")
    # On a case-insensitive file system a file named like another member's directory would collide with it.
    prefixes = {"/".join(item["path"].split("/")[:i]).casefold()
                for item in kept + refused for i in range(1, item["path"].count("/") + 1)}
    prefixes.update(name.casefold() for name in directories)
    for item in kept + refused:
        if item["path"].casefold() in prefixes:
            problems.add(f"{_label(item['path'])}:file_directory_collision")
    return archive_sha, archive_size, kept, refused, directories, problems


def build_manifest(name, size, sha256, kept, refused, directories, limits):
    """The manifest value (schema_version 1): archive identity, plan, the layout and gate it was read under, the
    limits, kept members (path, size, sha256, decompressed size, plan, month and date from the path, event and
    market id, file type, content_months, inspected_month), refused members (path, size, sha256, reason, month from
    the path or null, content_months for a content refusal or null), directory entries and totals; every list
    sorted, so one archive read under one gate and one set of limits has one byte string."""
    plans = sorted({item["plan"] for item in kept})
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION, "kind": MANIFEST_KIND,
        "archive": {"name": name, "sha256": sha256, "size_bytes": size},
        "plan": plans[0] if len(plans) == 1 else ("mixed" if plans else None), "plans": plans,
        "months": sorted({item["month"] for item in kept}), "layout": MEMBER_LAYOUT, "month_gate": gate_snapshot(),
        "limits": asdict(limits),
        "members": sorted(kept, key=lambda item: item["path"]),
        "refused": sorted(refused, key=lambda item: item["path"]),
        "directories": sorted(directories),
        "totals": {"members": len(kept), "refused": len(refused), "directories": len(directories),
                   "member_bytes": sum(item["size_bytes"] for item in kept),
                   "decompressed_bytes": sum(item["decompressed_bytes"] for item in kept)},
    }


def manifest_bytes(manifest):
    return canonical_json(manifest) + b"\n"


def file_sha256(path):
    """(sha256, size) of one local file, read in chunks."""
    with open(path, "rb") as handle:
        return _hash_rest(handle.read)


def safe_path(root, *parts):
    """root joined with parts, or ArchiveProblem(unsafe_target) when a part is not one plain name (empty, ".", "..",
    or holding a separator), any component under the data root is a symlink, one before the last exists and is not
    a directory (a write would land elsewhere, a read would come from elsewhere), or the root or a directory walked
    holds a ".git" entry (licensed raw bytes never go into a Git worktree, not even one below the root)."""
    if holds_git(root):
        raise ArchiveProblem("unsafe_target")
    place = root
    for index, part in enumerate(parts):
        if not isinstance(part, str) or part in ("", ".", "..") or "/" in part or "\\" in part:
            raise ArchiveProblem("unsafe_target")
        place = place / part
        if place.is_symlink():
            raise ArchiveProblem("unsafe_target")
        if index < len(parts) - 1 and place.exists() and (not place.is_dir() or holds_git(place)):
            raise ArchiveProblem("unsafe_target")
    return place


def safe_target(root, archive_sha256, member_path):
    """The raw path of one member under the data root (safe_path)."""
    return safe_path(root, "raw", archive_sha256, *member_path.split("/"))


def write_new(target, chunks, expected_sha256=None, expected_size=None):
    """Write chunks to target through "<name>.partial" (exclusive create) and rename it into place.

    ArchiveProblem: partial_file_present (an interrupted or concurrent run left it; remove it by hand),
    target_exists, or changed_during_ingest (the bytes written do not hash to expected_sha256)."""
    partial = target.with_name(target.name + ".partial")
    try:
        handle = open(partial, "xb")
    except FileExistsError:
        raise ArchiveProblem("partial_file_present") from None
    try:
        digest, size = hashlib.sha256(), 0
        with handle:
            for chunk in chunks:
                handle.write(chunk)
                digest.update(chunk)
                size += len(chunk)
        if expected_sha256 is not None and (digest.hexdigest(), size) != (expected_sha256, expected_size):
            raise ArchiveProblem("changed_during_ingest")
        if target.exists() or target.is_symlink():
            raise ArchiveProblem("target_exists")
        partial.replace(target)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise


def _chunks(read):
    while True:
        data = read(CHUNK_BYTES)
        if not data:
            return
        yield data


def _copy(tar_path, root, archive_sha256, archive_size, wanted, limits):
    """Pass 2: write each wanted member ({path: (sha256, size)}) and recompute the archive hash.

    (archive sha256, written paths); ArchiveProblem with a full "<where>:<reason>" code: archive:changed_during_ingest
    (the TAR cannot be opened again, or a header no longer fits the file), raw:<path>:<reason> for a write problem."""
    written = []
    try:
        handle = open(tar_path, "rb")
    except OSError:
        raise ArchiveProblem("archive:changed_during_ingest") from None
    with handle:
        reader = HashingReader(handle)
        with _open_stream(reader, limits) as tar:
            for info in tar:
                if _size_problem(info, archive_size):
                    raise ArchiveProblem("archive:changed_during_ingest")
                if info.name not in wanted or _member_kind(info) != "file":
                    continue
                label = _label(info.name)
                sha, size = wanted[info.name]
                try:
                    target = safe_target(root, archive_sha256, info.name)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    safe_target(root, archive_sha256, info.name)
                    write_new(target, _chunks(tar.extractfile(info).read), sha, size)
                    safe_target(root, archive_sha256, info.name)        # defence in depth: see the docstring
                except ArchiveProblem as error:
                    raise ArchiveProblem(f"raw:{label}:{error}") from None
                except OSError:
                    raise ArchiveProblem(f"raw:{label}:write_failed") from None
                written.append(info.name)
        archive_sha, _ = reader.drain()
    return archive_sha, written


def refused_result(problems, **details):
    return {"status": "refused", "problems": sorted(set(problems)), **details}


def _refusal_counts(refused):
    """Counts of the month-gate refusals. sealed_holdout_members is null when any member was refused before its
    month was known (month_unknown_members), because then holdout files may have been downloaded unseen."""
    reasons = {}
    for item in refused:
        reasons[item["reason"]] = reasons.get(item["reason"], 0) + 1
    unknown = sum(1 for item in refused if item["month"] is None)
    sealed = sum(count for reason, count in reasons.items() if reason.endswith("month_sealed_holdout"))
    return {"refused": len(refused), "refused_by_reason": dict(sorted(reasons.items())),
            "month_unknown_members": unknown, "sealed_holdout_members": None if unknown else sealed}


def _existing_manifest_problem(target, archive_sha):
    """None when the manifest at target has exactly these bytes; else gate_changed (it was written under another
    month gate) or would_change."""
    try:
        old = json.loads(target.read_bytes())
        if isinstance(old, dict) and "month_gate" in old and old["month_gate"] != gate_snapshot():
            return f"manifest:{archive_sha}:gate_changed"
    except (ValueError, RecursionError):
        pass
    return f"manifest:{archive_sha}:would_change"


def ingest_archive(tar_path, data_root, limits=DEFAULT_LIMITS):
    """Ingest one TAR into data_root; a JSON-ready dict.

    status ingested (members written or restored, manifest written if new), already_ingested (identical manifest
    and every raw file present with its hash), or refused with every problem as "<where>:<reason>" (an archive
    that kept nothing is refused as archive:nothing_kept with its counts). Month-gate refusals are counted
    (refused_by_reason), not problems; sealed_holdout_members counts members dated in the sealed holdout by path or
    content: nothing of them is stored, but a non-zero count means holdout files were downloaded, which
    docs/backtest-plan.md section 3 says must be recorded. It is null when month_unknown_members (members refused
    before their month was known) is not zero: missing is reported as unknown, never as zero."""
    root, problems = check_data_root(data_root)
    if problems:
        return refused_result(problems)
    try:
        return _ingest(Path(tar_path), root, limits)
    except OSError as error:
        return refused_result([f"io:{type(error).__name__}"])


def _ingest(tar, root, limits):
    try:
        if not tar.is_file():
            return refused_result(["archive:not_a_regular_file"])
        if tar.stat().st_size > limits.max_archive_bytes:
            return refused_result(["archive:too_large"])
        archive_sha, size, kept, refused, directories, problems = _scan(tar, limits)
    except OSError:
        return refused_result(["archive:unreadable"])
    if problems:
        return refused_result(problems)
    counts = _refusal_counts(refused)
    if not kept:
        return refused_result(["archive:nothing_kept"], archive_sha256=archive_sha, directories=len(directories),
                              **counts)
    manifest = build_manifest(tar.name, size, archive_sha, kept, refused, directories, limits)
    data = manifest_bytes(manifest)
    try:
        target = safe_path(root, "manifests", f"{archive_sha}.json")
        partial = target.with_name(target.name + ".partial")
        if partial.exists() or partial.is_symlink():                   # with the raw partials, before any write
            problems.add(f"manifest:{archive_sha}:partial_file_present")
        existed = target.exists()
        if existed and target.read_bytes() != data:
            return refused_result(problems | {_existing_manifest_problem(target, archive_sha)})
    except ArchiveProblem as error:
        return refused_result([f"manifest:{archive_sha}:{error}"])
    except OSError:
        return refused_result([f"manifest:{archive_sha}:unreadable"])
    wanted, present = {}, 0
    for item in kept:
        where = f"raw:{_label(item['path'])}"
        try:
            raw = safe_target(root, archive_sha, item["path"])
            leftover = raw.with_name(raw.name + ".partial")
            if leftover.exists() or leftover.is_symlink():              # a dangling link too: write_new would fail
                problems.add(f"{where}:partial_file_present")
            elif raw.exists():
                if not raw.is_file() or file_sha256(raw) != (item["sha256"], item["size_bytes"]):
                    problems.add(f"{where}:existing_hash_mismatch")
                else:
                    present += 1
            else:
                wanted[item["path"]] = (item["sha256"], item["size_bytes"])
        except ArchiveProblem as error:
            problems.add(f"{where}:{error}")
        except OSError:
            problems.add(f"{where}:unreadable")
    if problems:
        return refused_result(problems)
    written = []
    if wanted:
        try:
            again, written = _copy(tar, root, archive_sha, size, wanted, limits)
        except ArchiveProblem as error:
            return refused_result([str(error)])
        except (tarfile.TarError, EOFError, ValueError, OSError):
            return refused_result(["archive:changed_during_ingest"])
        if again != archive_sha or len(written) != len(wanted):
            return refused_result(["archive:changed_during_ingest"])
    if not existed:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            write_new(target, [data])
        except ArchiveProblem as error:
            return refused_result([f"manifest:{archive_sha}:{error}"])
        except OSError:
            return refused_result([f"manifest:{archive_sha}:write_failed"])
    return {"status": "already_ingested" if existed and not written else "ingested", "archive_sha256": archive_sha,
            "manifest_path": str(target), "manifest_sha256": hashlib.sha256(data).hexdigest(),
            "plan": manifest["plan"], "months": manifest["months"], "members": len(kept), "written": len(written),
            "already_present": present, "directories": len(directories), **counts}
