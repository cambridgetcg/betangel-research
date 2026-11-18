"""python3 -m betangel_research.betfair.backtest.resting_study: run close-v1, close-early-v1 and close-v2-explore.

There are three kinds of study here, as in resting.py: close-v1 (resting.STUDY_ID), whose passes are blind and
registered; the new ids (resting.EARLY_IDS: close-early-v1 and its registered fallbacks), whose passes are blind and
explore and which read the explore label alone (resting.Plan.labels_read); and the fast ids (resting.FAST_IDS), whose
passes are explore passes alone (below: "A fast id"). STUDY_PASSES says which study runs which passes; PASSES stays
close-v1's two, which early_study imports.

--study ID --pass blind|registered|explore --registration-sha256 HEX --archive-root DIR --out DIR --code-commit HEX
--ledger DIR --ledger-head none|SEQ:SHA256 [--blind-receipt PATH] [--registration PATH] [--store DIR] runs
research/studies/<ID>.json (or --registration) over every archive ingested and indexed under the data root DIR
and writes summary.json, summary.md (totals only) and receipt.json into the new directory --out, through
"<out>.partial" (mode 0700, files 0600) renamed only when all three are whole. It prints one JSON line of counts,
codes and hashes and exits 0 (completed) or 2 (refused or failed: --out is not created then). A fault in the code
itself prints the failure internal:<exception class name> and nothing else of the error (main): a traceback or a
message can hold a key, and a key can be an id. An "order" here is a simulated intent inside the backtest package;
nothing here can reach an exchange. It is a module of its own, not a subcommand of the backtest CLI, because
__main__.py is one of the certified engine's sources and stays byte-identical; this module, resting.py and fills.py
are in the certificate's source descriptor, so adding them changed the certificate key again.

Refusals, before any record is read, in this order: --study is not one of STUDIES (nothing is read then); the pass
is not one of that study's (pass:not_supported; nothing is read then either); the registration file's raw bytes
must hash to --registration-sha256, the file must lie in a folder named studies, and the split file beside that
folder and the rule files in rules/ beside it, each a regular file of at most 1 MiB named by the registration, must
be the bytes it names (resting.parse_registration); the registration's id must be --study
(study:not_the_registrations: close-v1's file never runs under a new id's name, nor a new id's file under
close-v1's); --code-commit, the data root and --out as movement_study checks them; the blind pass takes no
--blind-receipt and every other pass needs the blind pass's canonical, completed receipt of this study,
registration, split file, --code-commit, backtest-source sha256 and input sha256; every manifest's read limits
must be the replay's. Then the study ledger (study_ledger.py): --out may not lie inside the ledger directory nor
hold it, under any spelling of that directory (the folders above --out are compared with it by file identity,
since a file system may ignore letter case); the ledger is opened with the head the operator typed, and for a
registered or explore pass with the entry the blind receipt names as its own (ledger_entry) as the anchor, where
the entry at that seq must be a look of this study, registration, input, backtest sources and commit; then the
budgets are checked. A pass whose identity the ledger already holds is a repeat. One explore pass per study: an
explore pass that is not a repeat is refused (explore:already_run) when the ledger holds an explore entry of this
study id, whatever its identity. The ledger request names the labels the plan reads (both for close-v1, the
explore label alone for a new id); its kind is look for a blind pass, registered for close-v1's registered pass
(with the registered m and alpha) and explore for a new id's explore pass (m 0, alpha "0").

The read is lp_study's: every record's months pass the current month gate (sealed months are never read), a market
id seen twice in the registered tier fails the run (duplicate_market), and the member's path is guarded and its
bytes hashed against the manifest. What the index record alone says of a record is read first (its month, its race
date and partition label, each cell's code: resting.record_code); only those calls report malformed_record. A
record whose cells all have a code, one with an unregistered label or a label the plan does not read included, is
never opened. The one place a file is opened (_walk) also refuses a record whose label the plan does not read
(replay:label_not_read): it never fires unless the wiring is wrong, and then the pass fails before it appends. Any
other record is walked once (resting.walk) to its last pull time. A cell holds one row (a favourite cell) or a tuple
of rows (a cell of an each_active rule file): the market counts once in the codes, each order once in B1 and in the
rows. Value rows hold no market or selection id, are kept in memory by cell and partition label read, and never
reach disk.

After the read the summary's bytes are built in memory; then the ledger gets its one entry (a look for a blind
pass, the registered entry for a registered pass, the explore entry for an explore pass) before any file or stdout
write; then the outputs are written. A repeat appends nothing, and its summary must hash to the stored tables hash
(ledger:repeat_differs otherwise). A
pass that fails or is stopped during the read has appended and written nothing. If an output cannot be written
after the append, or the code faults there, the entry stands, the result names the new head (ledger_head) beside
the failure, and the re-run is a repeat. If the entry was written and only its lock file could not be removed, the
outputs are still written and the result says ledger_lock_left: a person removes the lock file before the next
pass. The summary has no clock, so the same registration, input and code give the same bytes. The receipt names the
hashes, the commit, the ledger head after the pass, the pass's own entry (ledger_entry: the head for a new entry,
the stored entry for a repeat, so a repeat's receipt still leads the registered or explore pass to the look), the
lineage's counts of looks and registered passes (an explore entry is in neither count), started_at and recorded_at,
and two totals: bytes_walked, the compressed bytes the walks consumed, and bytes_drained, the rest of those members,
hashed and not parsed.

Tables of close-v1. Both passes (registration blind_pass.releases): B1 coverage by registered month and partition
label, records with no anchor and with no registered label; per cell B2 the codes, B3 the classes by label, B4 the fill
counts by label and model (among them the fills in a batch that left the market not OPEN and those in the batch
that turned it in play) and the fill counts of the classes that are not scored, B5 the sample SD and symmetric
ranges of gross g at the BSP as written over each model's filled part (both labels pooled, part and rest), the SD
over all scored orders, quantiles of the size shown at t ahead of the order and of the seconds to a fill, B6 the
detectable effect on the test label, B7 the diagnostics' counts (among them the scored orders with a batch that
began with no market status). The blind summary holds no location of an
outcome: no average, middle value, raw quantile, sign, interval, test, label or selection
(tests/test_backtest_resting.py walks every key and the Markdown). The registered pass adds: T5 the
intent-to-treat figures per cell and label at both BSP ends (resting.figures); T7 the same by half-year, every cell
on the explore label and the primaries on the test label; T8 the selection; T9 per primary the test, Holm, both
bounds, the gap, the null and the label; T10 the filled figures without the fills in the first registered second,
and the quantiles of the sum of 1 / BSP per market with a scored order. A p-value, test or label exists only under
a primary; every other figure is descriptive, and a primary's own rows in T5, T7 and T10 are headed as tested in
T9 only. Part and rest is also applied across a split, always after the labels are decided on the full values:
across the fill models (resting.given_figures; a model's figures that are held back show rest_not_given); a
half-year's figure is given only when the other half-year gives it too and both take it over the same kind of
group (the same filled_is_all_placed flag); a kill's pooled figures are given only when, on each label, the orders
they are over number statistics.min_values and are the orders that stand for that label's own optimistic fills,
and the label gives its own optimistic figures (the label a kill decided is printed either way); and the null's
main mean over the same orders is withheld when 1 to statistics.min_values - 1 of the claim's orders have no null
value.

Tables of a new id. Its blind look (registration blind_pass.releases) gives close-v1's blind tables on the labels
the plan reads, with these differences. B1 counts the records of every label and the orders of the labels read;
B2's columns add up to every index record (one of a label not read is under the code of an earlier gate it fails,
else partition_not_registered); every other table by label holds the labels read. B4 adds, per label and model,
the fills by kind in the second and in the third minute after a weekday whole hour (resting.OrderRow.fill_minute).
B5 gives no SD over all scored orders, and for a model flagged filled_is_all_placed neither an SD nor ranges: its
counts and the flag alone, so no SD over every scored order stands in for its filled SD. There is no B6: the
detectable effect describes a test. B7 reads the plan's trade counts (four, the last the rises the hour switch left
out) and adds the orders placed in the after-the-hour seconds. B8: per cell of an each_active rule file, the runners
of the markets whose market rows and selector held, by their own code (resting.Walked.runner_codes; issued: ordered
on). B9: per cell, the orders placed, the scored orders and the pessimistic fills of scored orders by the gap at t
in CLASSIC ticks (GAP_RANGES, and not_in_a_range for any other gap or none). Its withheld text is its own
(WITHHELD_EARLY). Its explore pass gives every table of the blind look with equal values (and no withheld text), and
adds three tables, each headed exploratory with its number of cells c and sqrt(2 ln c): E1 resting.figures per cell
on the labels read at both BSP ends; E2 the same by half-year, part and rest across the split as in T7; E3 the
quantiles of the sum of 1 / BSP per market with a scored order. A cell whose pessimistic fills fail resting.gate
gives counts only in E1 and E2 (reason counts_only; size_gate says which). Nothing in an explore summary is tested,
bounded, selected or labelled: no selection, verdict or null is computed for it.

A fast id (resting.FAST_IDS; DESIGN-rule-v2 §3). Its only pass is explore (G1: blind and registered are
pass:not_supported, so it never writes a look or a registered line), any number of them, each a registration of its
own, up to its lineage's explore budget on the explore label, which the ledger keeps (no explore:already_run). Its
refusals, after the study's and before any record is read: its registration lies in the repository's
research/studies/ (and then takes no --store: store:not_for_this_registration) or in the content-addressed store,
<store>/<the registration's sha256>/studies/<id>.json, where <store> is --store (registration:not_in_the_store),
a directory of mode 0700 through path_guard, under no data tree's name and outside this repository and every Git
worktree (store:<reason>), and the registration, its split file and its rule files are each a regular file of mode
0400, written once (<where>:mode_not_0400); any other id takes no --store (store:not_for_this_study). It takes no
--blind-receipt (blind_receipt:not_for_a_fast_pass). Every rule file's parent chain is resolved, parent by parent, to
a file in research/rules/ (a file Git holds ends the chain), over the regular *.json files of research/rules/ and,
for a store registration, the rule files an earlier pass ran from the store: in every folder d of the store named by
a sha256 (not a symlink, not the pass's own) that holds a fast id's registration of mode 0400 whose bytes hash to d,
the rule files of mode 0400 that registration names by sha256 (rule:parent_not_found otherwise); every member of the
chain names the fast id's lineage (rule:parent_of_another_lineage). Every manifest names the registered tier alone
(input:tier_not_registered); the ledger request names the explore label alone, of role explore
(fast:not_explore_only). After the ledger's own checks, every store folder a chain goes through must be the
registration of a fast explore line the ledger holds (rule:parent_pass_not_in_the_ledger): a parent is a file a
recorded pass ran. "A file in research/rules/" is a file of the checkout; that the checkout is clean and merged stays
the operator's to keep. Its summary: B1 gives the explore label's records and orders by month and one total of the
records on a label not read (resting.record_code codes such a record partition_not_registered before any other gate,
so B2 holds them in one total, the same at every cell); B4 has no minute block; every cell adds its moves with no
value per window; the machinery counts of §2.2a (b) per cell and per rule file (B10 to B14); the machinery checks
MC1 to MC7 (CHECKS), computed from counts before any figure, the minute rule (MC6, MC7) read per rule file, per cell
and pooled; and E1 to E3, where each cell's figures are resting.figures under net_closable with the realised
diagnostic (rounded and unrounded) and closable_differs beside them, and E1 and E2 name the BSP ends the reading rule
reads for a claim and for a kill (reading_ends). If a check fails, every outcome table (E1, E2, E3's quantiles and
B5's spread) is built by the one counts-only path (_fast_counts), which computes no statistic and leaves no count that
locates an outcome (no hit count, no BSP off the ladder, nothing read from won or dead_heat); the same path serves a
cell below the size gate, and gives the counts of every figure part and rest across the half-years leaves with no
mean (_rest_counts). The receipt adds explore_lines_on_label (the lineage's explore and registered lines on the
label, counted as the budget counts them), explore_budget, the parent chains, the registration's folder and the store
root (store_root; None for research/studies/). The registered pass's tables read net_held, and a plan that values
fills otherwise never reaches them (_registered).

Markdown tables print 100 x g as points of stake; summary.json keeps fractions of stake, floats rounded to 6
places.

Standard library only; it imports the backtest modules named above and nothing outside the package but what they
import (tests/test_betfair_backtest_boundaries.py). It never logs in, reads an account or sends anything.
"""

import argparse
import bz2
from dataclasses import asdict, replace
import hashlib
import itertools
import json
import math
from pathlib import Path
import sys

from . import fills
from .archive import DEFAULT_LIMITS, REPOSITORY_ROOT, ArchiveProblem, HashingReader, check_data_root, month_gate, \
    safe_target, utc_month
from .certificate import CertificateProblem, source_descriptor
from .contract import canonical_json
from .lp import CROSS, FILL_KINDS, LAY, THROUGH, VOLUME, bsp_book
from .market_index import loads_strict
from .movement import ISSUED, MAX_REGISTRATION_BYTES, RegistrationError, registration_sha256
from .movement_study import (COMMIT_RE, MAX_RECEIPT_BYTES, SHA256_RE, StudyRefused, _failure, _guarded, _in_data_tree,
                             _out_path, _read_regular, _refuse, _table, _write)
from .partition import EXPLORE as EXPLORE_ROLE
from .partition import HALVES, MAX_SPLIT_BYTES
from .replay import ReplayProblem, _member
from .resting import (AS_WRITTEN, BSP_ENDS, BSP_OFF_THE_LADDER, CLAIM_END, CLASSES, CLOSABLE_FILLS, CLV_TICKS,
                      COUNTS_ONLY, EARLY_IDS, ENDS, FAST_IDS, FAST_LINEAGES, FILLED, FILLED_IS_ALL_PLACED, FLAG_GATES,
                      G, HIT_RATE, IMAGE_BATCHES, IMAGE_LEFT_OUT, KILL_END, LATER, MACHINERY_CHECK_FAILED, MEASURES,
                      MINUTE_BUCKETS, MOVE_COUNTS, NET_CLOSABLE, NET_CLOSED, NET_HELD, NULL_UNDEFINED,
                      PRICE_BAND_NAMES, PRICE_BANDS, REST_NOT_GIVEN, SCORED, SECOND, STATISTICS, STUDY_ID, THIRD,
                      THROUGH_NOT_LOWERED, UNFILLED_AFTER_REFUSAL, OrderRow, blind_outcome, closable_counts,
                      closable_differs, deciding_end, described, ends_apart, figures, filled_part, gap, gate,
                      given_figures, named_files, named_rule_files, orders_unfilled_after_through_refused,
                      parse_registration, price_band, quantile_table, race_label, realised_figure, record_code,
                      rounded, scored, select_primaries, series, spread, verdicts, walk, whole_hundredths, withheld,
                      without_first_fills)
from .resting import _counts as _no_statistics
from .resting import _withholds
from .rulefile import EACH_ACTIVE, MAX_RULE_BYTES, RuleError, parse_rule
from .rulefile import windows as rulefile_windows
from .runner import RunRefused, _inputs, _record_months, _records, utc_now_text
from .study_ledger import (COMPLETED, EXPLORE_KIND, LOOK, PassRequest, StudyLedger, head_text, identity, ledger_dir,
                           parse_head)
from .study_ledger import REGISTERED as REGISTERED_KIND
from .trial_ledger import LedgerError, UnlockError

SUMMARY_KIND, RECEIPT_KIND, SCHEMA_VERSION = "betangel_resting_summary", "betangel_resting_receipt", 1
BLIND, REGISTERED, EXPLORE = "blind", "registered", "explore"
PASSES = (BLIND, REGISTERED)                        # close-v1's passes (early_study imports this tuple)
PASS_NAMES = (BLIND, REGISTERED, EXPLORE)           # every pass this module knows
# The passes each study runs: close-v1 its blind look and its registered pass; a new id its blind look and one
# explore pass, and no registered pass; a fast id explore passes alone (G1), so it never writes a look or a
# registered line.
STUDY_PASSES = {STUDY_ID: PASSES, **dict.fromkeys(EARLY_IDS, (BLIND, EXPLORE)), **dict.fromkeys(FAST_IDS, (EXPLORE,))}
STUDIES = tuple(STUDY_PASSES)                       # the studies this module runs
REGISTRATIONS = REPOSITORY_ROOT / "research" / "studies"
RULES_IN_GIT = REPOSITORY_ROOT / "research" / "rules"     # where a fast rule file's parent chain must end (G11)
STUDIES_FOLDER, RULES_FOLDER = "studies", "rules"
IN_GIT = "research"                                 # the folder a chain item names when it is in Git
# The content-addressed store (S9): its root, given as --store, a directory of mode 0700; every file a store
# registration's pass reads (the registration, the split file, the rule files), and every store file a parent chain
# goes through, a regular file of mode 0400, written once.
STORE_MODE, STORE_FILE_MODE = 0o700, 0o400
FILE_TYPE, DIRECTORY, REGULAR = 0o170000, 0o040000, 0o100000
# A fast summary's headings and notes (DESIGN-rule-v2 §2.5, S3; PR D's N8 correction; lab-Claude's option (a)).
DECIDING_HEADING = "the expectation given BSP; excludes the race result's noise"
REALISED_NOTE = ("realised: the race result read, commission per market rounded half up to the penny; at GBP 2 "
                 "commission is 0 on market wins under 12.5p, so the figure is usually kinder; any one market can be "
                 "up to half a penny less kind; read the unrounded figure beside it. It decides nothing alone")
DIFFERS_NOTE = ("closable_differs: the orders filled under both models of a pair whose close was possible under one "
                "and not the other; two models' net_closable or realised figures also rest on these orders, which "
                "part and rest does not withhold (a residual, named and counted)")
# The machinery checks of a fast summary (§2.5 S5, §2.2a (c)), in order, each with the name its failure gives
# (machinery_check_failed:<name>). MC6 and MC7 are close-early-v1's S1 minute rule for trade_through and for
# volume_at_price: under the pessimistic model on weekdays, at least MINUTE_FLOOR fills in minute 1 and more than
# MINUTE_RATIO times those in minute 2, read per rule file and pooled over the pass (R3), and per cell, which no
# sibling cell's fills can dilute (PR E's adversarial review, S1).
CHECKS = (("MC1", "input_hash_not_the_requests"), ("MC2", "codes_or_classes_not_adding_up"),
          ("MC3", "labels_read_not_the_explore_label_alone"), ("MC4", "trade_fill_in_the_after_the_hour_seconds"),
          ("MC5", "trade_fill_in_an_image_batch"), ("MC6", "after_the_hour_minute_excess"),
          ("MC7", "after_the_hour_minute_excess"))
MINUTE_KINDS = {"MC6": THROUGH, "MC7": VOLUME}
MINUTE_FLOOR, MINUTE_RATIO, POOLED = 40, 2, "pooled"
TRADE_KINDS = (THROUGH, VOLUME)                     # the kinds a counted trade fills by (MC4 to MC7)
SUMMARY_JSON, SUMMARY_MD, RECEIPT_JSON = "summary.json", "summary.md", "receipt.json"
WITHHELD = ("every location statistic of an outcome (an average, a middle value, a raw quantile or a sign), every "
            "interval, test, label and selection, any SD of a figure after commission, the SD over unfilled orders, "
            "and any statistic by price band or half-year: the registration's blind_pass.withholds")
# A new id's blind look withholds more (registration blind_pass.withholds).
WITHHELD_EARLY = ("every location statistic of an outcome (an average, a middle value, a raw quantile or a sign), "
                  "every interval, test, label and selection, any SD of a figure after commission, the SD over "
                  "unfilled orders and the SD over all scored orders (also where it would stand in for a model's "
                  "filled SD: that model gives no SD and no ranges, flagged filled_is_all_placed), the detectable "
                  "effect, any statistic of an outcome by gap, price or half-year, and of a label not read anything "
                  "but index record counts: the registration's blind_pass.withholds")
# B9: the ranges of the gap at t in CLASSIC ticks, and the name of the rest (no gap, or one outside them).
GAP_RANGES, NOT_IN_A_RANGE = ((1, 2), (3, 5), (6, 10), (11, 20)), "not_in_a_range"
INDEPENDENCE = "under independence; clustering makes it larger"
EXPLORATORY, NOT_TESTED = "exploratory", "not tested, unadjusted"
PRIMARY = "primary: tested in T9 only; this row is descriptive"     # a primary cell's rows on the test label
NO_ANCHOR, OUTSIDE, NO_LABEL = "no_anchor", "outside_registered_months", "no_registered_label"
RECORD_ERRORS = (KeyError, TypeError, AttributeError, IndexError, ValueError)


def _store_root(store):
    """The resolved store root (--store) after its checks (S9): given, an existing directory through path_guard, no
    data tree's name in its given or resolved form, outside this repository and every Git worktree (as
    archive.check_data_root checks a data root), and of mode 0700 (STORE_MODE); StudyRefused("store:<reason>")
    otherwise."""
    if store is None or not str(store).strip():
        _refuse("store:missing_path")
    if _in_data_tree(Path(store).absolute()):
        _refuse("store:under_a_data_tree")
    resolved = _guarded(store, "store")
    if _in_data_tree(resolved):
        _refuse("store:under_a_data_tree")
    _, problems = check_data_root(resolved)
    if problems:
        _refuse(problems[0].replace("data_root:", "store:", 1))
    try:
        mode = resolved.lstat().st_mode
    except OSError:
        _refuse("store:unreadable")
    if mode & FILE_TYPE != DIRECTORY or mode & 0o7777 != STORE_MODE:
        _refuse("store:mode_not_0700")
    return resolved


def _written_once(path):
    """Whether a store file is a regular file of mode 0400 (STORE_FILE_MODE), by lstat: written once (S9). A path that
    cannot be inspected is not."""
    try:
        mode = Path(path).lstat().st_mode
    except OSError:
        return False
    return mode & FILE_TYPE == REGULAR and mode & 0o7777 == STORE_FILE_MODE


def _same_folder(path, other):
    """Whether two existing paths are one directory, by file identity; a path that cannot be inspected is not."""
    try:
        return path.samefile(other)
    except OSError:
        return False


def _plan(study, path, expected, store=None):
    """(Plan, the split file's raw bytes, the registered alpha as its decimal text, the store root or None) of the
    registration and the files it names, after the checks in the module docstring; StudyRefused otherwise. A fast
    id's registration lies in the repository's research/studies/ (store root None; --store is then refused,
    store:not_for_this_registration) or in the content-addressed store, at <store>/<its own sha256>/studies/<id>.json
    where <store> is --store (registration:not_in_the_store otherwise; S9, C1): the store root passes _store_root,
    and the registration, the split file and every rule file are each of mode 0400 (<where>:mode_not_0400). Any other
    id takes no --store (store:not_for_this_study)."""
    if not (isinstance(expected, str) and SHA256_RE.fullmatch(expected)):
        _refuse("registration_sha256:malformed")
    path = REGISTRATIONS / f"{study}.json" if path is None else path
    raw = _read_regular(path, "registration", MAX_REGISTRATION_BYTES)
    if registration_sha256(raw) != expected:
        _refuse("registration:sha256_mismatch")
    folder = _guarded(path, "registration").parent
    if folder.name != STUDIES_FOLDER:
        _refuse("registration:not_in_a_studies_folder")
    try:
        split_name, rule_names = named_files(raw)
        split_raw = _read_regular(folder.parent / split_name, "split", MAX_SPLIT_BYTES)
        rules_raw = {name: _read_regular(folder.parent / RULES_FOLDER / name, "rule_file", MAX_RULE_BYTES)
                     for name in rule_names}
        plan = parse_registration(raw, split_raw, rules_raw)
    except RegistrationError as error:
        raise StudyRefused(str(error)) from None
    if not plan.fast:
        if store is not None:
            _refuse("store:not_for_this_study")
        return plan, split_raw, loads_strict(raw)["family"]["alpha"], None
    if folder == REGISTRATIONS.resolve():
        if store is not None:
            _refuse("store:not_for_this_registration")
        return plan, split_raw, loads_strict(raw)["family"]["alpha"], None
    if folder.parent.name != expected:
        _refuse("registration:not_in_the_store")
    root = _store_root(store)
    if not _same_folder(folder.parent.parent, root):
        _refuse("registration:not_in_the_store")
    for where, item in (("registration", path), ("split", folder.parent / split_name),
                        *(("rule_file", folder.parent / RULES_FOLDER / name) for name in rule_names)):
        if not _written_once(item):
            _refuse(f"{where}:mode_not_0400")
    return plan, split_raw, loads_strict(raw)["family"]["alpha"], root


def _store_candidates(store, own):
    """[(folder name, path, sha256, Rule)] of the rule files an earlier fast pass ran from the store (G11, C1, S9):
    for every folder d of the store named by 64 lowercase hex characters, not a symlink and not the pass's own (own),
    that holds the registration studies/<fast id>.json of a fast id, a regular file of mode 0400 whose raw bytes hash
    to d, each rule file in d/rules/ that registration names by sha256, a regular file of mode 0400 of those bytes
    that rulefile.parse_rule takes. Folders by name, files in the registration's order; nothing else is read. Whether
    d's pass ran is the ledger's (run_study: rule:parent_pass_not_in_the_ledger)."""
    found = []
    for item in sorted(store.iterdir()):
        if not SHA256_RE.fullmatch(item.name) or item.name == own or item.is_symlink() or not item.is_dir():
            continue
        for study in FAST_IDS:
            path = item / STUDIES_FOLDER / f"{study}.json"
            try:
                raw = _read_regular(path, "parent", MAX_REGISTRATION_BYTES)
                if not _written_once(path) or registration_sha256(raw) != item.name:
                    continue
                named, rules = named_rule_files(raw)
            except (StudyRefused, RegistrationError):
                continue
            if named != study:
                continue
            for name, digest in rules:
                rule_path = item / RULES_FOLDER / name
                try:
                    rule_raw = _read_regular(rule_path, "parent", MAX_RULE_BYTES)
                    if not _written_once(rule_path) or registration_sha256(rule_raw) != digest:
                        continue
                    found.append((item.name, rule_path, digest, parse_rule(rule_raw)))
                except (StudyRefused, RuleError):
                    continue
    return found


def _candidates(store, own):
    """[(where, path, sha256, Rule)] of every file a fast rule file's parent may be (G11, C1): the regular *.json
    files of research/rules/ at the checkout that rulefile.parse_rule takes (where: IN_GIT), sorted by path; then, for
    a registration in the store, the rule files an earlier pass ran from the store (_store_candidates; where: its
    folder's name). own: the pass's own registration sha256, whose folder is never a candidate."""
    found = []
    if not RULES_IN_GIT.is_symlink() and RULES_IN_GIT.is_dir():
        for path in sorted(RULES_IN_GIT.glob("*.json")):
            try:
                raw = _read_regular(path, "parent", MAX_RULE_BYTES)
                found.append((IN_GIT, path, registration_sha256(raw), parse_rule(raw)))
            except (StudyRefused, RuleError):
                continue
    return found + ([] if store is None else _store_candidates(store, own))


def _parent_chains(plan, store):
    """({rule id: [{"path", "sha256"}, ...]}, the store folders the chains go through): each rule file's parent chain,
    parent by parent, to the first file in research/rules/ (G11, C1). A parent is the candidate (_candidates) whose
    raw bytes hash to parent_sha256, one in Git first (equal hashes are equal bytes, so the order changes only the
    path named, and a file Git holds ends the chain). A parent no candidate holds, or a store file with no parent, is
    rule:parent_not_found; a parent of another lineage than the fast id's (FAST_LINEAGES), the file in Git included,
    is rule:parent_of_another_lineage. A path is research/rules/<name>, or <store folder>/rules/<name> under the store
    root. Every store folder named must be an explore pass's registration in the ledger (run_study)."""
    candidates = _candidates(store, plan.sha256)
    chains, folders = {}, set()
    for rule in plan.rules:
        chain, wanted, seen = [], rule.parent_sha256, set()
        while True:
            match = next((item for item in candidates if item[2] == wanted), None) \
                if wanted is not None and wanted not in seen else None
            if match is None:
                _refuse("rule:parent_not_found")
            where, path, digest, parent = match
            if parent.lineage != FAST_LINEAGES[plan.study]:
                _refuse("rule:parent_of_another_lineage")
            chain.append({"path": f"{where}/{RULES_FOLDER}/{path.name}", "sha256": digest})
            if where == IN_GIT:
                break
            folders.add(where)
            seen.add(wanted)
            wanted = parent.parent_sha256
        chains[rule.id] = chain
    return chains, folders


def _blind_receipt(path, plan):
    """(receipt, sha256 of its bytes, its own ledger entry as (seq, sha256)) of a completed blind-pass receipt of
    this registration and split file; StudyRefused otherwise."""
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
         receipt.get("status")) != (RECEIPT_KIND, SCHEMA_VERSION, plan.study, BLIND, "completed")):
        _refuse("blind_receipt:not_a_completed_blind_receipt")
    if receipt.get("registration_sha256") != plan.sha256:
        _refuse("blind_receipt:another_registration")
    if receipt.get("partitions_sha256") != plan.split.sha256:
        _refuse("blind_receipt:another_split")
    try:
        anchor = parse_head(receipt.get("ledger_entry"))
    except LedgerError:
        anchor = None
    if anchor is None:
        _refuse("blind_receipt:ledger_entry_malformed")
    return receipt, hashlib.sha256(data).hexdigest(), anchor


def _same_directory(path, other):
    """Whether two existing paths are one directory, by file identity: a file system that ignores letter case or
    Unicode form gives one directory several spellings. A path that cannot be inspected counts as the same."""
    try:
        return path.samefile(other)
    except OSError:
        return True


def _ledger(directory, expected, root, target, request, split_raw, anchor):
    """(the opened StudyLedger, the stored entry when this pass is a repeat, else None) after the ledger's checks
    in the module docstring; LedgerError or StudyRefused otherwise. anchor: the entry the blind receipt names as
    its own, for a registered pass."""
    resolved = ledger_dir(directory, root)
    if resolved in target.parents or target == resolved or target in resolved.parents:
        _refuse("out:not_apart_from_the_ledger")
    if any(_same_directory(above, resolved) for above in (target.parent, *target.parent.parents)):
        _refuse("out:not_apart_from_the_ledger")
    ledger = StudyLedger.open(directory, expected, root=root, anchor=anchor)
    if anchor is not None and identity(ledger.entries()[anchor[0]]) != identity(replace(request, kind=LOOK)):
        _refuse("blind_receipt:not_this_ledgers_look")
    return ledger, ledger.check(request, split_raw)


class Tally:
    """Everything the summary needs. rows: {Cell: {partition label read: [OrderRow]}}, every placed order of every
    class; no market or selection id enters it. Records are counted by every label, orders by the labels read.
    runner_codes: {Cell of an each_active rule file: {code: runners}} (B8). markets: the same rows grouped as the
    walk gave them, one item per market (an OrderRow, or an each_active cell's tuple), in the same order, which the
    realised diagnostic needs (commission nets within a market). move_undefined: {Cell: {window: counts}} summed
    over the records (resting.Walked.move_undefined; a v2 plan's only)."""

    def __init__(self, plan):
        self.markets = {cell: {label: [] for label in plan.labels_read} for cell in plan.cells}
        self.move_undefined = {cell: {} for cell in plan.cells}
        self.records = self.replayed = self.bytes_walked = self.bytes_drained = 0
        self.months = {month: {"records": dict.fromkeys(plan.labels + (NO_LABEL,), 0),
                               "orders": dict.fromkeys(plan.labels_read, 0)}
                       for month in plan.months + (OUTSIDE, NO_ANCHOR)}
        self.codes = {cell: dict.fromkeys(plan.codes + (ISSUED,), 0) for cell in plan.cells}
        self.rows = {cell: {label: [] for label in plan.labels_read} for cell in plan.cells}
        self.flags_missing = {cell: {name: 0 for name, _, _ in FLAG_GATES
                                     if getattr(plan.rule(cell.rule_id).market, name) is True}
                              for cell in plan.cells}
        self.books = {label: [] for label in plan.labels_read}  # the sum of 1 / BSP of a market with a scored order
        self.runner_codes = {cell: dict.fromkeys(plan.codes + (ISSUED,), 0) for cell in plan.cells
                             if plan.rule(cell.rule_id).select == EACH_ACTIVE}


def _walk(root, manifest, record, plan, limits):
    """(resting.walk's answer over the record's primary member, the compressed bytes the walk consumed, the bytes
    drained after it), after replay_view's checks of the member; the member's bytes are hashed against the
    manifest (lp_study._walk's checks). First, before the member is looked up: a record whose label the plan does
    not read is refused (replay:label_not_read). record_code has coded such a record already, so this never fires
    unless the wiring is wrong; it is the one place a file is opened."""
    market_id = record["market_id"]
    dated = race_label(plan, record)
    if dated is None or dated[1] not in plan.labels_read:
        raise ReplayProblem(f"replay:{market_id}:label_not_read")
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
                walked = walk(stream, record, plan, limits)
            consumed = reader.size
            digest = reader.drain()
    except OSError:
        raise ReplayProblem(f"replay:{market_id}:member_unreadable_file") from None
    if digest != (member["sha256"], member["size_bytes"]):
        raise ReplayProblem(f"replay:{market_id}:member_hash_mismatch")
    return walked, consumed, digest[1] - consumed


def record_facts(plan, record):
    """What the index record alone says: (its coverage row, or None for another plan; resting.race_label's
    answer; {Cell: its code, or None when the cell reaches the book}). These are the only calls whose errors
    are reported as a malformed record."""
    month = None
    if record["provenance"]["plan"] in plan.tiers:
        first = record["decision_time"]["first_published_market_time"]
        month = None if first is None else utc_month(first["value"])
        if month is None:
            month = NO_ANCHOR
        elif not plan.start_month <= month <= plan.end_month:
            month = OUTSIDE
    return month, race_label(plan, record), {cell: record_code(plan, record, cell)[0] for cell in plan.cells}


def decide_record(plan, record, facts, root, manifest, tally, limits=DEFAULT_LIMITS):
    """Add one index record to the tally: its codes, one row per placed order, and the runners of each each_active
    cell by code (module docstring)."""
    month, dated, outcomes = facts
    tally.records += 1
    label = None if dated is None else dated[1]
    if month is not None:
        tally.months[month]["records"][label if label in plan.labels else NO_LABEL] += 1
    if any(code is None for code in outcomes.values()):
        walked, consumed, drained = _walk(root, manifest, record, plan, limits)
        tally.replayed += 1
        tally.bytes_walked += consumed
        tally.bytes_drained += drained
        outcomes = walked.outcomes
        for cell, names in walked.flags_missing.items():
            for name in names:
                tally.flags_missing[cell][name] += 1
        for cell, found in walked.runner_codes.items():
            for code, runners in found.items():
                tally.runner_codes[cell][code] += runners
        for cell, windows in walked.move_undefined.items():
            for window, counts in windows.items():
                held = tally.move_undefined[cell].setdefault(window, dict.fromkeys(counts, 0))
                for name, count in counts.items():
                    held[name] += count
    valued = False
    for cell in plan.cells:
        outcome = outcomes[cell]
        if not isinstance(outcome, tuple):          # a code; an OrderRow is a tuple too
            tally.codes[cell][outcome] += 1
            continue
        tally.codes[cell][ISSUED] += 1              # the market counts once in the codes, each order once below
        found = (outcome,) if isinstance(outcome, OrderRow) else outcome
        for row in found:
            tally.months[month]["orders"][row.label] += 1
            tally.rows[cell][row.label].append(row)
            valued = valued or row.klass == SCORED
        tally.markets[cell][found[0].label].append(outcome)     # one race date, so one label, a market
    book = bsp_book(record) if valued else None
    if book is not None:
        tally.books[label].append(float(book))


# ---------------------------------------------------------------------------------------------------------------
# Tables

def _key(cell):
    """The text a table names a cell by: its rule file and its time."""
    return f"{cell.rule_id}@{cell.at_s}"


def _both(plan, tally, cell):
    """A cell's rows of every partition label the plan reads (both for close-v1), the explore label's first."""
    return [row for label in plan.labels_read for row in tally.rows[cell][label]]


def _early(plan):
    """Whether the plan is a new id's or a fast id's (resting.Plan.is_new, M3), whose tables differ from close-v1's
    (module docstring). One exception reads the id list itself: B4's minute block (_fill_counts, markdown), which
    counts minutes from 1, stays the new ids' alone (R5); a fast summary gives its own minute table instead."""
    return plan.is_new


def _fill_counts(plan, rows, model):
    """B4 for the scored rows of one cell and label under one model: counts and the fill rate; for a new id
    (resting.EARLY_IDS, not a fast id: R5) also the fills by kind in the second and in the third minute after a
    weekday whole hour."""
    found = [row.fills[model] for row in rows]
    filled = [fill for fill in found if fill.kind is not None]
    counts = {"scored": len(rows), "fills": len(filled), "fill_rate": len(filled) / len(rows) if rows else None,
              "by_kind": {kind: sum(1 for fill in filled if fill.kind == kind) for kind in FILL_KINDS},
              "within_s_of_t": {str(seconds): sum(1 for fill in filled if fill.within[index])
                                for index, seconds in enumerate(plan.within_s)},
              "after_the_hour": {kind: sum(1 for fill in filled if fill.kind == kind and fill.after_the_hour)
                                 for kind in FILL_KINDS},
              "left_the_market_not_open": sum(1 for fill in filled if fill.transition),
              "turned_the_market_in_play": sum(1 for fill in filled if fill.turn),
              "ended_by": {end: sum(1 for fill in found if fill.ended == end) for end in ENDS}}
    if plan.study in EARLY_IDS:         # fill_minute is None on a row built by hand: no minute is named then
        counts["minutes_after_the_hour"] = {
            str(minute): {kind: sum(1 for row in rows if row.fills[model].kind == kind
                                    and (row.fill_minute or {}).get(model) == minute) for kind in FILL_KINDS}
            for minute in (SECOND, THIRD)}
    return counts


def _filled_spread(plan, rows, model):
    """B5 of a new id under one model: spread() of gross g at the BSP as written over the model's filled scored
    rows, with the flag. A model flagged filled_is_all_placed gives its counts and the flag alone: no SD over every
    scored order stands in for its filled SD."""
    part = filled_part(plan, rows, model)
    values = series(plan, part.rows, G, AS_WRITTEN)
    found = {"values": len(values), "race_dates": len({day for day, _ in values}), "sd": None, "ranges": None}
    return dict(found if part.all_placed else spread(plan, values), **{FILLED_IS_ALL_PLACED: part.all_placed})


def _gaps(rows):
    """B9 of one cell's placed rows: {gap range: {placed, scored, pessimistic_fills}} by the gap in CLASSIC ticks at
    t (OrderRow.gap_ticks), each range of GAP_RANGES and then NOT_IN_A_RANGE for any other gap or none; the
    pessimistic fills are those of scored rows."""
    names = [f"{low}-{high}" for low, high in GAP_RANGES]
    out = {name: {"placed": 0, "scored": 0, "pessimistic_fills": 0} for name in names + [NOT_IN_A_RANGE]}
    for row in rows:
        ticks = row.gap_ticks
        item = out[next((name for name, (low, high) in zip(names, GAP_RANGES)
                         if ticks is not None and low <= ticks <= high), NOT_IN_A_RANGE)]
        item["placed"] += 1
        if row.klass == SCORED:
            item["scored"] += 1
            item["pessimistic_fills"] += row.fills[fills.PESSIMISTIC].kind is not None
    return out


def _spread_counts(plan, rows, model):
    """B5 of a fast plan under a failed machinery check: _filled_spread's keys, its counts and flag alone; no SD and
    no range is computed."""
    part = filled_part(plan, rows, model)
    return {"values": len(part.rows), "race_dates": len({row.race_date for row in part.rows}), "sd": None,
            "ranges": None, FILLED_IS_ALL_PLACED: part.all_placed}


def _window_key(window):
    """The text a table names a move window by: "<from_s>..<to_s>", "t" for a window that ends at the decision."""
    return f"{window[0]}..{'t' if window[1] is None else window[1]}"


def _cell_tables(plan, tally, cell, failed=None):
    """Tables B2 to B9 of one cell (module docstring): counts, and of an outcome its spread only. failed: a fast
    summary's failed machinery check, under which B5 gives counts alone (_spread_counts)."""
    by_label, pooled = tally.rows[cell], _both(plan, tally, cell)
    good, behind = scored(pooled), plan.models[-1]
    if _early(plan):
        spreads = _filled_spread if failed is None else _spread_counts
        spread_of_g = {"bsp_end": AS_WRITTEN, "filled": {model: spreads(plan, good, model) for model in plan.models}}
    else:
        spread_of_g = {
            "bsp_end": AS_WRITTEN,
            "all_scored": {key: item for key, item in spread(plan, series(plan, good, G, AS_WRITTEN)).items()
                           if key != "ranges"},
            "filled": {model: blind_outcome(plan, pooled, model, AS_WRITTEN) for model in plan.models}}
    tables = {
        "rule": cell.rule_id, "side": plan.rule(cell.rule_id).intents[0].side, "at_s": cell.at_s,
        "pull_s": cell.pull_s, "codes": dict(tally.codes[cell]),
        "classes": {label: {klass: sum(1 for row in rows if row.klass == klass) for klass in CLASSES}
                    for label, rows in by_label.items()},
        "fills": {label: {model: _fill_counts(plan, scored(rows), model) for model in plan.models}
                  for label, rows in by_label.items()},
        "fills_not_scored": {label: {klass: {model: sum(1 for row in rows if row.klass == klass
                                                         and row.fills[model].kind is not None)
                                             for model in plan.models} for klass in CLASSES if klass != SCORED}
                             for label, rows in by_label.items()},
        "spread_of_g": spread_of_g,
        "queue_at_t": {"model": behind, "orders": len(pooled),
                       "queue_quantiles": quantile_table(plan, [row.fills[behind].queue for row in pooled])},
        "seconds_to_fill": {model: _seconds(plan, [row.fills[model].seconds for row in good
                                                   if row.fills[model].kind is not None])
                            for model in plan.models},
        "trd_changes": {name: sum(row.trd[name] for row in pooled) for name in plan.trd_counts},
        "null_values": {"defined": sum(1 for row in good if row.null_mid is not None),
                        "undefined": {reason: sum(1 for row in good if row.null_reason == reason)
                                      for reason in NULL_UNDEFINED}},
        "flags_missing_at_t": dict(tally.flags_missing[cell]),
        "scored_orders_with_a_batch_begun_with_no_status": sum(1 for row in good if row.no_status),
    }
    if _early(plan):
        tables["orders_placed_in_the_hour_seconds"] = sum(1 for row in pooled if row.placed_in_the_hour)
        tables["by_gap_at_t"] = _gaps(pooled)
        if cell in tally.runner_codes:
            tables["runners_by_code"] = dict(tally.runner_codes[cell])
    if plan.fast:                       # §2.5: the moves without a value, per window of the cell's rule file
        tables["move_undefined"] = {_window_key(window): dict(tally.move_undefined[cell].get(window) or
                                                              dict.fromkeys(MOVE_COUNTS, 0))
                                    for window in rulefile_windows(plan.rule(cell.rule_id))}
    return tables


def _seconds(plan, values):
    return {"fills": len(values), "seconds_quantiles": quantile_table(plan, values)}


def _all_withheld(tree):
    """A tree of figures with every figure in it withheld (resting.withheld); counts and flags stay."""
    if not isinstance(tree, dict):
        return tree
    if "mean" in tree or "estimate" in tree:
        return withheld(tree)
    return {key: _all_withheld(item) for key, item in tree.items()}


def _together(parts):
    """Part and rest across the parts of one split. parts: {part: a tree of figures}, every tree of one shape. A
    figure (a dict with a mean or an estimate) is given for a part only when every part gives it; else each part
    keeps its counts and loses its statistics, so that a small part's figure cannot be worked out from the
    whole's and the other part's. Where the parts' filled_is_all_placed flags differ, their figures under that
    flag are over different kinds of group (one part's fills, another part's every order), and none is given."""
    first = next(iter(parts.values()))
    if not isinstance(first, dict):
        return parts
    if len({item.get(FILLED_IS_ALL_PLACED) for item in parts.values()}) > 1:
        return {name: _all_withheld(item) for name, item in parts.items()}
    value = next((key for key in ("mean", "estimate") if key in first), None)
    if value is None:
        merged = {key: _together({name: item[key] for name, item in parts.items()}) for key in first}
        return {name: {key: merged[key][name] for key in first} for name in parts}
    if all(item[value] is not None for item in parts.values()):
        return parts
    return {name: withheld(item) for name, item in parts.items()}


def _kill(plan, kill, rows):
    """A primary's kill block as it is printed. Its figures are over both labels pooled, so they are given only
    when, for each label, the pooled orders on it number min_values, are the very orders that stand for the
    label's own optimistic fills, and the label gives its own optimistic filled figures (given_figures): part and
    rest. figures_given says which."""
    front = fills.OPTIMISTIC
    part = filled_part(plan, scored(rows), front)
    sizes = {label: sum(1 for row in part.rows if row.label == label) for label in plan.labels}
    own = {label: scored(rows, label) for label in plan.labels}
    if all(sizes[label] >= plan.min_values and sizes[label] == len(filled_part(plan, found, front).rows)
           and (FILLED, front) in given_figures(plan, found) for label, found in own.items()):
        return dict(kill, orders_by_label=sizes, figures_given=True)
    return dict({key: None if key in STATISTICS else item for key, item in kill.items()}, orders_by_label=sizes,
                both={end: dict.fromkeys(nets) for end, nets in kill["both"].items()}, figures_given=False)


def _exploratory(cells):
    """What every exploratory table prints: its number of cells c and sqrt(2 ln c), about the largest of c
    independent standard scores."""
    return {"cells": cells, "sqrt_2_ln_c": math.sqrt(2 * math.log(cells))}


def _registered(plan, tally, apart):
    """Tables T5 and T7 to T10 of the registered pass (module docstring). The selection is given the explore
    label's rows and two counts per cell from the test label, nothing else. Its selection, tests and labels read
    net_held: a plan that values fills otherwise (net_closable, a fast plan's) never reaches them (ValueError)."""
    if plan.fast or plan.value_measure != NET_HELD:
        raise ValueError("registered:value_measure_not_net_held")

    def ends(rows):
        return {end: figures(plan, rows, end) for end in BSP_ENDS}

    def later(rows, model):
        found, stands = without_first_fills(plan, rows, model)
        given = (LATER, model) in given_figures(plan, rows)
        return {"filled_figure_stands": stands, "ends": {end: {
            what: figure if given else withheld(figure)
            for what, figure in ((what, described(plan, series(plan, found, what, end))) for what in (G, NET_HELD))}
            for end in BSP_ENDS}}

    explore, test = plan.labels
    counts = {}
    for cell in plan.cells:
        part = filled_part(plan, scored(tally.rows[cell][test]), plan.select_test_model)
        counts[cell] = (part.fills, part.fill_dates)
    selection = select_primaries(plan, {cell: tally.rows[cell][explore] for cell in plan.cells}, counts, apart)
    chosen = [cell for cell in selection.primaries.values() if cell is not None]
    found = verdicts(plan, selection.primaries, {cell: _both(plan, tally, cell) for cell in plan.cells}, apart)
    primaries = {}
    for rule_id, verdict in found.items():
        if verdict is None:
            primaries[rule_id] = None
            continue
        held = scored(tally.rows[verdict["cell"]][test])
        given = given_figures(plan, held)
        primaries[rule_id] = dict(
            verdict, cell=_key(verdict["cell"]), half_years=_together(verdict["half_years"]),
            models={model: item if (FILLED, model) in given else withheld(item)
                    for model, item in verdict["models"].items()},
            kill=_kill(plan, verdict["kill"], _both(plan, tally, verdict["cell"])),
            gap=gap(plan, held, fills.PESSIMISTIC, verdict["test"]["bsp_end"]))
    headings = {explore: EXPLORATORY, test: NOT_TESTED, "primary": PRIMARY}     # primary: a chosen cell on test
    return {
        "figures": dict(_exploratory(len(plan.cells)), headings=headings, by_cell={
            _key(cell): {label: ends(scored(tally.rows[cell][label])) for label in plan.labels}
            for cell in plan.cells}),
        "half_years": dict(_exploratory(len(plan.cells) * len(HALVES)), headings=headings, by_cell={
            _key(cell): {label: _together({half: ends(scored(tally.rows[cell][label], half=half))
                                           for half in HALVES})
                         for label in plan.labels if label == explore or cell in chosen}
            for cell in plan.cells}),
        "selection": {"eligible": [_key(cell) for cell in selection.eligible],
                      "primaries": {rule_id: cell and _key(cell) for rule_id, cell in selection.primaries.items()}},
        "primaries": primaries,
        "sensitivity": dict(_exploratory(len(plan.cells)), headings=headings, without_first_fills={
            _key(cell): {label: {model: later(scored(tally.rows[cell][label]), model) for model in plan.models}
                         for label in plan.labels} for cell in plan.cells},
            bsp_book={label: {"markets": len(values), "quantiles": quantile_table(plan, values)}
                      for label, values in tally.books.items()}),
    }


def _counts_only(tree):
    """A tree of figures with every statistic in it None and the reason counts_only (a cell below the size gate);
    counts and flags stay."""
    if not isinstance(tree, dict):
        return tree
    if "mean" in tree or "estimate" in tree:
        return {key: None if key in STATISTICS else COUNTS_ONLY if key == "reason" else item
                for key, item in tree.items()}
    return {key: _counts_only(item) for key, item in tree.items()}


def _explore(plan, tally):
    """Tables E1 to E3 of a new id's explore pass (module docstring), on the labels read, each headed exploratory
    with c and sqrt(2 ln c). A cell gives figures in E1 and E2 only when the pessimistic model's fills pass the size
    gate (resting.gate); else counts only. Nothing is tested, bounded, selected or labelled here."""
    def ends(rows):
        return {end: figures(plan, rows, end) for end in BSP_ENDS}

    gates, by_cell, halves = {}, {}, {}
    for cell in plan.cells:
        key = _key(cell)
        gates[key], by_cell[key], halves[key] = {}, {}, {}
        for label in plan.labels_read:
            rows = scored(tally.rows[cell][label])
            part = filled_part(plan, rows, fills.PESSIMISTIC)
            given = gate(plan, part.fills, part.fill_dates)
            gates[key][label] = {"model": fills.PESSIMISTIC, "fills": part.fills, "fill_race_dates": part.fill_dates,
                                 "figures_given": given}
            found, split = ends(rows), _together({half: ends(scored(rows, half=half)) for half in HALVES})
            by_cell[key][label] = found if given else _counts_only(found)
            halves[key][label] = split if given else _counts_only(split)
    return {
        "figures": dict(_exploratory(len(plan.cells)), heading=EXPLORATORY, size_gate=gates, by_cell=by_cell),
        "half_years": dict(_exploratory(len(plan.cells) * len(HALVES)), heading=EXPLORATORY, by_cell=halves),
        # As close-v1's T10, which holds the same table: headed with the pass's number of cells.
        "bsp_book": dict(_exploratory(len(plan.cells)), heading=EXPLORATORY, by_label={
            label: {"markets": len(values), "quantiles": quantile_table(plan, values)}
            for label, values in tally.books.items()}),
    }


# ---------------------------------------------------------------------------------------------------------------
# A fast id's tables (DESIGN-rule-v2 §2.2a (b), §2.5, §3.3 G6): the machinery counts and checks first, from counts
# alone; then E1 to E3, each figure either computed (_fast_figures) or, when a check failed or a cell's fills fail
# the size gate, built by the one counts-only path (_fast_counts), which computes no statistic.

def _buckets():
    return dict.fromkeys(MINUTE_BUCKETS, 0)


def _machinery_counts(plan, rows):
    """§2.2a (b) over placed rows of every class (a fill is a fill whatever the order's class): per model the fills
    by kind and minute bucket (resting.MINUTE_BUCKETS, R4), the level rule's refusals by bucket and the orders still
    unfilled at the pull after one (C3), the trade_through fills whose volume through the price was below the stake
    and the quote_cross fills on a crossed or locked book (S6), and the trade-kind fills in the after-the-hour
    seconds and in an image batch (MC4, MC5); per order the image batches met and the image rises left out by
    bucket, and the orders with in-play before the pull (N6)."""
    by_minute = {kind: {model: _buckets() for model in plan.models} for kind in FILL_KINDS}
    refused = {model: _buckets() for model in plan.models}
    images = {name: _buckets() for name in (IMAGE_BATCHES, IMAGE_LEFT_OUT)}
    for row in rows:
        for model in plan.models:
            fill = row.fills[model]
            if fill.kind is not None:
                by_minute[fill.kind][model][row.minutes[model]] += 1
            for bucket, count in row.through_refused[model].items():
                refused[model][bucket] += count
        for name, counts in row.images.items():
            for bucket, count in counts.items():
                images[name][bucket] += count

    def per_model(test):
        return {model: sum(1 for row in rows if row.fills[model].kind is not None and test(row.fills[model]))
                for model in plan.models}

    return {"fills_by_minute": by_minute, "image_batches_by_minute": images, THROUGH_NOT_LOWERED: refused,
            UNFILLED_AFTER_REFUSAL: {model: orders_unfilled_after_through_refused(rows, model)
                                     for model in plan.models},
            "trade_throughs_below_the_stake": per_model(lambda fill: fill.kind == THROUGH and fill.through_below_stake),
            "quote_crosses_on_a_crossed_book": per_model(lambda fill: fill.kind == CROSS and fill.crossed),
            "trade_fills_in_the_after_the_hour_seconds": per_model(
                lambda fill: fill.kind in TRADE_KINDS and fill.after_the_hour),
            "trade_fills_in_an_image_batch": per_model(lambda fill: fill.kind in TRADE_KINDS and fill.image),
            "orders": len(rows), "orders_in_play_before_the_pull": sum(1 for row in rows if row.in_play_before_pull)}


def _added(first, second):
    """The sum of two count trees of one shape."""
    if isinstance(first, dict):
        return {key: _added(first[key], second[key]) for key in first}
    return first + second


def _machinery_tables(plan, tally):
    """{by_cell, by_rule}: _machinery_counts per cell over its rows of the labels read, and per rule file summed over
    its cells."""
    by_cell = {cell: _machinery_counts(plan, _both(plan, tally, cell)) for cell in plan.cells}
    by_rule = {}
    for cell, counts in by_cell.items():
        by_rule[cell.rule_id] = counts if cell.rule_id not in by_rule else _added(by_rule[cell.rule_id], counts)
    return {"by_cell": {_key(cell): counts for cell, counts in by_cell.items()}, "by_rule": by_rule}


def _minute_excess(counts, kind):
    """Whether close-early-v1's S1 rule holds of one count tree for one kind: under the pessimistic model, fills in
    minute 1 after a weekday whole hour number MINUTE_FLOOR or more and are more than MINUTE_RATIO times those in
    minute 2."""
    minutes = counts["fills_by_minute"][kind][fills.PESSIMISTIC]
    return minutes["1"] >= MINUTE_FLOOR and minutes["1"] > MINUTE_RATIO * minutes["2"]


def _checks(plan, tally, machinery, input_sha256, requested):
    """The machinery checks MC1 to MC7 of a fast summary (CHECKS), from counts alone, before any figure: {check:
    {name, holds}}, MC6 and MC7 also with what they fired on: the rule files, then the cells (rule@seconds), then
    pooled, when the pass's rule files pooled fire it. requested: the input sha256 of the ledger request the pass
    recorded under."""
    rows = {cell: _both(plan, tally, cell) for cell in plan.cells}
    every = [row for found in rows.values() for row in found]
    holds = {
        "MC1": requested == input_sha256,
        "MC2": all(sum(tally.codes[cell].values()) == tally.records for cell in plan.cells)
        and all(row.klass in CLASSES for row in every)
        and sum(count for month in tally.months.values() for count in month["orders"].values()) == len(every),
        "MC3": plan.labels_read == (plan.explore_label,)
        and all(row.label == plan.explore_label for row in every)
        and all(set(by_label) == {plan.explore_label} for by_label in tally.rows.values()),
        "MC4": not any(sum(counts["trade_fills_in_the_after_the_hour_seconds"].values())
                       for counts in machinery["by_rule"].values()),
        "MC5": not any(sum(counts["trade_fills_in_an_image_batch"].values())
                       for counts in machinery["by_rule"].values()),
    }
    found = {name: {"check": check, "holds": holds[name]} for name, check in CHECKS[:5]}
    pooled = None
    for counts in machinery["by_rule"].values():
        pooled = counts if pooled is None else _added(pooled, counts)
    for name, check in CHECKS[5:]:
        kind = MINUTE_KINDS[name]
        fired = [rule for rule, counts in machinery["by_rule"].items() if _minute_excess(counts, kind)]
        # Every cell too, so that a sibling cell's minute-2 fills cannot dilute one cell's excess (adversarial S1).
        fired += [cell for cell, counts in machinery["by_cell"].items() if _minute_excess(counts, kind)]
        if pooled is not None and _minute_excess(pooled, kind):
            fired.append(POOLED)
        found[name] = {"check": check, "kind": kind, "holds": not fired, "fired_on": fired}
    return found


def _failed(checks):
    """The reason every outcome statistic of a fast summary is withheld, machinery_check_failed:<the first failed
    check's name>, or None when every check holds."""
    return next((f"{MACHINERY_CHECK_FAILED}:{item['check']}" for name, item in checks.items() if not item["holds"]),
                None)


def _fast_coverage(plan, tally):
    """B1 of a fast summary (G6): by registered month, the records and the orders of the explore label alone; then
    the records with no anchor and one total of the records with an anchor whose label is not read (a label not
    read or no registered label), with no split by label or month."""
    months = tally.months
    return {"by_month": {month: {"records": {plan.explore_label: row["records"][plan.explore_label]},
                                 "orders": dict(row["orders"])}
                         for month, row in months.items() if month != NO_ANCHOR},
            "records_without_an_anchor": sum(months[NO_ANCHOR]["records"].values()),
            "records_on_labels_not_read": sum(count for month, row in months.items() if month != NO_ANCHOR
                                              for label, count in row["records"].items()
                                              if label != plan.explore_label)}


def _pairs(plan):
    return list(itertools.combinations(plan.models, 2))


def _differs(plan, rows):
    """{"<model> and <model>": resting.closable_differs} for every pair of models: a count only (DIFFERS_NOTE)."""
    return {f"{first} and {second}": closable_differs(rows, first, second) for first, second in _pairs(plan)}


def _fast_figures(plan, rows, markets, end, stake):
    """resting.figures of one group of scored rows at one BSP end (a net_closable plan's: hit rate, CLV in ticks,
    price bands and closable fills per model), each model with its realised diagnostic beside (rounded and
    unrounded: resting.realised_figure over the group's markets), and closable_differs."""
    found = figures(plan, rows, end)
    given = given_figures(plan, rows)
    for model in plan.models:
        found["models"][model]["realised"] = {
            "rounded": realised_figure(plan, markets, model, end, stake, given),
            "unrounded": realised_figure(plan, markets, model, end, stake, given, rounded=False)}
    found["closable_differs"] = _differs(plan, rows)
    return found


def _fast_counts(plan, rows, why):
    """The one counts-only path (R2): _fast_figures' keys over one group of scored rows, every statistic None and
    the reason why, and no count that locates an outcome (no hit count, no BSP off the ladder, nothing read from won
    or dead_heat: realised's left_out is None). It computes no statistic, so none can be scrubbed late."""
    def counts(group):
        return _no_statistics(group, why)

    out = {"placed": counts(rows), "mid": counts([row for row in rows if row.mid is not None]), "models": {}}
    for model in plan.models:
        part = filled_part(plan, rows, model)
        filled = [row for row in rows if row.fills[model].kind is not None]
        unfilled = [row for row in rows if row.fills[model].kind is None]
        bands = {name: [] for name in PRICE_BAND_NAMES}
        for row in part.rows:
            bands[price_band(row.price)].append(row)
        out["models"][model] = {
            "fills": part.fills, "fill_race_dates": part.fill_dates, FILLED_IS_ALL_PLACED: part.all_placed,
            "filled": {what: counts(part.rows) for what in MEASURES + (NET_CLOSABLE,)},
            "value_per_placed": counts(rows),
            "gap": {"estimate": None, "se": None, "df": None, "bets": [len(filled), len(unfilled)],
                    "race_dates": [len({row.race_date for row in group}) for group in (filled, unfilled)],
                    "reason": why, "ci_low": None, "ci_high": None},
            HIT_RATE: counts(part.rows),
            CLV_TICKS: {FILLED: counts(part.rows), "unfilled": counts(unfilled), BSP_OFF_THE_LADDER: None},
            PRICE_BANDS: {name: counts(group) for name, group in bands.items()},
            CLOSABLE_FILLS: closable_counts(rows, model),
            "realised": {kind: dict(counts(part.rows), left_out=None) for kind in ("rounded", "unrounded")}}
    out["closable_differs"] = _differs(plan, rows)
    return out


def _rest_counts(plan, rows, merged):
    """The half-years of one cell's group after part and rest across them (_together), with what the counts-only path
    gives (_fast_counts over that half-year's rows) wherever a figure has no mean: a realised figure keeps the count
    and race dates of the model's filled part and no left_out; a CLV in ticks with no filled mean keeps, for each of
    its figures with no mean, the count and race dates of the filled part or of the unfilled rows, and no
    bsp_off_the_ladder. Each takes the reason its model's filled part gives (resting._withholds), else
    rest_not_given. So no count or reason left beside a withheld figure is read from won, dead_heat or a BSP off
    the ladder (R2): the valued orders a realised figure counts leave out the dead-heat markets, and the orders a CLV
    in ticks counts leave out the BSPs beyond the ladder; and a figure's own counts_only can say that too few were
    left. merged: _together's answer, whose dicts are its own wherever it withholds, so they are changed in place."""
    for half, by_end in merged.items():
        group = scored(rows, half=half)
        counted, given = _fast_counts(plan, group, REST_NOT_GIVEN)["models"], given_figures(plan, group)
        for found in by_end.values():
            for model, item in found["models"].items():
                # The reason, too, from the filled part alone: a figure's own counts_only can be read from dead_heat
                # (valued orders below min_values) or from a BSP beyond the ladder.
                why = _withholds(plan, filled_part(plan, group, model), model, given) or REST_NOT_GIVEN
                for kind, figure in item["realised"].items():
                    if figure["mean"] is None:
                        item["realised"][kind] = dict(counted[model]["realised"][kind], reason=why)
                ticks = item[CLV_TICKS]
                if ticks[FILLED]["mean"] is None:
                    for part in (FILLED, "unfilled"):
                        if ticks[part]["mean"] is None:
                            ticks[part] = dict(counted[model][CLV_TICKS][part], reason=why)
                    ticks[BSP_OFF_THE_LADDER] = None
    return merged


def _in_half(markets, half):
    """The markets of one half-year (a market's orders share one race date)."""
    return [market for market in markets
            if ((market,) if isinstance(market, OrderRow) else tuple(market))[0].half == half]


def _fast_explore(plan, tally, failed, apart):
    """E1 to E3 of a fast pass (module docstring), on the explore label: when failed (a machinery check), every
    figure of every cell by the counts-only path with that reason; else as _explore, a cell whose pessimistic fills
    fail the size gate by the counts-only path (reason counts_only), and E3's quantiles only when the checks hold.
    E1 and E2 name the BSP end the frozen reading rule (§3.2a) reads for a claim and for a kill (reading_ends:
    resting.deciding_end of a LAY, every fast intent's side, under the switch apart), so no reader picks one."""
    def ends(rows, markets, stake, why):
        if why is not None:
            return {end: _fast_counts(plan, rows, why) for end in BSP_ENDS}
        return {end: _fast_figures(plan, rows, markets, end, stake) for end in BSP_ENDS}

    gates, by_cell, halves = {}, {}, {}
    for cell in plan.cells:
        key, stake = _key(cell), plan.rule(cell.rule_id).intents[0].stake
        gates[key], by_cell[key], halves[key] = {}, {}, {}
        for label in plan.labels_read:
            rows, markets = scored(tally.rows[cell][label]), tally.markets[cell][label]
            part = filled_part(plan, rows, fills.PESSIMISTIC)
            given = failed is None and gate(plan, part.fills, part.fill_dates)
            gates[key][label] = {"model": fills.PESSIMISTIC, "fills": part.fills, "fill_race_dates": part.fill_dates,
                                 "figures_given": given}
            why = None if given else failed or COUNTS_ONLY
            by_cell[key][label] = ends(rows, markets, stake, why)
            split = {half: ends(scored(rows, half=half), _in_half(markets, half), stake, why) for half in HALVES}
            halves[key][label] = split if why is not None else _rest_counts(plan, rows, _together(split))
    headings = dict(heading=EXPLORATORY, deciding_heading=DECIDING_HEADING, realised_note=REALISED_NOTE,
                    differs_note=DIFFERS_NOTE,
                    reading_ends={"claim_end": deciding_end(LAY, CLAIM_END, apart),
                                  "kill_end": deciding_end(LAY, KILL_END, apart)})
    return {
        "figures": dict(_exploratory(len(plan.cells)), **headings, size_gate=gates, by_cell=by_cell),
        "half_years": dict(_exploratory(len(plan.cells) * len(HALVES)), **headings, by_cell=halves),
        "bsp_book": dict(_exploratory(len(plan.cells)), heading=EXPLORATORY, by_label={
            label: {"markets": len(values), "quantiles": None if failed else quantile_table(plan, values)}
            for label, values in tally.books.items()}),
    }


def summary(plan, tally, pass_name, input_sha256, requested_input_sha256=None):
    """The summary of one pass (module docstring), every float rounded; the blind pass's holds no location of an
    outcome, and the explore pass's holds every key and value of the blind pass's but withheld. A fast plan's
    machinery checks are computed first, from counts (requested_input_sha256: the ledger request's input hash, for
    MC1); if one fails, every outcome table is built by the counts-only path."""
    months = tally.months
    whole, of = whole_hundredths(plan, [row for cell in plan.cells for row in _both(plan, tally, cell)])
    apart = ends_apart(plan, whole, of)
    failed = machinery = checks = None
    if plan.fast:
        machinery = _machinery_tables(plan, tally)
        checks = _checks(plan, tally, machinery, input_sha256, requested_input_sha256)
        failed = _failed(checks)
    value = {"kind": SUMMARY_KIND, "schema_version": SCHEMA_VERSION, "study": plan.study, "pass": pass_name,
             "registration_sha256": plan.sha256, "partitions_sha256": plan.split.sha256,
             "input_sha256": input_sha256,
             "partitions": {"explore_label": plan.explore_label, "test_label": plan.test_label},
             "records": tally.records, "replayed_markets": tally.replayed,
             "coverage": {"by_month": {month: {"records": dict(row["records"]), "orders": dict(row["orders"])}
                                       for month, row in months.items()},
                          "records_without_an_anchor": sum(months[NO_ANCHOR]["records"].values()),
                          "records_without_a_registered_label": sum(row["records"][NO_LABEL]
                                                                    for month, row in months.items()
                                                                    if month != NO_ANCHOR)},
             "cells": {_key(cell): _cell_tables(plan, tally, cell, failed) for cell in plan.cells},
             "bsp_precision": {"scored_bsps_whole_hundredths": whole, "scored_bsps_not_whole_hundredths": of - whole,
                               "bsp_ends_decide_apart": apart}}
    if _early(plan):
        value["partitions"]["read"] = list(plan.labels_read)
    if plan.fast:
        value.update(coverage=_fast_coverage(plan, tally), machinery_checks=checks, machinery_counts=machinery,
                     withheld_by_the_checks=failed)
    if pass_name == REGISTERED:
        value.update(_registered(plan, tally, apart))
    elif pass_name == EXPLORE:
        value.update(_fast_explore(plan, tally, failed, apart) if plan.fast else _explore(plan, tally))
    else:
        value["withheld"] = WITHHELD_EARLY if _early(plan) else WITHHELD
    return rounded(value)


# ---------------------------------------------------------------------------------------------------------------
# Markdown

def _points(value):
    """A fraction of stake as points of stake (100 x), to 4 places."""
    return None if value is None else round(100 * value, 4)


def _figure_cells(item, value="mean"):
    return [_points(item.get(key)) for key in (value, "sd", "ci_low", "ci_high")]


def _figure_rows(found):
    """[(name, figure)] of one group's resting.figures at one BSP end, without the gaps."""
    rows = [("all placed: g", found["placed"]), ("the mid at t: g", found["mid"])]
    for model, item in found["models"].items():
        flag = " (all placed)" if item[FILLED_IS_ALL_PLACED] else ""
        rows += [(f"{model} filled{flag}: {what}", item["filled"][what]) for what in MEASURES]
        rows.append((f"{model}: value per placed order", item["value_per_placed"]))
    return rows


def _figure_lines(ends):
    """One group's figures at both BSP ends as two Markdown tables, in points of stake."""
    listed = [_figure_rows(ends[end]) for end in BSP_ENDS]
    columns = [f"{name} ({end})" for end in BSP_ENDS for name in ("mean", "SD", "CI low", "CI high")]
    lines = _table(["figure", "n", "race dates", *columns, "note"],
                   [[name, item["bets"], item["race_dates"], *_figure_cells(item), *_figure_cells(other),
                     item["reason"]] for (name, item), (_, other) in zip(*listed)])
    gaps = [[model, *(part for end in BSP_ENDS for part in _gap_cells(ends[end]["models"][model]["gap"]))]
            for model in ends[AS_WRITTEN]["models"]]
    columns = [f"{name} ({end})" for end in BSP_ENDS for name in ("filled", "unfilled", "gap", "CI low", "CI high",
                                                                   "note")]
    return lines + ["Gap: filled less unfilled, gross g (descriptive, no p)", ""] + _table(["model", *columns], gaps)


def _gap_cells(item):
    return [*item["bets"], _points(item["estimate"]), _points(item["ci_low"]), _points(item["ci_high"]),
            item["reason"]]


def _primary_lines(rule_id, item):
    """T9 for one rule file: the label with everything it is printed with."""
    if item is None:
        return [f"### {rule_id}: no primary cell", "", "No cell passed the selection's gates, so nothing is tested "
                "for this rule file. It still counts in the family.", ""]
    test, kill, null = item["test"], item["kill"], item["null"]
    lines = [f"### {rule_id}: primary cell {item['cell']}, label {item['label']}", "",
             *(f"- {text}" for text in item["printed_with"]),
             f"- blocked: {item['blocked'] or 'no'}; pessimistic fill rate on the test label: "
             f"{item['pessimistic_fill_rate']}",
             "- checks: " + ", ".join(f"{name} {'yes' if held else 'no'}" for name, held in item["checks"].items()),
             ""]
    lines += _table(["test (pessimistic, test label, net_held)", "BSP end", "n", "fills", "race dates", "mean",
                     "CI low", "CI high", "p one-sided", "Holm p", "rejected", "lower bound", FILLED_IS_ALL_PLACED,
                     "note"],
                    [["claim", test["bsp_end"], test["bets"], test["fills"], test["race_dates"],
                      _points(test["mean"]), _points(test["ci_low"]), _points(test["ci_high"]), test["p_one_sided"],
                      test["holm_p_adjusted"], test["holm_rejected"], _points(test["lower_bound"]),
                      test[FILLED_IS_ALL_PLACED], test["reason"]]])
    lines += _table(["kill (optimistic, both labels, net_closed)", "BSP end", "n", "fills", "race dates", "mean",
                     "kill bound", FILLED_IS_ALL_PLACED, "figures given", "note"],
                    [["kill", kill["bsp_end"], kill["bets"], kill["fills"], kill["race_dates"], _points(kill["mean"]),
                      _points(kill["bound"]), kill[FILLED_IS_ALL_PLACED], kill["figures_given"], kill["reason"]]])
    lines += _table(["both nets at both BSP ends", "BSP end", NET_CLOSED, NET_HELD],
                    [[name, end, _points(nets[NET_CLOSED]), _points(nets[NET_HELD])]
                     for name, block in (("claim", test), ("kill", kill)) for end, nets in block["both"].items()])
    lines += _table(["mean net_held at the claim end", "fills", FILLED_IS_ALL_PLACED, "mean"],
                    [[name, part["fills"], part[FILLED_IS_ALL_PLACED], _points(part["mean"])]
                     for group in (item["models"], item["half_years"]) for name, part in group.items()])
    if null is not None:
        lines += _table(["null", "n", "null mean", "null p two-sided", "main over the same orders", "main: note",
                         "null at least the fraction of main", "undefined", "blocks", "why"],
                        [["random time", null["null"]["bets"], _points(null["null"]["mean"]),
                          null["null"]["p_two_sided"], _points(null["main_same_orders"]["mean"]),
                          null["main_same_orders"]["reason"], null["at_least_the_fraction"], null["undefined"],
                          null["blocks"], null["why"]]])
    return lines + _table(["gap (pessimistic, test label, gross g)", "filled", "unfilled", "gap", "CI low", "CI high",
                           "note"], [["filled less unfilled", *_gap_cells(item["gap"])]])


def _registered_lines(value):
    """The registered pass's tables T8, T9, T5, T7 and T10."""
    labels = value["partitions"]
    chosen = {cell for cell in value["selection"]["primaries"].values() if cell is not None}

    def heading(table, cell, label):            # a primary's rows on the test label are not "not tested"
        return table["headings"]["primary" if label == labels["test_label"] and cell in chosen else label]

    lines = ["## T8 Selection (on the explore label's figures and the test label's counts)", "",
             f"- eligible cells: {', '.join(value['selection']['eligible']) or 'none'}",
             *(f"- {rule_id}: {cell or 'no primary'}" for rule_id, cell in value["selection"]["primaries"].items()),
             "", "## T9 Primaries (the only tests, p-values and labels of this pass)", ""]
    for rule_id, item in value["primaries"].items():
        lines += _primary_lines(rule_id, item)
    for title, key in (("T5 Intent-to-treat figures per cell and label", "figures"),
                       ("T7 The same by half-year", "half_years")):
        table = value[key]
        lines += [f"## {title} (points of stake; descriptive, no p)", "",
                  f"Cells in this table: c = {table['cells']}; sqrt(2 ln c) = {table['sqrt_2_ln_c']}.", ""]
        for cell, by_label in table["by_cell"].items():
            for label, group in by_label.items():
                head = f"### {cell}, {label}: {heading(table, cell, label)}"
                if key == "figures":
                    classes = value["cells"][cell]["classes"][label]
                    lines += [head, "", "Classes: " + ", ".join(f"{name} {count}"
                                                                 for name, count in classes.items()) + ".", ""]
                    lines += _figure_lines(group)
                else:
                    for half, ends in group.items():
                        lines += [f"{head}, {half}", ""] + _figure_lines(ends)
    table = value["sensitivity"]
    lines += ["## T10 Sensitivity (points of stake; descriptive, no p)", "",
              f"Cells in this table: c = {table['cells']}; sqrt(2 ln c) = {table['sqrt_2_ln_c']}. Filled figures "
              "without the fills in the first registered second after t; where too few fall outside it, the filled "
              "figure stands.", ""]
    columns = [f"{name} ({end})" for end in BSP_ENDS for name in ("mean", "SD", "CI low", "CI high")]
    lines += _table(["cell", "label", "heading", "model", "figure", "stands", "n", "race dates", *columns, "note"],
                    [[cell, label, heading(table, cell, label), model, what, item["filled_figure_stands"],
                      item["ends"][AS_WRITTEN][what]["bets"], item["ends"][AS_WRITTEN][what]["race_dates"],
                      *(part for end in BSP_ENDS for part in _figure_cells(item["ends"][end][what])),
                      item["ends"][AS_WRITTEN][what]["reason"]]
                     for cell, by_label in table["without_first_fills"].items() for label, models in by_label.items()
                     for model, item in models.items() for what in (G, NET_HELD)])
    books = table["bsp_book"]
    keys = next((list(item["quantiles"]) for item in books.values() if item["quantiles"]), [])
    lines += ["The sum of 1 / BSP per market with a scored order (nearest-rank quantiles)", ""]
    lines += _table(["label", "markets", *keys], [[label, item["markets"], *((item["quantiles"] or {}).get(q)
                                                                              for q in keys)]
                                                  for label, item in books.items()])
    return lines + [f"Partition labels: explore {labels['explore_label']}, test {labels['test_label']}.", ""]


def _early_lines(plan, cells):
    """A new id's tables B8 and B9, in both of its passes."""
    runners = {cell: tables["runners_by_code"] for cell, tables in cells.items() if "runners_by_code" in tables}
    lines = ["## B8 Runners of each each_active cell by their own code (the runners of the markets whose market rows "
             "and selector held; issued: ordered on)", ""]
    if runners:
        lines += _table(["code", *runners], [[code, *(found[code] for found in runners.values())]
                                             for code in plan.codes + (ISSUED,)
                                             if any(found[code] for found in runners.values())])
    else:
        lines += ["No cell of this study is of an each_active rule file.", ""]
    names = [*(f"{low}-{high}" for low, high in GAP_RANGES), NOT_IN_A_RANGE]
    lines += [f"## B9 Orders per cell by the gap at t in CLASSIC ticks (placed, scored, and the {fills.PESSIMISTIC} "
              "model's fills of scored orders)", ""]
    return lines + _table(["cell", "gap", "placed", "scored", f"{fills.PESSIMISTIC} fills"],
                          [[cell, name, *tables["by_gap_at_t"][name].values()]
                           for cell, tables in cells.items() for name in names])


def _explore_lines(value):
    """A new id's explore tables E1 to E3, each headed exploratory with its c and sqrt(2 ln c)."""
    lines = []
    for title, key in (("E1 Intent-to-treat figures per cell", "figures"), ("E2 The same by half-year", "half_years")):
        table = value[key]
        lines += [f"## {title} (points of stake; {table['heading']}, descriptive, no p)", "",
                  f"Cells in this table: c = {table['cells']}; sqrt(2 ln c) = {table['sqrt_2_ln_c']}. A cell whose "
                  f"{fills.PESSIMISTIC} fills fail the size gate gives counts only.", ""]
        for cell, by_label in table["by_cell"].items():
            for label, group in by_label.items():
                head = f"### {cell}, {label}: {table['heading']}"
                if key == "figures":
                    classes, given = value["cells"][cell]["classes"][label], table["size_gate"][cell][label]
                    lines += [head, "", "Classes: " + ", ".join(f"{name} {count}" for name, count in classes.items())
                              + f". Size gate: {given['fills']} {given['model']} fills on {given['fill_race_dates']} "
                              f"race dates; figures given: {'yes' if given['figures_given'] else 'no'}.", ""]
                    lines += _figure_lines(group)
                else:
                    for half, ends in group.items():
                        lines += [f"{head}, {half}", ""] + _figure_lines(ends)
    table = value["bsp_book"]
    keys = next((list(item["quantiles"]) for item in table["by_label"].values() if item["quantiles"]), [])
    lines += [f"## E3 The sum of 1 / BSP per market with a scored order (nearest-rank quantiles; {table['heading']})",
              "", f"Cells in this table: c = {table['cells']}; sqrt(2 ln c) = {table['sqrt_2_ln_c']}.", ""]
    return lines + _table(["label", "markets", *keys], [[label, item["markets"], *((item["quantiles"] or {}).get(q)
                                                                                    for q in keys)]
                                                        for label, item in table["by_label"].items()])


def _check_lines(value):
    """A fast summary's machinery checks MC1 to MC7, printed at its top."""
    checks, failed = value["machinery_checks"], value["withheld_by_the_checks"]
    lines = ["## M Machinery checks (computed from counts before any figure)", ""]
    lines += _table(["check", "what fails it", "holds", "fired on"],
                    [[name, item["check"] + (f" ({item['kind']})" if "kind" in item else ""),
                      "yes" if item["holds"] else "no", ", ".join(item.get("fired_on", ())) or None]
                     for name, item in checks.items()])
    if failed:
        return lines + [f"A check failed: every outcome statistic of this pass is withheld ({failed}). Counts stay, "
                        "but none that locates an outcome.", ""]
    return lines + ["Every check holds.", ""]


def _machinery_lines(plan, value):
    """A fast summary's tables B10 to B14 (DESIGN-rule-v2 §2.2a (b), §2.5): counts only, per rule file (summed over
    its cells) and per cell."""
    counts = value["machinery_counts"]
    groups = [(f"rule {rule}", item) for rule, item in counts["by_rule"].items()] + list(counts["by_cell"].items())
    lines = ["## B10 Fills by minute after a whole UTC hour, Monday to Friday (every placed order; minutes 0 to 5, the "
             "rest of the hour, weekend days)", ""]
    lines += _table(["group", "kind", "model", *MINUTE_BUCKETS],
                    [[name, kind, model, *item["fills_by_minute"][kind][model].values()]
                     for name, item in groups for kind in FILL_KINDS for model in plan.models])
    lines += ["## B11 Image batches met by resting orders, and image rises left out, by minute", ""]
    lines += _table(["group", "count", *MINUTE_BUCKETS],
                    [[name, what, *item["image_batches_by_minute"][what].values()]
                     for name, item in groups for what in (IMAGE_BATCHES, IMAGE_LEFT_OUT)])
    lines += ["## B12 The level rule: trade_through refused for a level not lowered, by minute, and the orders still "
              "unfilled at the pull after a refusal", ""]
    lines += _table(["group", "model", *MINUTE_BUCKETS, UNFILLED_AFTER_REFUSAL],
                    [[name, model, *item[THROUGH_NOT_LOWERED][model].values(), item[UNFILLED_AFTER_REFUSAL][model]]
                     for name, item in groups for model in plan.models])
    names = ("trade_throughs_below_the_stake", "quote_crosses_on_a_crossed_book",
             "trade_fills_in_the_after_the_hour_seconds", "trade_fills_in_an_image_batch")
    lines += ["## B13 Fills to look at, per model, and the orders with in-play before the pull", ""]
    lines += _table(["group", "model", *names], [[name, model, *(item[key][model] for key in names)]
                                                 for name, item in groups for model in plan.models])
    lines += _table(["group", "orders", "orders with in-play before the pull"],
                    [[name, item["orders"], item["orders_in_play_before_the_pull"]] for name, item in groups])
    lines += ["## B14 Moves with no value per cell and window (runners), by reason", ""]
    return lines + _table(["cell", "window", *MOVE_COUNTS],
                          [[cell, window, *found.values()] for cell, tables in value["cells"].items()
                           for window, found in tables["move_undefined"].items()])


def _fast_rows(found, rates=True):
    """[(name, figure)] of one group's fast figures at one BSP end, in points of stake (rates: with the hit rate,
    printed as a percentage of fills)."""
    rows = [("all placed: g", found["placed"]), ("the mid at t: g", found["mid"])]
    for model, item in found["models"].items():
        flag = " (all placed)" if item[FILLED_IS_ALL_PLACED] else ""
        rows += [(f"{model} filled{flag}: {what}", item["filled"][what]) for what in MEASURES + (NET_CLOSABLE,)]
        rows.append((f"{model}: value per placed order ({NET_CLOSABLE})", item["value_per_placed"]))
        if rates:
            rows.append((f"{model}: hit rate (percent of fills with g above 0)", item[HIT_RATE]))
        rows += [(f"{model}: realised, {kind}", item["realised"][kind]) for kind in ("rounded", "unrounded")]
        rows += [(f"{model}: {NET_CLOSABLE}, price band {name}", item[PRICE_BANDS][name]) for name in PRICE_BAND_NAMES
                 if item[PRICE_BANDS][name]["bets"]]
    return rows


def _fast_figure_lines(ends, given):
    """One group's fast figures at both BSP ends: given, the figures in points of stake, CLV in ticks and the gap;
    else counts only (n, race dates and the reason), with no statistic's name. Then the counts of both."""
    first = ends[AS_WRITTEN]
    if given:
        listed = [_fast_rows(ends[end]) for end in BSP_ENDS]
        columns = [f"{name} ({end})" for end in BSP_ENDS for name in ("mean", "SD", "CI low", "CI high")]
        lines = _table(["figure", "n", "race dates", *columns, "note"],
                       [[name, item["bets"], item["race_dates"], *_figure_cells(item), *_figure_cells(other),
                         item["reason"]] for (name, item), (_, other) in zip(*listed)])
        ticks = [[f"{model}: {part}", ends[AS_WRITTEN]["models"][model][CLV_TICKS][part]["bets"],
                  *(value for end in BSP_ENDS for value in (ends[end]["models"][model][CLV_TICKS][part]["mean"],
                                                            ends[end]["models"][model][CLV_TICKS][part]["ci_low"],
                                                            ends[end]["models"][model][CLV_TICKS][part]["ci_high"])),
                  ends[AS_WRITTEN]["models"][model][CLV_TICKS][part]["reason"]]
                 for model in first["models"] for part in (FILLED, "unfilled")]
        lines += ["CLV of the entry against BSP in CLASSIC ticks (above 0 is good for the order)", ""]
        lines += _table(["orders", "n", *(f"{name} ({end})" for end in BSP_ENDS for name in ("mean", "CI low",
                                                                                            "CI high")), "note"],
                        ticks)
        gaps = [[model, *(part for end in BSP_ENDS for part in _gap_cells(ends[end]["models"][model]["gap"]))]
                for model in first["models"]]
        lines += ["Gap: filled less unfilled, gross g (descriptive, no p)", ""]
        lines += _table(["model", *(f"{name} ({end})" for end in BSP_ENDS for name in (
            "filled", "unfilled", "gap", "CI low", "CI high", "note"))], gaps)
    else:
        lines = _table(["figure", "n", "race dates", "note"],
                       [[name, item["bets"], item["race_dates"], item["reason"]]
                        for name, item in _fast_rows(first, rates=False)])
    lines += _table(["model", "fills", "fill race dates", FILLED_IS_ALL_PLACED, "closable fills", "not closable fills",
                     *(f"realised left out ({end}): {why}" for end in BSP_ENDS for why in ("dead_heat",
                                                                                            "dead_heat_unknown")),
                     *(f"{BSP_OFF_THE_LADDER} ({end})" for end in BSP_ENDS)],
                    [[model, item["fills"], item["fill_race_dates"], item[FILLED_IS_ALL_PLACED],
                      *item[CLOSABLE_FILLS].values(),
                      *((ends[end]["models"][model]["realised"]["rounded"]["left_out"] or {}).get(why)
                        for end in BSP_ENDS for why in ("dead_heat", "dead_heat_unknown")),
                      *(ends[end]["models"][model][CLV_TICKS][BSP_OFF_THE_LADDER] for end in BSP_ENDS)]
                     for model, item in first["models"].items()])
    return lines + _table(["pair of models", "closable_differs"], list(first["closable_differs"].items()))


def _fast_explore_lines(plan, value):
    """A fast pass's E1 to E3, each headed exploratory with its c and sqrt(2 ln c), every net figure headed as the
    expectation given BSP, the realised figure and closable_differs with their notes."""
    lines = []
    for title, key in (("E1 Intent-to-treat figures per cell", "figures"), ("E2 The same by half-year", "half_years")):
        table = value[key]
        lines += [f"## {title} (points of stake; {table['heading']}, descriptive, no p)", "",
                  f"Cells in this table: c = {table['cells']}; sqrt(2 ln c) = {table['sqrt_2_ln_c']}. A cell whose "
                  f"{fills.PESSIMISTIC} fills fail the size gate gives counts only. Every net figure ({NET_CLOSED}, "
                  f"{NET_HELD}, {NET_CLOSABLE}): {table['deciding_heading']}. {table['realised_note']}. "
                  f"{table['differs_note']}. The reading rule's ends: the claim end is "
                  f"{table['reading_ends']['claim_end']}, the kill end {table['reading_ends']['kill_end']}.", ""]
        for cell, by_label in table["by_cell"].items():
            for label, group in by_label.items():
                head = f"### {cell}, {label}: {table['heading']}"
                given = value["figures"]["size_gate"][cell][label]["figures_given"]
                if key == "figures":
                    classes, size = value["cells"][cell]["classes"][label], table["size_gate"][cell][label]
                    lines += [head, "", "Classes: " + ", ".join(f"{name} {count}" for name, count in classes.items())
                              + f". Size gate: {size['fills']} {size['model']} fills on {size['fill_race_dates']} "
                              f"race dates; figures given: {'yes' if given else 'no'}.", ""]
                    lines += _fast_figure_lines(group, given)
                else:
                    for half, ends in group.items():
                        lines += [f"{head}, {half}", ""] + _fast_figure_lines(ends, given)
    table = value["bsp_book"]
    keys = next((list(item["quantiles"]) for item in table["by_label"].values() if item["quantiles"]), [])
    lines += [f"## E3 The sum of 1 / BSP per market with a scored order (nearest-rank quantiles; {table['heading']})",
              "", f"Cells in this table: c = {table['cells']}; sqrt(2 ln c) = {table['sqrt_2_ln_c']}.", ""]
    return lines + _table(["label", "markets", *keys], [[label, item["markets"], *((item["quantiles"] or {}).get(q)
                                                                                    for q in keys)]
                                                        for label, item in table["by_label"].items()])


def markdown(plan, value):
    """summary.md: the summary's tables as Markdown. Outcomes are printed as points of stake (100 x g). A new id's
    tables differ from close-v1's as the module docstring says; the explore pass's add E1 to E3. A fast summary
    prints its machinery checks first, its own B1, no minute block in B4, its tables B10 to B14 and its E1 to E3."""
    registered, early = value["pass"] == REGISTERED, _early(plan)
    cells, labels, read = value["cells"], list(plan.labels), list(plan.labels_read)
    lines = [f"# Study {value['study']}: {value['pass']} pass", "",
             f"- registration sha256: `{value['registration_sha256']}`",
             f"- split file sha256: `{value['partitions_sha256']}`", f"- input sha256: `{value['input_sha256']}`",
             f"- index records: {value['records']}; markets replayed: {value['replayed_markets']}",
             f"- partition labels: explore {labels[0]}, test {labels[1]}",
             "- unit: g is a fraction of the backer's stake; these tables print 100 x g as points of stake", ""]
    if early:
        lines[-2:-2] = [f"- labels read: {', '.join(read)}; of any other label, index record counts only"]
    if value["pass"] == BLIND:                      # an explore summary has no withheld text
        lines += [f"Withheld in this pass: {value['withheld']}.", ""]
    elif plan.fast:                                 # the machinery checks first (§2.5)
        lines += ["A fast pass: the machinery checks first, then the tables of a blind look and the fast tables, then "
                  "E1 to E3: exploratory and descriptive, on the explore label alone.", ""] + _check_lines(value)
    elif value["pass"] == EXPLORE:
        lines += ["Every table of the blind look, value for value, then E1 to E3: exploratory and descriptive, on the "
                  "labels read alone.", ""]
    lines += [f"A group with fewer than {plan.min_values} values gives its counts only. A cell is one rule file at "
              "one of its times, named rule@seconds before the off.", ""]
    coverage = value["coverage"]
    if plan.fast:                                   # G6: of a label not read, one total
        explore = plan.explore_label
        lines += [f"## B1 Coverage by month (records of the registered tier and orders placed, label {explore} "
                  "alone)", ""]
        lines += _table(["month", f"records {explore}", f"orders {explore}"],
                        [[month, row["records"][explore], row["orders"][explore]]
                         for month, row in coverage["by_month"].items()])
        lines += [f"Records with no anchor: {coverage['records_without_an_anchor']}. Records with an anchor on a label "
                  f"not read, or with no registered label, in all: {coverage['records_on_labels_not_read']}.", ""]
    else:
        lines += ["## B1 Coverage by month (records of the registered tier and orders placed, by partition label)", ""]
        lines += _table(["month", *(f"records {label}" for label in labels), "records, no registered label",
                         *(f"orders {label}" for label in read)],
                        [[month, *(row["records"][label] for label in labels), row["records"][NO_LABEL],
                          *(row["orders"][label] for label in read)] for month, row in coverage["by_month"].items()])
        lines += [f"Records with no anchor: {coverage['records_without_an_anchor']}. Records with an anchor and no "
                  f"registered label: {coverage['records_without_a_registered_label']}.", ""]
    lines += ["## B2 Codes per cell (each column adds up to the record count)", ""]
    lines += _table(["code", *cells], [[code, *(tables["codes"][code] for tables in cells.values())]
                                       for code in plan.codes + (ISSUED,)
                                       if any(tables["codes"][code] for tables in cells.values())])
    lines += ["## B3 Classes of placed orders per cell and label", ""]
    lines += _table(["cell", "label", *CLASSES], [[cell, label, *classes.values()] for cell, tables in cells.items()
                                                  for label, classes in tables["classes"].items()])
    lines += ["## B4 Fills of scored orders per cell, label and model (counts)", ""]
    within = [f"within {seconds} s of t" for seconds in plan.within_s]
    # B4's minute block is the new ids' alone (R5, V5): a fast summary has its own minute table (B10).
    minutes = [(str(minute), kind) for minute in (SECOND, THIRD) for kind in FILL_KINDS] \
        if plan.study in EARLY_IDS else []
    lines += _table(["cell", "label", "model", "scored", "fills", "fill rate", *FILL_KINDS, *within,
                     *(f"after the hour: {kind}" for kind in FILL_KINDS), "left the market not OPEN",
                     "turned the market in play", *(f"ended by {end}" for end in ENDS),
                     *(f"minute {minute} after the hour: {kind}" for minute, kind in minutes)],
                    [[cell, label, model, item["scored"], item["fills"], item["fill_rate"], *item["by_kind"].values(),
                      *item["within_s_of_t"].values(), *item["after_the_hour"].values(),
                      item["left_the_market_not_open"], item["turned_the_market_in_play"],
                      *item["ended_by"].values(),
                      *(item["minutes_after_the_hour"][minute][kind] for minute, kind in minutes)]
                     for cell, tables in cells.items() for label, models in tables["fills"].items()
                     for model, item in models.items()])
    lines += ["Fills of the orders that are not scored, by class (a class with none is left out here, not in "
              "summary.json)", ""]
    lines += _table(["cell", "label", "class", *plan.models],
                    [[cell, label, klass, *counts.values()] for cell, tables in cells.items()
                     for label, classes in tables["fills_not_scored"].items() for klass, counts in classes.items()
                     if any(counts.values())])
    if early:
        lines += ["## B5 Spread of gross g at the BSP as written, on the labels read (points of stake)", "",
                  "SD and nearest-rank ranges q(1 - a) - q(a) over each model's filled part; where fewer than "
                  f"{plan.min_values} scored orders are unfilled, neither is given ({FILLED_IS_ALL_PLACED}).", ""]
    else:
        lines += ["## B5 Spread of gross g at the BSP as written, both labels pooled (points of stake)", "",
                  "SD and nearest-rank ranges q(1 - a) - q(a) over each model's filled part; where fewer than "
                  f"{plan.min_values} scored orders are unfilled, every scored order stands in "
                  f"({FILLED_IS_ALL_PLACED}).", ""]
    filled = [(cell, model, item) for cell, tables in cells.items()
              for model, item in tables["spread_of_g"]["filled"].items()]
    ranges = next((list(item["ranges"]) for _, _, item in filled if item["ranges"]), [])
    rows = []
    for cell, tables in cells.items():
        if not early:                               # a new id gives no SD over all scored orders
            everything = tables["spread_of_g"]["all_scored"]
            rows.append([cell, "all scored orders", everything["values"], everything["race_dates"],
                         _points(everything["sd"]), *[None] * len(ranges), None])
        rows += [[cell, f"{model} filled", item["values"], item["race_dates"], _points(item["sd"]),
                  *(_points((item["ranges"] or {}).get(name)) for name in ranges), item[FILLED_IS_ALL_PLACED]]
                 for model, item in tables["spread_of_g"]["filled"].items()]
    lines += _table(["cell", "group", "values", "race dates", "SD", *ranges, FILLED_IS_ALL_PLACED], rows)
    queues = [(cell, tables["queue_at_t"]) for cell, tables in cells.items()]
    keys = next((list(item["queue_quantiles"]) for _, item in queues if item["queue_quantiles"]), [])
    lines += ["The size shown at t ahead of the order, at its price on its own side (GBP of backer's stake; the "
              f"{plan.models[-1]} model's queue; nearest-rank quantiles over every placed order)", ""]
    lines += _table(["cell", "orders", *keys], [[cell, item["orders"], *((item["queue_quantiles"] or {}).get(q)
                                                                         for q in keys)] for cell, item in queues])
    seconds = [(cell, model, item) for cell, tables in cells.items()
               for model, item in tables["seconds_to_fill"].items()]
    keys = next((list(item["seconds_quantiles"]) for _, _, item in seconds if item["seconds_quantiles"]), [])
    lines += ["Seconds from t to each fill of a scored order (nearest-rank quantiles)", ""]
    lines += _table(["cell", "model", "fills", *keys],
                    [[cell, model, item["fills"], *((item["seconds_quantiles"] or {}).get(q) for q in keys)]
                     for cell, model, item in seconds])
    if not early:                                   # a new id has no B6: the detectable effect describes a test
        lines += [f"## B6 Detectable effect on the test label (points of stake; {INDEPENDENCE})", ""]
        lines += _table(["cell", "model", "values", "race dates", FILLED_IS_ALL_PLACED, "detectable effect"],
                        [[cell, model, item["detectable"]["values"], item["detectable"]["race_dates"],
                          item["detectable"][FILLED_IS_ALL_PLACED], _points(item["detectable"]["effect"])]
                         for cell, model, item in filled])
    precision = value["bsp_precision"]
    lines += ["## B7 Diagnostics (counts)", "",
              f"- scored orders' BSPs that are whole hundredths: {precision['scored_bsps_whole_hundredths']}; that "
              f"are not: {precision['scored_bsps_not_whole_hundredths']}; the two BSP ends decide apart: "
              f"{'yes' if precision['bsp_ends_decide_apart'] else 'no'}", ""]
    flags = sorted({name for tables in cells.values() for name in tables["flags_missing_at_t"]})
    placed = ["orders placed in the after-the-hour seconds"] if early else []
    lines += _table(["cell", *plan.trd_counts, *placed, "scored orders with a batch begun with no status",
                     "null values", *(f"no null: {reason}" for reason in NULL_UNDEFINED),
                     *(f"no value at t: {name}" for name in flags)],
                    [[cell, *tables["trd_changes"].values(),
                      *(tables["orders_placed_in_the_hour_seconds"] for _ in placed),
                      tables["scored_orders_with_a_batch_begun_with_no_status"],
                      tables["null_values"]["defined"], *tables["null_values"]["undefined"].values(),
                      *(tables["flags_missing_at_t"].get(name) for name in flags)]
                     for cell, tables in cells.items()])
    if early:
        lines += _early_lines(plan, cells)
    if plan.fast:
        lines += _machinery_lines(plan, value)
    if registered:
        lines += _registered_lines(value)
    elif value["pass"] == EXPLORE:
        lines += _fast_explore_lines(plan, value) if plan.fast else _explore_lines(value)
    return "\n".join(lines).rstrip("\n") + "\n"


# ---------------------------------------------------------------------------------------------------------------
# The run

def _counts(plan, tally):
    return {"records": tally.records, "replayed_markets": tally.replayed,
            "issued": {_key(cell): tally.codes[cell][ISSUED] for cell in plan.cells},
            "scored": {_key(cell): len(scored(_both(plan, tally, cell))) for cell in plan.cells}}


def _tally(plan, root, archives, limits):
    """Decide every record of every archive (module docstring)."""
    tally, seen = Tally(plan), set()
    for archive in archives:
        sha = archive["archive_sha256"]
        for record in _records(archive):
            months = _record_months(record, sha)
            if months is None:
                raise RunRefused(f"input:{sha}:malformed_record")
            if any(month_gate(month) for month in months):
                raise RunRefused(f"input:{sha}:stale_gate")
            # Registration data.duplicates: a market of the registered tier is keyed by its id alone.
            tier, market_id = record["provenance"]["plan"], record["market_id"]
            key = market_id if tier in plan.tiers else (tier, market_id)
            if key in seen:
                raise RunRefused(f"input:{sha}:duplicate_market")
            seen.add(key)
            try:
                facts = record_facts(plan, record)
            except RECORD_ERRORS:
                raise RunRefused(f"input:{sha}:malformed_record") from None
            decide_record(plan, record, facts, root, archive["manifest"], tally, limits)
    return tally


def run_study(study, pass_name, registration_sha256_hex, archive_root, out, code_commit, ledger, ledger_head,
              blind_receipt=None, registration_path=None, limits=DEFAULT_LIMITS, clock=utc_now_text, store=None):
    """One pass (module docstring); a JSON-ready result: completed with counts and hashes, refused with problems,
    or failed with a failure code. store: the content-addressed store's root (--store), for a fast registration in
    the store alone."""
    started_at = clock()
    try:
        if study not in STUDIES:
            _refuse("study:not_supported")
        if pass_name not in STUDY_PASSES[study]:      # close-v1 runs no explore pass, a new id no registered pass
            _refuse("pass:not_supported")
        plan, split_raw, alpha, store = _plan(study, registration_path, registration_sha256_hex, store)
        if plan.study != study:                     # close-v1's file under a new id's name, or the reverse
            _refuse("study:not_the_registrations")
        chains, folders = None, set()
        if plan.fast:
            if blind_receipt is not None:           # G5: no fast pass anchors on, or passes for, another's look
                _refuse("blind_receipt:not_for_a_fast_pass")
            # G11, C1: every rule file's parents, back to a file in Git, each of the fast id's lineage
            chains, folders = _parent_chains(plan, store)
        if not (isinstance(code_commit, str) and COMMIT_RE.fullmatch(code_commit)):
            _refuse("code_commit:malformed")
        guarded = _guarded(archive_root, "archive_root")
        root, problems = check_data_root(guarded)
        if problems:
            _refuse(problems[0].replace("data_root:", "archive_root:", 1))
        target = _out_path(out, root)
        if pass_name == BLIND and blind_receipt is not None:
            _refuse("blind_receipt:not_for_the_blind_pass")
        blind = None if pass_name == BLIND or plan.fast else _blind_receipt(blind_receipt, plan)
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
        for archive in archives:                    # G12: no index line of another tier is parsed by a fast pass
            tiers = archive["manifest"].get("plans")
            if plan.fast and (not isinstance(tiers, list) or any(tier not in plan.tiers for tier in tiers)):
                _refuse("input:tier_not_registered")
        if blind is not None and blind[0].get("input_sha256") != input_sha256:
            _refuse("blind_receipt:another_input")
        request = PassRequest(                      # the labels the plan reads: both for close-v1
            study=plan.study, lineage=plan.lineage, kind=LOOK, partitions=plan.labels_read,
            partitions_sha256=plan.split.sha256, registration_sha256=plan.sha256, input_sha256=input_sha256,
            source_descriptor_sha256=sources_sha256, code_commit=code_commit)
        if pass_name == REGISTERED:
            request = replace(request, kind=REGISTERED_KIND, m=plan.family_m, alpha=alpha)
        elif pass_name == EXPLORE:                  # m 0 and alpha "0": nothing is tested
            request = replace(request, kind=EXPLORE_KIND)
        if plan.fast and (request.kind != EXPLORE_KIND or request.partitions != (plan.explore_label,)
                          or plan.split.role(plan.explore_label) != EXPLORE_ROLE):
            _refuse("fast:not_explore_only")        # G4: the request names the explore label and nothing else
        try:
            handle, stored = _ledger(ledger, ledger_head, root, target, request, split_raw, blind and blind[2])
            # One explore pass per new id: a repeat runs again; any other explore entry of this study refuses. A
            # fast id runs many, each a registration of its own, up to its lineage's explore budget (the ledger).
            if pass_name == EXPLORE and not plan.fast and stored is None and any(
                    entry["kind"] == EXPLORE_KIND and entry["study"] == plan.study for entry in handle.entries()):
                _refuse("explore:already_run")
            # G11, C1: a parent in the store is a rule file of a pass that ran, its folder a fast explore line's
            # registration in this ledger.
            if folders - {entry["registration_sha256"] for entry in handle.entries()
                          if entry["kind"] == EXPLORE_KIND and entry["study"] in FAST_IDS}:
                _refuse("rule:parent_pass_not_in_the_ledger")
        except LedgerError as error:
            raise StudyRefused(str(error)) from None
    except StudyRefused as error:
        return {"status": "refused", "problems": [str(error)]}
    try:
        tally = _tally(plan, root, archives, limits)
    except (RunRefused, ReplayProblem, StudyRefused) as error:
        return {"status": "failed", "failure": _failure(error)}
    value = summary(plan, tally, pass_name, input_sha256, request.input_sha256)
    summary_bytes = canonical_json(value) + b"\n"
    markdown_bytes = markdown(plan, value).encode("utf-8")
    counts = _counts(plan, tally)
    outputs = {SUMMARY_JSON: {"sha256": hashlib.sha256(summary_bytes).hexdigest(), "bytes": len(summary_bytes)},
               SUMMARY_MD: {"sha256": hashlib.sha256(markdown_bytes).hexdigest(), "bytes": len(markdown_bytes)}}
    lock_left = False
    try:                                            # the one ledger entry, before any file or stdout write
        head = handle.record(request, split_raw, outputs[SUMMARY_JSON]["sha256"], COMPLETED, clock)
    except UnlockError as error:                    # the entry was written and confirmed; the lock file is left
        head, lock_left = error.head, True
    except LedgerError as error:
        return {"status": "failed", "failure": str(error)}
    # From here the entry stands: whatever happens, the result names the head, and the re-run is a repeat from it.
    head, repeat = head_text(head), stored is not None
    try:
        own = head if stored is None else head_text((stored["seq"], stored["entry_sha256"]))
        passes = handle.counts(plan.lineage)
        receipt = {"kind": RECEIPT_KIND, "schema_version": SCHEMA_VERSION, "study": plan.study, "pass": pass_name,
                   "status": "completed", "registration_sha256": plan.sha256,
                   "partitions_sha256": plan.split.sha256, "input_sha256": input_sha256, "input": descriptor,
                   "code_commit": code_commit, "source_descriptor_sha256": sources_sha256,
                   "interpreter": "{0} {1}.{2}.{3}".format(sys.implementation.name, *sys.version_info[:3]),
                   "blind_receipt_sha256": None if blind is None else blind[1], "ledger_head": head,
                   "ledger_entry": own, "ledger_repeat": repeat, "ledger_counts": passes,
                   "bytes_walked": tally.bytes_walked, "bytes_drained": tally.bytes_drained, "counts": counts,
                   "outputs": outputs, "started_at": started_at, "recorded_at": clock()}
        if plan.fast:                               # S13 and C1: the receipt, never the summary (a repeat's hash)
            label = plan.explore_label
            receipt.update(
                explore_lines_on_label=sum(1 for entry in handle.entries() if entry["kind"] != LOOK
                                           and entry["lineage"] == plan.lineage and label in entry["partitions"]),
                explore_budget=plan.split.explore_budget(label), parent_chains=chains,
                registration_folder="research/studies" if store is None else f"{plan.sha256}/{STUDIES_FOLDER}",
                store_root=None if store is None else str(store))
        receipt_bytes = canonical_json(receipt) + b"\n"
        target = _out_path(out, root)
        _write(target, [(SUMMARY_JSON, summary_bytes), (SUMMARY_MD, markdown_bytes), (RECEIPT_JSON, receipt_bytes)])
    except (LedgerError, StudyRefused) as error:
        return {"status": "failed", "failure": str(error), "ledger_head": head, "ledger_lock_left": lock_left}
    except Exception as error:      # a fault of the code: its class name only (main), with the head that stands
        return {"status": "failed", "failure": f"internal:{type(error).__name__}", "ledger_head": head,
                "ledger_lock_left": lock_left}
    return {"status": "completed", "study": plan.study, "pass": pass_name, "registration_sha256": plan.sha256,
            "input_sha256": input_sha256, "summary_sha256": outputs[SUMMARY_JSON]["sha256"],
            "receipt_sha256": hashlib.sha256(receipt_bytes).hexdigest(), "ledger_head": head,
            "ledger_repeat": repeat, "ledger_lock_left": lock_left, "ledger_counts": passes, **counts}


def build_parser():
    parser = argparse.ArgumentParser(prog="python3 -m betangel_research.betfair.backtest.resting_study",
                                     description="Registered measurements close-v1 and close-early-v1, and the fast "
                                                 "explore passes of close-v2-explore, on local historical files; an "
                                                 "order here is simulated and nothing can reach an exchange.")
    parser.add_argument("--study", required=True, help=f"the study to run: one of {', '.join(STUDIES)}")
    parser.add_argument("--pass", dest="pass_name", required=True, choices=PASS_NAMES,
                        help="blind first, then registered (close-v1) or explore (a new id); a fast id: explore "
                             "alone")
    parser.add_argument("--registration-sha256", required=True, help="sha256 of the registration file's raw bytes")
    parser.add_argument("--archive-root", required=True, help="data root with manifests/, raw/ and index/")
    parser.add_argument("--out", required=True, help="a new directory outside every Git worktree and data tree")
    parser.add_argument("--code-commit", required=True, help="the 40-character commit the code was checked out at")
    parser.add_argument("--ledger", required=True, help="the study ledger's directory; it must exist")
    parser.add_argument("--ledger-head", required=True, help="the ledger's last head: none, or SEQ:SHA256")
    parser.add_argument("--blind-receipt", help="registered or explore pass: the blind pass's receipt.json (never "
                                                "for a fast id)")
    parser.add_argument("--registration", help="registration file (default: research/studies/<study>.json; a fast "
                                               "id's may also lie in the store, <store>/<its sha256>/studies/); the "
                                               "split file and rules/ are read beside its studies/ folder")
    parser.add_argument("--store", help="a fast id's registration in the store: the store's root, a directory of "
                                        "mode 0700 outside every Git worktree and data tree (never for another id)")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        result = run_study(args.study, args.pass_name, args.registration_sha256, args.archive_root, args.out,
                           args.code_commit, args.ledger, args.ledger_head, blind_receipt=args.blind_receipt,
                           registration_path=args.registration, store=args.store)
    except Exception as error:      # only the class name: a traceback or a message can print a key, and so an id
        result = {"status": "failed", "failure": f"internal:{type(error).__name__}"}
    sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
    return 0 if result["status"] == "completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
