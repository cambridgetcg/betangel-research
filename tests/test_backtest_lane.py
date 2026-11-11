"""The research lane's data split, study ledger and rule files (betangel_research.betfair.backtest.partition,
study_ledger and rulefile).

Everything here is synthetic: race dates are calendar text, ledger entries carry made-up hashes, every ledger lives
in a temporary directory that the test removes, and every view a rule reads is built by hand. No data root is read.
These tests show the split rule, the split file's checks, the ledger's chain, its head check and its budgets, the
rule file's checks and what decide answers for a given view. They do not show durability across a power loss
(nothing is synced), that an operator keeps one ledger file (a new directory starts every budget from zero), or
that a view built from real messages is right: no code here builds one, and decide trusts the view it is given.
"""

from dataclasses import FrozenInstanceError, fields, replace
from datetime import date, timedelta
from fractions import Fraction
import ast
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from betangel_research.betfair.backtest import movement, partition, rulefile, study_ledger
from betangel_research.betfair.backtest.contract import canonical_json
from betangel_research.betfair.backtest.partition import PartitionError, file_bytes, parse_split
from betangel_research.betfair.backtest.rulefile import (Decision, Intent, Market, Predicate, RuleError, RuleView,
                                                         RunnerView, Template, decide, parse_rule)
from betangel_research.betfair.backtest.study_ledger import (ENTRY_KEYS, LEDGER_FILE, PassRequest, StudyLedger,
                                                             head_text, identity, parse_head, verify)
from betangel_research.betfair.backtest.trial_ledger import (GENESIS, LOCK_FILE, LedgerError, UnlockError,
                                                             WriteUncertainError, entry_digest)
from betangel_research.betfair.backtest.trial_ledger import LEDGER_FILE as TRIAL_LEDGER_FILE

REPO = Path(__file__).resolve().parent.parent
SPLIT_FILE = REPO / "research" / "partitions-v1.json"
# The split file's sha256 as reviewed: any change to the file must change this line too (a new split).
SPLIT_SHA256 = "dafa34416d704b24f66b540f754fa7681ca8fcb188a2327cb72fd40340ee3f2e"
SPLIT = parse_split(SPLIT_FILE.read_bytes())
SALT = "betangel-research/partitions-v1/PRO-2015-05..2016-04"
COMMIT = "0123456789abcdef0123456789abcdef01234567"
CLOCK = "2026-11-11T12:00:00.000000Z"
RULES_DIR = REPO / "research" / "rules"
# Each rule file's sha256 as reviewed: any change to a file must change its line here too (a new rule file).
RULE_SHA256 = {"close-v1-back": "401a69f403a8651f919d43696fdcd367f203ec1babc9b36668935bfcbae9be24",
               "close-v1-lay": "fab9fbf5e99e38c3b14839526959608d8461cc3511b8a3fc2e06d429d963fb65",
               "inplay-v1-lay": "7a5dea217c5d4fec164e67f151d14e1f3f2fa45fe16b81ad31166c982ac1e9b6"}
RULES = {name: parse_rule((RULES_DIR / f"{name}.json").read_bytes()) for name in RULE_SHA256}


def days(first, last):
    day, end = date.fromisoformat(first), date.fromisoformat(last)
    while day <= end:
        yield day.isoformat()
        day += timedelta(days=1)


def sha(text):
    return hashlib.sha256(text.encode("ascii")).hexdigest()


class PartitionShares(unittest.TestCase):
    def test_the_file_is_the_reviewed_one_in_its_one_byte_form(self):
        raw = SPLIT_FILE.read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), SPLIT_SHA256)
        self.assertEqual(SPLIT.sha256, SPLIT_SHA256)
        raw.decode("ascii")
        self.assertEqual(raw, file_bytes(json.loads(raw)))
        self.assertTrue(raw.endswith(b"}\n") and not raw.endswith(b"\n\n"))
        self.assertEqual(json.loads(raw)["schema"], "betangel.partitions/v1")
        self.assertEqual(json.loads(raw)["ranges"][0]["salt"], SALT)

    def test_the_pro_year_splits_238_to_128(self):
        labels = {day: SPLIT.label(day) for day in days("2015-05-01", "2016-04-30")}

        def counts(label, half):
            return sum(1 for day, found in labels.items() if found == label and half in (None, SPLIT.half(day)))

        self.assertEqual(len(labels), 366)
        self.assertEqual((counts("PRO-D", None), counts("PRO-V", None)), (238, 128))
        self.assertEqual((counts("PRO-D", "H1"), counts("PRO-V", "H1")), (120, 64))
        self.assertEqual((counts("PRO-D", "H2"), counts("PRO-V", "H2")), (118, 64))
        self.assertEqual([labels[day] for day in ("2015-05-01", "2015-05-02", "2015-12-25")],
                         ["PRO-D", "PRO-V", "PRO-D"])

    def test_the_rule_is_the_written_hash(self):
        for day in days("2015-05-01", "2016-04-30"):
            u = int(hashlib.sha256((SALT + "|" + day).encode("ascii")).hexdigest()[:8], 16)
            self.assertEqual(SPLIT.label(day), "PRO-V" if u % 3 == 0 else "PRO-D", day)

    def test_fixed_years_and_dates_with_no_label(self):
        for day, label in (("2023-01-01", "BASIC-D"), ("2023-12-31", "BASIC-D"), ("2024-06-01", "BASIC-V"),
                           ("2025-07-15", "BASIC-T"), ("2026-01-01", "SEALED"), ("2026-12-31", "SEALED"),
                           ("2027-01-01", None), ("2016-05-01", None), ("2015-04-30", None), ("2022-12-31", None),
                           ("2019-06-01", None)):
            with self.subTest(day=day):
                self.assertEqual(SPLIT.label(day), label)
        for year in ("2023", "2024", "2025", "2026"):
            self.assertEqual(len({SPLIT.label(day) for day in days(f"{year}-01-01", f"{year}-12-31")}), 1)

    def test_half_years(self):
        for day, half in (("2015-05-01", "H1"), ("2015-10-31", "H1"), ("2015-11-01", "H2"), ("2016-04-30", "H2"),
                          ("2016-05-01", None), ("2015-04-30", None), ("2024-06-01", None)):
            with self.subTest(day=day):
                self.assertEqual(SPLIT.half(day), half)

    def test_a_race_date_must_be_iso_text(self):
        for bad in ("2015-5-1", "20150501", "2015-02-30", "2015-05-01T00:00:00", "", None, 20150501,
                    date(2015, 5, 1)):
            with self.subTest(bad=bad):
                for function in (SPLIT.label, SPLIT.half):
                    with self.assertRaises(PartitionError) as caught:
                        function(bad)
                    self.assertEqual(str(caught.exception), "partition:race_date:not_a_date")

    def test_roles_budgets_and_which_labels_a_registration_may_name(self):
        self.assertEqual(SPLIT.roles, {"PRO-D": "explore", "PRO-V": "holdout", "BASIC-D": "explore",
                                       "BASIC-V": "holdout", "BASIC-T": "sealed", "SEALED": "never"})
        self.assertEqual(SPLIT.explore_budgets, {"PRO-D": 20})
        self.assertEqual(SPLIT.alpha_lifetimes, {"PRO-V": Fraction(1, 10)})
        self.assertEqual((SPLIT.explore_budget("PRO-D"), SPLIT.alpha_lifetime("PRO-V"), SPLIT.role("PRO-V")),
                         (20, Fraction(1, 10), "holdout"))
        self.assertEqual((SPLIT.explore_budget("BASIC-D"), SPLIT.alpha_lifetime("BASIC-V"), SPLIT.role("NOPE")),
                         (None, None, None))
        self.assertEqual(SPLIT.max_looks, 2)
        self.assertEqual(sorted(SPLIT.prior_exposure), ["lp-v1", "movement-v1", "p0", "suite-counts"])
        self.assertEqual({label: SPLIT.label_problem(label) for label in list(SPLIT.roles) + ["NOPE", None]},
                         {"PRO-D": None, "PRO-V": None, "BASIC-D": "no_budget", "BASIC-V": "no_budget",
                          "BASIC-T": "role_sealed", "SEALED": "role_never", "NOPE": "label_unknown",
                          None: "label_unknown"})


class PartitionParse(unittest.TestCase):
    def setUp(self):
        self.value = json.loads(SPLIT_FILE.read_bytes())

    def assertRefused(self, code, change=None, raw=None):
        value = copy.deepcopy(self.value)
        if change is not None:
            change(value)
        with self.assertRaises(PartitionError) as caught:
            parse_split(file_bytes(value) if raw is None else raw)
        self.assertEqual(str(caught.exception), f"partition:{code}")

    def test_a_copy_parses_and_a_changed_salt_is_another_split(self):
        self.assertEqual(parse_split(file_bytes(self.value)), SPLIT)
        self.value["ranges"][0]["salt"] = SALT + "/b"
        other = parse_split(file_bytes(self.value))
        self.assertNotEqual(other.sha256, SPLIT_SHA256)        # nothing but the pinned hash tells the two apart
        self.assertNotEqual([other.label(day) for day in days("2015-05-01", "2015-06-30")],
                            [SPLIT.label(day) for day in days("2015-05-01", "2015-06-30")])

    def test_unknown_and_missing_keys(self):
        cases = (("top", lambda v: v.update(note="x")), ("top", lambda v: v.pop("max_looks")),
                 ("ranges[0]", lambda v: v["ranges"][0].update(label="PRO-D")),
                 ("ranges[1]", lambda v: v["ranges"][1].update(salt=SALT)),
                 ("ranges[1]", lambda v: v["ranges"][1].pop("last")),
                 ("labels", lambda v: v["labels"].update(EXTRA={"role": "explore"})),
                 ("labels", lambda v: v["labels"].pop("SEALED")),
                 ("labels.PRO-D", lambda v: v["labels"]["PRO-D"].update(alpha_lifetime="0.10")),
                 ("labels.PRO-V", lambda v: v["labels"]["PRO-V"].update(explore_budget=20)),
                 ("labels.SEALED", lambda v: v["labels"]["SEALED"].update(explore_budget=1)),
                 ("halves", lambda v: v["halves"].update(H3=v["halves"]["H2"])),
                 ("halves.H1", lambda v: v["halves"]["H1"].update(note="x")))
        for place, change in cases:
            with self.subTest(place=place):
                self.assertRefused(f"{place}:keys_not_the_expected_set", change)

    def test_ranges_must_be_ordered_and_apart(self):
        def swap(value):
            value["ranges"][1], value["ranges"][2] = value["ranges"][2], value["ranges"][1]
        cases = (("ranges[1]:not_after_the_one_before", lambda v: v["ranges"][1].update(first="2016-04-30")),
                 ("ranges[1]:not_after_the_one_before", lambda v: v["ranges"][1].update(first="2015-06-01")),
                 ("ranges[2]:not_after_the_one_before", swap),
                 ("ranges[4]:ends_before_it_starts", lambda v: v["ranges"][4].update(last="2025-12-31")),
                 ("ranges[3]:not_a_date", lambda v: v["ranges"][3].update(first="2025-02-30")),
                 ("ranges[3]:not_a_date", lambda v: v["ranges"][3].update(last=20251231)),
                 ("ranges:not_a_list", lambda v: v.update(ranges=[])),
                 ("ranges:not_a_list", lambda v: v.update(ranges={"0": v["ranges"][0]})),
                 ("ranges[1]:rule_not_supported", lambda v: v["ranges"][1].update(rule="random")),
                 ("ranges[1]:rule_not_supported", lambda v: v["ranges"].insert(1, "2016")),
                 ("ranges:label_used_twice", lambda v: v["ranges"][2].update(label="BASIC-D")),
                 ("ranges:label_used_twice", lambda v: v["ranges"][0].update(label_otherwise="PRO-V")),
                 ("ranges[1]:label_malformed", lambda v: v["ranges"][1].update(label="basic d")))
        for code, change in cases:
            with self.subTest(code=code):
                self.assertRefused(code, change)
        self.value["ranges"][1]["first"] = "2016-05-01"                    # the day after the range before: apart
        self.assertEqual(parse_split(file_bytes(self.value)).label("2016-05-01"), "BASIC-D")

    def test_a_bad_salt_or_residue(self):
        for salt in ("", "short", SALT + " x", SALT + "|x", SALT + "é", "x" * 129, 7, None):
            with self.subTest(salt=salt):
                self.assertRefused("ranges[0]:salt_malformed", lambda v: v["ranges"][0].update(salt=salt))
        for residue in (3, -1, True, "0", 0.0, None):
            with self.subTest(residue=residue):
                self.assertRefused("ranges[0]:residue_not_below_modulus",
                                   lambda v: v["ranges"][0].update(residue=residue))
        for modulus in (1, 65, True, "3", 3.0):
            with self.subTest(modulus=modulus):
                self.assertRefused("ranges[0]:not_an_int_in_range", lambda v: v["ranges"][0].update(modulus=modulus))

    def test_roles_budgets_halves_looks_and_exposure(self):
        cases = (("labels.PRO-D:role_not_supported", lambda v: v["labels"]["PRO-D"].update(role="train")),
                 ("labels.PRO-D:role_not_supported", lambda v: v["labels"].update({"PRO-D": "explore"})),
                 ("labels.PRO-D:not_an_int_in_range", lambda v: v["labels"]["PRO-D"].update(explore_budget=0)),
                 ("labels.PRO-D:not_an_int_in_range", lambda v: v["labels"]["PRO-D"].update(explore_budget=20.0)),
                 ("labels.PRO-V:not_a_decimal_rate", lambda v: v["labels"]["PRO-V"].update(alpha_lifetime=0.1)),
                 ("labels.PRO-V:not_a_decimal_rate", lambda v: v["labels"]["PRO-V"].update(alpha_lifetime="1.0")),
                 ("labels.PRO-V:not_a_decimal_rate", lambda v: v["labels"]["PRO-V"].update(alpha_lifetime="0.000")),
                 ("halves.H2:not_after_the_one_before", lambda v: v["halves"]["H2"].update(first="2015-10-31")),
                 ("halves.H1:ends_before_it_starts", lambda v: v["halves"]["H1"].update(last="2015-04-30")),
                 ("halves:keys_not_the_expected_set", lambda v: v.update(halves={"H1": v["halves"]["H1"]})),
                 ("halves:not_an_object", lambda v: v.update(halves=[])),
                 ("max_looks:not_an_int_in_range", lambda v: v.update(max_looks=0)),
                 ("max_looks:not_an_int_in_range", lambda v: v.update(max_looks=True)),
                 ("prior_exposure:not_ids_with_a_sentence_each", lambda v: v.update(prior_exposure={})),
                 ("prior_exposure:not_ids_with_a_sentence_each", lambda v: v["prior_exposure"].update(p0=" ")),
                 ("prior_exposure:not_ids_with_a_sentence_each", lambda v: v["prior_exposure"].update({"P 0": "x"})),
                 ("prior_exposure:not_ids_with_a_sentence_each", lambda v: v.update(prior_exposure=["p0"])),
                 ("schema:not_supported", lambda v: v.update(schema="betangel.partitions/v2")),
                 ("split_by:not_supported", lambda v: v.update(split_by="utc_date")))
        for code, change in cases:
            with self.subTest(code=code):
                self.assertRefused(code, change)

    def test_only_the_one_byte_form_is_taken(self):
        raw = SPLIT_FILE.read_bytes()
        doubled = raw.replace(b'"max_looks": 2,', b'"max_looks": 9,\n  "max_looks": 2,', 1)
        self.assertEqual(json.loads(doubled), self.value)                   # a duplicate key json.loads lets through
        for code, data in (("file:not_canonical", canonical_json(self.value)), ("file:not_canonical", raw + b"\n"),
                           ("file:not_canonical", raw[:-1]), ("file:not_canonical", doubled),
                           ("file:not_json", raw[:-3]), ("file:not_json", b"\xff"),
                           ("file:not_json", raw.replace(b'"max_looks": 2', b'"max_looks": NaN', 1)),
                           ("file:not_bytes_or_too_large", raw.decode("ascii")),
                           ("file:not_bytes_or_too_large", b" " * ((1 << 20) + 1)), ("top:not_an_object", b"[]\n")):
            with self.subTest(code=code, size=len(data)):
                self.assertRefused(code, raw=data)


class LedgerCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.tmp = Path(self.temp.name).resolve()
        self.directory = self.tmp / "ledger"
        self.directory.mkdir()
        self.path = self.directory / LEDGER_FILE
        self.head = None

    def request(self, study="close-v1", kind="look", run=0, **changes):
        """A pass of a study; run numbers a different input, so a different identity."""
        values = dict(study=study, lineage=str(study).split("-")[0], kind=kind, partitions=("PRO-D", "PRO-V"),
                      partitions_sha256=SPLIT_SHA256, registration_sha256=sha(f"registration:{study}"),
                      input_sha256=sha(f"input:{run}"), source_descriptor_sha256=sha("source"), code_commit=COMMIT)
        if kind == "registered":
            values.update(m=2, alpha="0.02")
        if kind == "explore":
            values.update(partitions=("PRO-D",))
        values.update(changes)
        return PassRequest(**values)

    def open(self, **kwargs):
        return StudyLedger.open(self.directory, head_text(self.head), **kwargs)

    def run_pass(self, request, tables=None, result="completed", split=SPLIT):
        """One pass as a runner makes it: open with the last head, check, then record. Returns what check gave."""
        ledger = self.open()
        stored = ledger.check(request, split)
        self.head = ledger.record(request, split, sha("tables") if tables is None else tables, result, lambda: CLOCK)
        return stored

    def three(self):
        self.run_pass(self.request())
        self.run_pass(self.request("inplay-v1"))
        self.run_pass(self.request(kind="registered"))
        return self.head

    def lines(self):
        return self.path.read_bytes().split(b"\n")[:-1]

    def write_lines(self, lines):
        self.path.write_bytes(b"".join(line + b"\n" for line in lines))

    def rehashed(self, index, lines=None, **change):
        """The ledger's lines (or the given ones) with entry index changed and every hash from there on recomputed,
        as a careful forger would."""
        entries = [json.loads(line) for line in (self.lines() if lines is None else lines)]
        entries[index].update(change)
        for at in range(index, len(entries)):
            if at:
                entries[at]["prev_sha256"] = entries[at - 1]["entry_sha256"]
            entries[at]["entry_sha256"] = entry_digest(entries[at])
        return [canonical_json(entry) for entry in entries]

    def assertRefused(self, code, function, *args, **kwargs):
        with self.assertRaises(LedgerError) as caught:
            function(*args, **kwargs)
        self.assertEqual(str(caught.exception), code)
        return caught.exception


class LedgerChain(LedgerCase):
    def test_entries_chain_with_exactly_the_18_keys(self):
        head = self.three()
        entries = verify(self.path)
        self.assertEqual(len(ENTRY_KEYS), 18)
        self.assertEqual([sorted(e) for e in entries], [sorted(ENTRY_KEYS)] * 3)
        self.assertEqual([e["seq"] for e in entries], [0, 1, 2])
        self.assertEqual([e["prev_sha256"] for e in entries], [GENESIS] + [e["entry_sha256"] for e in entries[:-1]])
        self.assertEqual([e["entry_sha256"] for e in entries], [entry_digest(e) for e in entries])
        self.assertEqual(head, (2, entries[-1]["entry_sha256"]))
        self.assertEqual(self.lines(), [canonical_json(e) for e in entries])
        self.assertEqual(entries[0], {
            "alpha": "0", "code_commit": COMMIT, "entry_sha256": entries[0]["entry_sha256"],
            "input_sha256": sha("input:0"), "kind": "look", "ledger_schema": 1, "lineage": "close", "m": 0,
            "partitions": ["PRO-D", "PRO-V"], "partitions_sha256": SPLIT_SHA256, "prev_sha256": GENESIS,
            "recorded_at": CLOCK, "registration_sha256": sha("registration:close-v1"), "result": "completed",
            "seq": 0, "source_descriptor_sha256": sha("source"), "study": "close-v1", "tables_sha256": sha("tables")})
        self.assertEqual([(e["kind"], e["m"], e["alpha"]) for e in entries],
                         [("look", 0, "0"), ("look", 0, "0"), ("registered", 2, "0.02")])
        self.assertEqual(sorted(p.name for p in self.directory.iterdir()), [LEDGER_FILE])
        self.assertEqual(self.open().entries(), entries)

    def test_an_edit_a_deletion_and_a_reorder(self):
        self.three()
        lines = self.lines()
        edited = lines[1].replace(b'"study":"inplay-v1"', b'"study":"inplay-v2"')
        cases = (([lines[0], edited, lines[2]], "ledger:line 2:entry_hash_mismatch"),
                 (self.rehashed(1, study="inplay-v2")[:2] + [lines[2]], "ledger:line 3:prev_sha256_mismatch"),
                 ([lines[0], lines[2]], "ledger:line 2:seq_out_of_order"),
                 ([lines[1], lines[0], lines[2]], "ledger:line 1:seq_out_of_order"),
                 (lines[1:], "ledger:line 1:seq_out_of_order"))
        for changed, code in cases:
            with self.subTest(code=code):
                self.write_lines(changed)
                self.assertRefused(code, verify, self.path)
                self.assertRefused(code, self.open)

    def test_a_cut_line_and_an_empty_file(self):
        self.three()
        data = self.path.read_bytes()
        for cut, code in ((data[:-1], "ledger:line 3:truncated_line"), (data[:-40], "ledger:line 3:truncated_line"),
                          (b"", "ledger:empty")):
            with self.subTest(code=code, size=len(cut)):
                self.path.write_bytes(cut)
                self.assertRefused(code, verify, self.path)
                self.assertRefused(code, self.open)
                self.assertRefused(code, StudyLedger.open, self.directory, "none")

    def test_a_cut_at_a_line_boundary_needs_the_head_or_an_anchor(self):
        head = self.three()
        lines = self.lines()
        self.write_lines(lines[:2])
        shorter = verify(self.path)                                         # a valid chain on its own
        self.assertEqual(len(shorter), 2)
        self.assertRefused("ledger:truncated_before_anchor", verify, self.path, head)
        self.assertRefused("ledger:head_not_expected", self.open)
        cut_head = head_text((1, shorter[-1]["entry_sha256"]))
        self.assertRefused("ledger:truncated_before_anchor", StudyLedger.open, self.directory, cut_head, anchor=head)
        self.write_lines(self.rehashed(2, lines, tables_sha256=sha("other")))
        self.assertEqual(len(verify(self.path)), 3)                         # a rewritten tail: valid alone
        self.assertRefused("ledger:anchor_mismatch", verify, self.path, head)
        self.assertRefused("ledger:head_not_expected", self.open)
        self.path.unlink()
        self.assertRefused("ledger:truncated_before_anchor", StudyLedger.open, self.directory, "none", anchor=head)
        for bad in ([2], (2, head[1].upper()), "2:" + head[1], (True, head[1])):
            with self.subTest(bad=bad):
                self.assertRefused("ledger:anchor_malformed", StudyLedger.open, self.directory, "none", anchor=bad)

    def test_an_anchor_inside_the_chain_is_taken(self):
        self.run_pass(self.request())
        look = self.head
        self.run_pass(self.request("inplay-v1"))
        ledger = self.open(anchor=list(look))                               # as a receipt gives it back from JSON
        self.assertEqual(ledger.entries()[look[0]]["kind"], "look")

    def test_a_held_lock_refuses_before_the_read_and_at_the_append(self):
        self.run_pass(self.request())
        before = self.path.read_bytes()
        ledger = self.open()
        (self.directory / LOCK_FILE).write_bytes(b"")
        self.assertRefused("ledger:locked", self.open)
        self.assertRefused("ledger:locked", ledger.record, self.request(run=1), SPLIT, sha("t"), "completed",
                           lambda: CLOCK)
        self.assertEqual(self.path.read_bytes(), before)
        (self.directory / LOCK_FILE).unlink()                               # a person, after checking no pass runs
        ledger.record(self.request(run=1), SPLIT, sha("t"), "completed", lambda: CLOCK)
        self.assertEqual(len(verify(self.path)), 2)
        self.assertFalse((self.directory / LOCK_FILE).exists())

    def test_the_lock_names_its_pass_while_held(self):
        seen = []
        write = study_ledger._write_all

        def peeking(handle, data):
            seen.append(json.loads((self.directory / LOCK_FILE).read_text(encoding="ascii")))
            write(handle, data)

        with mock.patch.object(study_ledger, "_write_all", peeking):
            self.run_pass(self.request())
        self.assertEqual(seen, [{"code_identity": COMMIT, "kind": "look", "recorded_at": CLOCK,
                                 "trial_id": "close-v1"}])

    def test_no_null_and_no_float_enters(self):
        self.three()
        lines = self.lines()
        for key in ENTRY_KEYS:
            with self.subTest(key=key):
                entry = dict(json.loads(lines[2]), **{key: None})
                if key != "entry_sha256":
                    entry["entry_sha256"] = entry_digest(entry)
                self.write_lines(lines[:2] + [canonical_json(entry)])
                with self.assertRaises(LedgerError) as caught:
                    verify(self.path)
                self.assertRegex(str(caught.exception),
                                 r"^ledger:line 3:[a-z0-9_]+(_malformed|_not_allowed|_unsupported)$")
        for old, new in ((b'"m":2', b'"m":2.0'), (b'"alpha":"0.02"', b'"alpha":0.02'), (b'"seq":2', b'"seq":2e0')):
            with self.subTest(new=new):
                self.assertIn(old, lines[2])
                self.write_lines(lines[:2] + [lines[2].replace(old, new)])
                self.assertRefused("ledger:line 3:not_strict_json", verify, self.path)

    def test_field_rules_on_rehashed_lines(self):
        self.three()
        lines = self.lines()
        cases = ((0, dict(kind="prior"), "kind_not_allowed"), (0, dict(kind=["look"]), "kind_not_allowed"),
                 (2, dict(result="engine_check_failed"), "result_not_allowed"),
                 (0, dict(result="failed"), "result_not_allowed"), (0, dict(m=2), "m_or_alpha_not_zero"),
                 (0, dict(alpha="0.02"), "m_or_alpha_not_zero"), (2, dict(alpha="0"), "m_or_alpha_malformed"),
                 (2, dict(alpha="0.000"), "m_or_alpha_malformed"), (2, dict(m=0), "m_or_alpha_malformed"),
                 (2, dict(m=True), "m_or_alpha_malformed"), (2, dict(alpha="2e-2"), "m_or_alpha_malformed"),
                 (0, dict(partitions=["PRO-V", "PRO-D"]), "partitions_malformed"),
                 (0, dict(partitions=["PRO-D", "PRO-D"]), "partitions_malformed"),
                 (0, dict(partitions=[]), "partitions_malformed"),
                 (0, dict(partitions="PRO-D"), "partitions_malformed"),
                 (0, dict(partitions=["pro d"]), "partitions_malformed"),
                 (1, dict(ledger_schema=2), "ledger_schema_unsupported"), (1, dict(seq=True), "seq_malformed"),
                 (1, dict(code_commit=COMMIT[:39]), "code_commit_malformed"),
                 (1, dict(study="Close V1"), "study_malformed"), (1, dict(lineage=""), "lineage_malformed"),
                 (1, dict(tables_sha256="AB" * 32), "tables_sha256_malformed"),
                 (1, dict(recorded_at="2026-02-30T12:00:00Z"), "recorded_at_malformed"))
        for index, change, reason in cases:
            with self.subTest(reason=reason, change=str(change)[:40]):
                self.write_lines(self.rehashed(index, lines, **change))
                self.assertRefused(f"ledger:line {index + 1}:{reason}", verify, self.path)
        self.write_lines(lines)
        entry = json.loads(lines[0])
        look_failed = self.rehashed(0, lines, result="engine_check_failed")     # allowed for a look
        extra = canonical_json(dict(entry, summary={}))
        fewer = canonical_json({key: value for key, value in entry.items() if key != "alpha"})
        spaced = json.dumps(entry, sort_keys=True).encode("utf-8")
        for changed, code in ((look_failed, None), ([extra] + lines[1:], "ledger:line 1:keys_not_entry_keys"),
                              ([fewer] + lines[1:], "ledger:line 1:keys_not_entry_keys"),
                              ([spaced] + lines[1:], "ledger:line 1:not_canonical"),
                              ([b"x" * 16385], "ledger:line 1:line_too_long"),
                              ([b"[]"], "ledger:line 1:keys_not_entry_keys")):
            with self.subTest(code=code):
                self.write_lines(changed)
                if code is None:
                    self.assertEqual(verify(self.path)[0]["result"], "engine_check_failed")
                else:
                    self.assertRefused(code, verify, self.path)

    def test_a_request_with_a_bad_value_is_refused_before_anything_is_written(self):
        cases = ((dict(alpha=0.02, m=2, kind="registered"), "m_or_alpha_malformed"),
                 (dict(m=None), "m_or_alpha_malformed"), (dict(m=1), "m_or_alpha_not_zero"),
                 (dict(study=None), "study_malformed"), (dict(code_commit="main"), "code_commit_malformed"),
                 (dict(input_sha256=None), "input_sha256_malformed"), (dict(kind="prior"), "kind_not_allowed"),
                 (dict(partitions="PRO-D"), "partitions_malformed"),
                 (dict(partitions=("PRO-D", 3)), "partitions_malformed"),
                 (dict(partitions=("PRO-D", "PRO-D")), "partitions_malformed"),
                 (dict(partitions=()), "partitions_malformed"))
        ledger = self.open()
        for change, reason in cases:
            with self.subTest(reason=reason, change=str(change)[:40]):
                request = self.request(**change)
                self.assertRefused(f"ledger:entry:{reason}", ledger.check, request, SPLIT)
                self.assertRefused(f"ledger:entry:{reason}", ledger.record, request, SPLIT, sha("t"), "completed",
                                   lambda: CLOCK)
        for tables, result, clock, reason in ((None, "completed", CLOCK, "tables_sha256_malformed"),
                                              (sha("t"), "failed", CLOCK, "result_not_allowed"),
                                              (sha("t"), "completed", "2026-11-11 12:00", "recorded_at_malformed"),
                                              (sha("t"), "completed", None, "recorded_at_malformed")):
            with self.subTest(reason=reason):
                self.assertRefused(f"ledger:entry:{reason}", ledger.record, self.request(), SPLIT, tables, result,
                                   lambda: clock)
        self.assertEqual(list(self.directory.iterdir()), [])
        self.assertIsNone(ledger.head)

    def test_a_directory_with_a_trial_ledger_refuses(self):
        (self.directory / TRIAL_LEDGER_FILE).write_bytes(b"")
        self.assertRefused("ledger:trial_ledger_present", self.open)
        (self.directory / TRIAL_LEDGER_FILE).unlink()
        (self.directory / TRIAL_LEDGER_FILE).symlink_to(self.tmp / "nothing")     # a dangling link is still present
        self.assertRefused("ledger:trial_ledger_present", self.open)

    def test_the_directory_gets_the_checks_an_out_directory_gets(self):
        worktree = self.tmp / "clone"
        (worktree / ".git").mkdir(parents=True)
        (worktree / "ledger").mkdir()
        data_tree = self.tmp / "Betangel-Data" / "ledger"
        data_tree.mkdir(parents=True)
        root = self.tmp / "root"
        (root / "ledger").mkdir(parents=True)
        (self.tmp / "file").write_bytes(b"")
        (self.tmp / "link").symlink_to(data_tree)
        denied = self.tmp / ".config" / "betangel-research" / "betfair" / "ledger"       # never created
        for directory, code in ((self.tmp / "missing", "ledger:missing_path"), (None, "ledger:missing_path"),
                                (" ", "ledger:missing_path"), (self.tmp / "file", "ledger:not_a_directory"),
                                (worktree / "ledger", "ledger:inside_git_worktree"),
                                (REPO / "research", "ledger:inside_repository"),
                                (data_tree, "ledger:under_a_data_tree"),
                                (self.tmp / "link", "ledger:under_a_data_tree"),
                                (denied, "ledger:denied_live_state_tree"),
                                (root / "ledger", "ledger:roots_not_distinct"),
                                (root, "ledger:roots_not_distinct"), (self.tmp, "ledger:roots_not_distinct")):
            with self.subTest(code=code, directory=str(directory)):
                self.assertRefused(code, StudyLedger.open, directory, "none", root=root)
        self.assertFalse(denied.exists())
        self.assertEqual(study_ledger.ledger_dir(self.directory, root), self.directory)
        self.assertEqual(sorted(p.name for p in self.directory.iterdir()), [])        # nothing is created

    def test_two_handles_at_one_head_cannot_both_append(self):
        first, second = self.open(), self.open()
        first.record(self.request(), SPLIT, sha("t"), "completed", lambda: CLOCK)
        before = self.path.read_bytes()
        self.assertRefused("ledger:head_moved", second.record, self.request(run=1), SPLIT, sha("t"), "completed",
                           lambda: CLOCK)
        self.head = first.head
        third, fourth = self.open(), self.open()
        third.record(self.request(run=1), SPLIT, sha("t"), "completed", lambda: CLOCK)
        self.assertRefused("ledger:head_moved", fourth.record, self.request(run=2), SPLIT, sha("t"), "completed",
                           lambda: CLOCK)
        self.assertEqual(self.path.read_bytes()[:len(before)], before)
        self.assertEqual(len(verify(self.path)), 2)
        self.assertFalse((self.directory / LOCK_FILE).exists())

    def test_a_full_ledger_writes_nothing_and_an_unconfirmed_append_is_uncertain(self):
        self.run_pass(self.request())
        ledger, before = self.open(), self.path.read_bytes()
        with mock.patch.object(study_ledger, "MAX_LEDGER_BYTES", 2 * len(before) - 1):
            self.assertRefused("ledger:entry:ledger_full", ledger.record, self.request(run=1), SPLIT, sha("t"),
                               "completed", lambda: CLOCK)
        self.assertEqual(self.path.read_bytes(), before)
        with mock.patch.object(study_ledger, "verify", side_effect=LedgerError("ledger:unreadable")):
            with self.assertRaises(WriteUncertainError) as caught:
                ledger.record(self.request(run=1), SPLIT, sha("t"), "completed", lambda: CLOCK)
        self.assertEqual(str(caught.exception), "ledger:append_not_confirmed")
        self.assertEqual(ledger.head, self.head)                            # not advanced: reopen with the true head
        self.assertEqual(len(verify(self.path)), 2)
        self.assertRefused("ledger:head_moved", ledger.record, self.request(run=2), SPLIT, sha("t"), "completed",
                           lambda: CLOCK)

    def test_a_write_that_fails_part_way_is_uncertain_and_leaves_a_cut_line(self):
        self.run_pass(self.request())
        ledger, before = self.open(), self.path.read_bytes()
        write = study_ledger._write_all

        def partial(handle, data):
            write(handle, data[:len(data) // 2])
            raise OSError("synthetic")

        with mock.patch.object(study_ledger, "_write_all", partial):
            with self.assertRaises(WriteUncertainError) as caught:
                ledger.record(self.request(run=1), SPLIT, sha("t"), "completed", lambda: CLOCK)
        self.assertEqual(str(caught.exception), "ledger:write_failed")
        self.assertEqual(ledger.head, self.head)                            # the head did not advance
        self.assertTrue(self.path.read_bytes().startswith(before))
        self.assertFalse((self.directory / LOCK_FILE).exists())
        self.assertRefused("ledger:line 2:truncated_line", self.open)       # every later pass refuses: no repair

    def test_an_unremovable_lock_after_a_written_entry_reports_the_new_head(self):
        ledger = self.open()
        unlink = Path.unlink

        def stuck(path, *args, **kwargs):
            if path.name == LOCK_FILE:
                raise OSError("synthetic")
            return unlink(path, *args, **kwargs)

        with mock.patch.object(Path, "unlink", stuck):
            with self.assertRaises(UnlockError) as caught:
                ledger.record(self.request(), SPLIT, sha("t"), "completed", lambda: CLOCK)
        entries = verify(self.path)
        self.assertEqual(caught.exception.head, (0, entries[0]["entry_sha256"]))
        self.assertEqual(ledger.head, caught.exception.head)
        self.head = ledger.head
        self.assertRefused("ledger:locked", self.open)


class LedgerHead(LedgerCase):
    def test_none_is_right_exactly_once(self):
        ledger = StudyLedger.open(self.directory, "none")
        self.assertIsNone(ledger.head)
        self.assertEqual(ledger.entries(), ())
        head = ledger.record(self.request(), SPLIT, sha("t"), "completed", lambda: CLOCK)
        self.assertEqual((head, ledger.head), ((0, verify(self.path)[0]["entry_sha256"]), head))
        self.assertRefused("ledger:head_not_expected", StudyLedger.open, self.directory, "none")
        self.assertEqual(StudyLedger.open(self.directory, head_text(head)).head, head)

    def test_a_stale_head_a_wrong_hash_and_a_head_for_no_file(self):
        self.run_pass(self.request())
        first = self.head
        self.run_pass(self.request(run=1))
        self.assertEqual(self.open().head, self.head)                       # the right head passes
        for expected in (head_text(first), f"1:{first[1]}", f"1:{sha('other')}", f"2:{self.head[1]}",
                         f"0:{self.head[1]}"):
            with self.subTest(expected=expected[:12]):
                self.assertRefused("ledger:head_not_expected", StudyLedger.open, self.directory, expected)
        fresh = self.tmp / "fresh"                                          # a mistyped or new directory
        fresh.mkdir()
        self.assertRefused("ledger:head_not_expected", StudyLedger.open, fresh, head_text(self.head))
        self.assertEqual(list(fresh.iterdir()), [])

    def test_the_head_text(self):
        digest = sha("x")
        self.assertEqual((parse_head("none"), parse_head(f"0:{digest}"), parse_head(f"12:{digest}")),
                         (None, (0, digest), (12, digest)))
        self.assertEqual((head_text(None), head_text((12, digest)), head_text([3, digest])),
                         ("none", f"12:{digest}", f"3:{digest}"))
        for bad in ("", "None", "none ", "0", digest, f"0:{digest.upper()}", f"0:{digest[:63]}", f"-1:{digest}",
                    f"01:{digest}", f"+1:{digest}", f"1 :{digest}", f"١:{digest}", f"1:{digest}:x", None, 0,
                    (0, digest), b"none"):
            with self.subTest(bad=bad):
                self.assertRefused("ledger:head_malformed", parse_head, bad)
                self.assertRefused("ledger:head_malformed", StudyLedger.open, self.directory, bad)

    def test_counts_for_the_receipt(self):
        self.three()
        self.run_pass(self.request(run=1), result="engine_check_failed")
        ledger = self.open()
        self.assertEqual(ledger.counts("close"), {"looks": 2, "registered": 1})
        self.assertEqual(ledger.counts("inplay"), {"looks": 1, "registered": 0})
        self.assertEqual(ledger.counts("other"), {"looks": 0, "registered": 0})


class LedgerBudgets(LedgerCase):
    def refused(self, code, request, split=SPLIT):
        """Refused before the read (check) and at the append (record), and nothing is written."""
        before = self.path.read_bytes() if self.path.exists() else None
        ledger = self.open()
        self.assertRefused(code, ledger.check, request, split)
        self.assertRefused(code, ledger.record, request, split, sha("t"), "completed", lambda: CLOCK)
        self.assertEqual(self.path.read_bytes() if self.path.exists() else None, before)
        self.assertEqual(ledger.head, self.head)
        self.assertFalse((self.directory / LOCK_FILE).exists())

    def test_a_third_look_at_one_study(self):
        self.run_pass(self.request())
        self.run_pass(self.request(run=1), result="engine_check_failed")    # a failed engine check costs a look
        self.refused("ledger:looks_spent", self.request(run=2))
        self.refused("ledger:looks_spent", self.request(code_commit="f" * 40))
        self.run_pass(self.request("inplay-v1"))                           # another study has its own looks
        self.run_pass(self.request("close-v2"))                            # and so has a new id in the lineage
        self.run_pass(self.request(kind="registered"))                     # a look budget stops no test
        self.assertEqual(len(verify(self.path)), 5)

    def test_the_21st_explore_of_a_lineage(self):
        for run in range(20):
            self.assertIsNone(self.run_pass(self.request(kind="explore", run=run)))
        self.refused("ledger:explore_budget_spent", self.request(kind="explore", run=20))
        self.refused("ledger:explore_budget_spent", self.request("close-v2", kind="explore"))
        self.refused("ledger:explore_budget_spent", self.request(kind="registered"))
        self.run_pass(self.request("inplay-v1", kind="explore"))            # another lineage has its own budget
        self.run_pass(self.request())                                      # a look is not an explore
        self.assertEqual(len(verify(self.path)), 22)

    def test_a_registered_entry_counts_as_an_explore(self):
        self.run_pass(self.request(kind="registered"))
        for run in range(19):
            self.run_pass(self.request(kind="explore", run=run))
        self.refused("ledger:explore_budget_spent", self.request(kind="explore", run=19))
        self.assertEqual([e["kind"] for e in verify(self.path)].count("explore"), 19)

    def test_a_second_registered_pass_for_a_lineage(self):
        self.run_pass(self.request(kind="registered"))
        self.refused("ledger:lineage_already_tested", self.request(kind="registered", run=1))
        self.refused("ledger:lineage_already_tested", self.request("close-v2", kind="registered"))
        self.refused("ledger:lineage_already_tested", self.request("other-v1", kind="registered", lineage="close"))
        self.run_pass(self.request("inplay-v1", kind="registered", m=1))    # another lineage may still test
        self.assertEqual(self.open().counts("close"), {"looks": 0, "registered": 1})

    def test_alpha_over_the_lifetime_of_the_holdout(self):
        for lineage in "abcde":                                             # 5 x 0.02 is exactly 0.10: allowed
            self.run_pass(self.request(f"{lineage}-v1", kind="registered"))
        self.refused("ledger:alpha_spent", self.request("f-v1", kind="registered"))
        self.refused("ledger:alpha_spent", self.request("f-v1", kind="registered", alpha="0.000001"))
        self.run_pass(self.request("f-v1"))                                 # looking is still free
        self.assertEqual(sum(Fraction(e["alpha"]) for e in verify(self.path)), Fraction(1, 10))

    def test_one_pass_may_not_take_more_than_the_lifetime(self):
        self.refused("ledger:alpha_spent", self.request(kind="registered", alpha="0.11"))
        self.run_pass(self.request(kind="registered", alpha="0.10"))
        self.refused("ledger:alpha_spent", self.request("inplay-v1", kind="registered", alpha="0.01"))

    def test_budgets_are_the_given_splits_plain_values(self):
        self.run_pass(self.request())
        self.refused("ledger:looks_spent", self.request(run=1), replace(SPLIT, max_looks=1))
        self.run_pass(self.request(kind="explore"))
        self.refused("ledger:explore_budget_spent", self.request(kind="explore", run=1),
                     replace(SPLIT, explore_budgets={"PRO-D": 1}))
        self.refused("ledger:alpha_spent", self.request(kind="registered"),
                     replace(SPLIT, alpha_lifetimes={"PRO-V": Fraction(1, 100)}))
        self.run_pass(self.request(run=1))

    def test_a_repeat_appends_nothing_and_must_match_the_stored_hash(self):
        self.run_pass(self.request(), tables=sha("blind"))
        self.run_pass(self.request(run=1), tables=sha("blind 2"))
        self.run_pass(self.request(kind="registered"), tables=sha("registered"))
        before, head = self.path.read_bytes(), self.head
        entries = verify(self.path)
        # Both looks are spent and the lineage is tested, yet each run may be made again: a repeat counts nowhere.
        for index, (request, tables) in enumerate(((self.request(), sha("blind")),
                                                   (self.request(run=1), sha("blind 2")),
                                                   (self.request(kind="registered"), sha("registered")))):
            with self.subTest(index=index):
                self.assertEqual(identity(request), identity(entries[index]))
                self.assertEqual(self.run_pass(request, tables=tables), entries[index])
                self.assertEqual((self.head, self.path.read_bytes()), (head, before))
                ledger = self.open()
                self.assertEqual(ledger.check(request, SPLIT), entries[index])
                self.assertRefused("ledger:repeat_differs", ledger.record, request, SPLIT, sha("other"), "completed",
                                   lambda: CLOCK)
                self.assertEqual((ledger.head, self.path.read_bytes()), (head, before))
        ledger = self.open()
        self.assertRefused("ledger:repeat_differs", ledger.record, self.request(), SPLIT, sha("blind"),
                           "engine_check_failed", lambda: CLOCK)
        self.refused("ledger:repeat_differs", self.request(lineage="inplay"))
        self.refused("ledger:repeat_differs", self.request(partitions=("PRO-D",)))
        self.refused("ledger:repeat_differs", self.request(kind="registered", m=3))
        self.refused("ledger:repeat_differs", self.request(kind="registered", alpha="0.01"))
        self.refused("ledger:looks_spent", self.request(run=2))             # a new identity is still refused
        self.assertFalse((self.directory / LOCK_FILE).exists())

    def test_a_repeat_never_reads_the_clock_into_the_file(self):
        self.run_pass(self.request())
        ledger = self.open()
        ledger.record(self.request(), SPLIT, sha("tables"), "completed", lambda: "2027-01-01T00:00:00Z")
        self.assertEqual([e["recorded_at"] for e in verify(self.path)], [CLOCK])

    def test_labels_and_kinds_the_split_does_not_allow(self):
        cases = ((dict(partitions=("PRO-D", "BASIC-T")), "partition_role_sealed"),
                 (dict(partitions=("SEALED",)), "partition_role_never"),
                 (dict(partitions=("BASIC-D", "BASIC-V")), "partition_no_budget"),
                 (dict(partitions=("PRO-D", "PRO-X")), "partition_label_unknown"),
                 (dict(partitions_sha256=sha("another split")), "split_not_the_requests"),
                 (dict(kind="explore", partitions=("PRO-D", "PRO-V")), "explore_touches_another_role"),
                 (dict(kind="registered", partitions=("PRO-D",)), "registered_without_holdout"))
        for change, reason in cases:
            with self.subTest(reason=reason):
                self.refused(f"ledger:entry:{reason}", self.request(**change))
        self.run_pass(self.request(partitions=["PRO-V", "PRO-D"]))          # a list in any order is sorted
        self.assertEqual(verify(self.path)[0]["partitions"], ["PRO-D", "PRO-V"])


def rule_bytes(name="close-v1-back", change=None):
    """A rule file's value, changed, in the one byte form."""
    value = json.loads((RULES_DIR / f"{name}.json").read_bytes())
    if change is not None:
        change(value)
    return rulefile.file_bytes(value)


def horse(selection_id, back=None, size=None, lay=None, ticks=None):
    """One runner of a view; prices and sizes are decimal text, read exactly."""
    return RunnerView(selection_id, *(None if text is None else Fraction(text) for text in (back, size, lay)), ticks)


def view_of(*runners, at_s=-300, market_open=1, active=None, malformed=False):
    return RuleView(at_s, market_open, len(runners) if active is None else active, malformed, tuple(runners))


def field(favourite=None, **kwargs):
    """Five runners; the favourite (id 11) is 2.50 to back for 50.00 with a lay one tick above, unless given."""
    first = horse(11, "2.50", "50.00", "2.52", 1) if favourite is None else favourite
    return view_of(first, horse(12, "5.00", "20.00", "5.10", 1), horse(13, "8.00", "5.00", "8.40", 2),
                   horse(14, "12.00", "3.00", "13.00", 2), horse(15), **kwargs)


class RuleParse(unittest.TestCase):
    def assertRefused(self, code, change=None, raw=None, name="close-v1-back"):
        with self.assertRaises(RuleError) as caught:
            parse_rule(rule_bytes(name, change) if raw is None else raw)
        self.assertEqual(str(caught.exception), f"rule:{code}")

    def test_a_copy_parses_and_decimals_are_exact(self):
        rule = parse_rule(rule_bytes())
        self.assertEqual(rule, RULES["close-v1-back"])
        self.assertEqual(rule.when[2], Predicate("best_back", ">=", Fraction(3, 2), "best_back_out_of_range"))
        self.assertEqual(rule.when[1], Predicate("active_runners", ">=", 5, "too_few_active_runners"))
        self.assertEqual([type(row.value) for row in rule.when], [int, int, Fraction, Fraction, Fraction, int, int])
        self.assertEqual(rule.intents, (Template("BACK", "best_lay", None, Fraction(2), "lapse"),))
        self.assertEqual(RULES["inplay-v1-lay"].intents[0], Template("LAY", "fixed", Fraction(101, 100), Fraction(2),
                                                                     "keep"))
        with self.assertRaises(FrozenInstanceError):
            rule.select = "each_active"

    def test_exact_keys_at_every_level(self):
        places = (("top", lambda v: v), ("market", lambda v: v["market"]), ("times[1]", lambda v: v["times"][1]),
                  ("when[3]", lambda v: v["when"][3]), ("intents[0]", lambda v: v["intents"][0]))
        for place, find in places:
            with self.subTest(place=place, case="unknown key"):
                self.assertRefused(f"{place}:keys_not_the_expected_set", lambda v: find(v).update(note="x"))
            for key in sorted(find(json.loads(rule_bytes()))):
                with self.subTest(place=place, case="missing key", key=key):
                    self.assertRefused(f"{place}:keys_not_the_expected_set", lambda v: find(v).pop(key))
        cut = ("exit", "min_active_runners", "best_lay_size")              # keys an earlier draft had
        self.assertEqual([key for key in cut if key in rulefile.TOP_KEYS + rulefile.MARKET_KEYS + rulefile.FEATURES],
                         [])
        self.assertRefused("intents[0].price:keys_not_the_expected_set",
                           lambda v: v["intents"][0].update(price={"ref": "fixed"}))
        self.assertRefused("intents[0].price:keys_not_the_expected_set",
                           lambda v: v["intents"][0].update(price={"ref": "best_lay", "ticks": 1}))
        self.assertRefused("intents[0].price:keys_not_the_expected_set",
                           lambda v: v["intents"][0].update(price={"ref": "best_lay", "value": "2.00"}))
        self.assertRefused("intents[0].price:price_ref_not_supported", lambda v: v["intents"][0].update(price={}))
        for place, find in (("market", lambda v: v.update(market=[])), ("times[0]", lambda v: v["times"].insert(0, 5)),
                            ("when[0]", lambda v: v["when"].insert(0, "market_open")),
                            ("intents[0]", lambda v: v.update(intents=["BACK"]))):
            with self.subTest(place=place, case="not an object"):
                self.assertRefused(f"{place}:not_an_object", find)

    def test_an_unknown_feature_operator_selector_or_price_ref(self):
        cases = (("when[0]:feature_not_supported", lambda v: v["when"][0].update(feature="ltp")),
                 ("when[0]:feature_not_supported", lambda v: v["when"][0].update(feature="bsp")),
                 ("when[0]:feature_not_supported", lambda v: v["when"][0].update(feature="traded_volume")),
                 ("when[0]:feature_not_supported", lambda v: v["when"][0].update(feature="best_lay_size")),
                 ("when[0]:feature_not_supported", lambda v: v["when"][0].update(feature=["market_open"])),
                 ("when[2]:operator_not_supported", lambda v: v["when"][2].update(op=">")),
                 ("when[2]:operator_not_supported", lambda v: v["when"][2].update(op="!=")),
                 ("when[2]:operator_not_supported", lambda v: v["when"][2].update(op="or")),
                 ("when[2]:operator_not_supported", lambda v: v["when"][2].update(op=None)),
                 ("select:selector_not_supported", lambda v: v.update(select="second_favourite")),
                 ("select:selector_not_supported", lambda v: v.update(select=["favourite"])),
                 ("intents[0].price:price_ref_not_supported", lambda v: v["intents"][0]["price"].update(ref="ltp")),
                 ("intents[0].price:price_ref_not_supported", lambda v: v["intents"][0]["price"].update(ref="bsp")),
                 ("intents[0].price:price_ref_not_supported", lambda v: v["intents"][0].update(price="2.00")),
                 ("intents[0]:side_not_supported", lambda v: v["intents"][0].update(side="back")),
                 ("intents[0]:persist_not_supported", lambda v: v["intents"][0].update(persist="market_on_close")))
        for code, change in cases:
            with self.subTest(code=code):
                self.assertRefused(code, change)

    def test_status_schema_names_and_parent(self):
        cases = (("status:not_frozen", lambda v: v.update(status="draft")),
                 ("status:not_frozen", lambda v: v.update(status=None)),
                 ("schema:not_supported", lambda v: v.update(schema="betangel.rule/v2")),
                 ("id:name_malformed", lambda v: v.update(id="Close V1")),
                 ("id:name_malformed", lambda v: v.update(id="")),
                 ("lineage:name_malformed", lambda v: v.update(lineage=None)),
                 ("lineage:name_malformed", lambda v: v.update(lineage="x" * 65)),
                 ("parent_sha256:not_a_sha256_or_null", lambda v: v.update(parent_sha256="AB" * 32)),
                 ("parent_sha256:not_a_sha256_or_null", lambda v: v.update(parent_sha256="")),
                 ("parent_sha256:not_a_sha256_or_null", lambda v: v.update(parent_sha256=0)),
                 ("claim:not_a_sentence", lambda v: v.update(claim="")),
                 ("claim:not_a_sentence", lambda v: v.update(claim=" padded ")),
                 ("counter_claim:not_a_sentence", lambda v: v.update(counter_claim="two\nlines")),
                 ("counter_claim:not_a_sentence", lambda v: v.update(counter_claim="x" * 401)),
                 ("counter_claim:not_a_sentence", lambda v: v.update(counter_claim=None)))
        for code, change in cases:
            with self.subTest(code=code):
                self.assertRefused(code, change)
        parent = RULE_SHA256["close-v1-back"]
        child = parse_rule(rule_bytes(change=lambda v: v.update(id="close-v2-back", parent_sha256=parent)))
        self.assertEqual((child.parent_sha256, child.lineage), (parent, "close"))
        self.assertNotEqual(child.sha256, parent)
        self.assertEqual(rulefile.NAME_RE.pattern, study_ledger._NAME_RE.pattern)   # a lineage the ledger will take

    def test_bad_times(self):
        cases = (("times[0]:not_an_int_in_range", lambda v: v["times"][0].update(at_s=0)),
                 ("times[0]:not_an_int_in_range", lambda v: v["times"][0].update(at_s=600)),
                 ("times[0]:not_an_int_in_range", lambda v: v["times"][0].update(at_s=-86401)),
                 ("times[0]:not_an_int_in_range", lambda v: v["times"][0].update(at_s="-600")),
                 ("times[0]:not_an_int_in_range", lambda v: v["times"][0].update(at_s=True)),
                 ("times[0]:not_an_int_in_range", lambda v: v["times"][0].update(at_s=None)),
                 ("times[0]:not_an_int_in_range", lambda v: v["times"][0].update(pull_s=0)),
                 ("times[0]:not_an_int_in_range", lambda v: v["times"][0].update(pull_s="-120")),
                 ("times[0]:pull_not_after_at", lambda v: v["times"][0].update(pull_s=-600)),
                 ("times[0]:pull_not_after_at", lambda v: v["times"][0].update(pull_s=-601)),
                 ("times[1]:not_after_the_one_before", lambda v: v["times"][1].update(at_s=-600)),
                 ("times[1]:not_after_the_one_before", lambda v: v["times"][1].update(at_s=-700)),
                 ("times[1]:not_after_the_one_before", lambda v: v["times"].reverse()),
                 ("times:not_a_list", lambda v: v.update(times=[])),
                 ("times:not_a_list", lambda v: v.update(times={"at_s": -600, "pull_s": -120})),
                 ("times:not_a_list", lambda v: v.update(times=[{"at_s": -100 - n, "pull_s": None}
                                                                for n in range(65, 0, -1)])))
        for code, change in cases:
            with self.subTest(code=code):
                self.assertRefused(code, change)
        edge = parse_rule(rule_bytes(change=lambda v: v.update(times=[{"at_s": -86400, "pull_s": -1},
                                                                      {"at_s": -2, "pull_s": -1},
                                                                      {"at_s": -1, "pull_s": None}])))
        self.assertEqual(edge.times, ((-86400, -1), (-2, -1), (-1, None)))

    def test_constants_counts_are_integers_and_decimals_are_text(self):
        cases = (("when[1]:not_an_int_in_range", lambda v: v["when"][1].update(value="5")),
                 ("when[1]:not_an_int_in_range", lambda v: v["when"][1].update(value=-1)),
                 ("when[0]:not_an_int_in_range", lambda v: v["when"][0].update(value=True)),
                 ("when[5]:not_an_int_in_range", lambda v: v["when"][5].update(value="1.00")),
                 ("when[2]:not_decimal_text", lambda v: v["when"][2].update(value=2)),
                 ("when[2]:not_decimal_text", lambda v: v["when"][2].update(value="1.5")),
                 ("when[2]:not_decimal_text", lambda v: v["when"][2].update(value="1.500")),
                 ("when[2]:not_decimal_text", lambda v: v["when"][2].update(value="01.50")),
                 ("when[2]:not_decimal_text", lambda v: v["when"][2].update(value="1,50")),
                 ("when[2]:not_decimal_text", lambda v: v["when"][2].update(value="-1.50")),
                 ("when[2]:not_decimal_text", lambda v: v["when"][2].update(value="1e2")),
                 ("when[2]:not_decimal_text", lambda v: v["when"][2].update(value=None)),
                 ("file:not_strict_json", lambda v: v["when"][2].update(value=1.5)),
                 ("file:not_strict_json", lambda v: v["when"][1].update(value=5.0)),
                 ("intents[0]:not_decimal_text", lambda v: v["intents"][0].update(stake=2)),
                 ("intents[0]:not_decimal_text", lambda v: v["intents"][0].update(stake="2")),
                 ("intents[0]:stake_not_positive", lambda v: v["intents"][0].update(stake="0.00")))
        for code, change in cases:
            with self.subTest(code=code):
                self.assertRefused(code, change)

    def test_when_codes_and_the_order_of_rows(self):
        cases = (("when[0]:code_malformed", lambda v: v["when"][0].update(code="Market Not Open")),
                 ("when[0]:code_malformed", lambda v: v["when"][0].update(code="")),
                 ("when[0]:code_malformed", lambda v: v["when"][0].update(code=None)),
                 ("when[1]:market_feature_after_a_runner_feature", lambda v: v["when"].insert(0, v["when"].pop(2))),
                 ("when[6]:market_feature_after_a_runner_feature", lambda v: v["when"].append(v["when"].pop(0))),
                 ("when:not_a_list", lambda v: v.update(when=None)),
                 ("when:not_a_list", lambda v: v.update(when=v["when"] * 10)))
        cases += tuple(("when[0]:code_reserved", lambda v, code=code: v["when"][0].update(code=code))
                       for code in rulefile.EVALUATOR_CODES)
        for code, change in cases:
            with self.subTest(code=code):
                self.assertRefused(code, change)
        self.assertEqual(parse_rule(rule_bytes(change=lambda v: v.update(when=[]))).when, ())   # no condition

    def test_intents_and_fixed_prices(self):
        def fixed(value):
            return lambda v: v["intents"][0].update(price={"ref": "fixed", "value": value})
        cases = (("intents[0].price:fixed_price_out_of_range", fixed("1.00")),
                 ("intents[0].price:fixed_price_out_of_range", fixed("0.50")),
                 ("intents[0].price:fixed_price_out_of_range", fixed("1000.01")),
                 ("intents[0].price:not_decimal_text", fixed(2)), ("intents[0].price:not_decimal_text", fixed("2")),
                 ("intents[0].price:not_decimal_text", fixed(None)),
                 ("intents:row_used_twice", lambda v: v["intents"].append(dict(v["intents"][0]))),
                 ("intents:not_a_list", lambda v: v.update(intents=[])),
                 ("intents:not_a_list", lambda v: v.update(intents=v["intents"][0])))
        for code, change in cases:
            with self.subTest(code=code):
                self.assertRefused(code, change)
        for value in ("1.01", "1000.00", "3.33"):                          # 3.33 is no ladder price: not checked here
            self.assertEqual(parse_rule(rule_bytes(change=fixed(value))).intents[0].fixed, Fraction(value))

    def test_the_market_block(self):
        cases = (("market.bsp_market:not_true_or_null", lambda v: v["market"].update(bsp_market=False)),
                 ("market.turn_in_play_enabled:not_true_or_null",
                  lambda v: v["market"].update(turn_in_play_enabled="true")),
                 ("market.persistence_enabled:not_true_or_null", lambda v: v["market"].update(persistence_enabled=1)),
                 ("market.countries:not_sorted_and_unique", lambda v: v["market"].update(countries=["IE", "GB"])),
                 ("market.countries:not_sorted_and_unique", lambda v: v["market"].update(countries=["GB", "GB"])),
                 ("market.countries:country_malformed", lambda v: v["market"].update(countries=["gb"])),
                 ("market.countries:country_malformed", lambda v: v["market"].update(countries=["GBR"])),
                 ("market.countries:not_a_list", lambda v: v["market"].update(countries=[])),
                 ("market.countries:not_a_list", lambda v: v["market"].update(countries="GB")),
                 ("market.event_type_id:not_an_id", lambda v: v["market"].update(event_type_id=7)),
                 ("market.event_type_id:not_an_id", lambda v: v["market"].update(event_type_id="07")),
                 ("market.market_type:not_a_token", lambda v: v["market"].update(market_type="win")),
                 ("market.betting_type:not_a_token", lambda v: v["market"].update(betting_type=None)),
                 ("market.number_of_winners:not_an_int_in_range", lambda v: v["market"].update(number_of_winners=0)),
                 ("market.number_of_winners:not_an_int_in_range",
                  lambda v: v["market"].update(number_of_winners="1")))
        for code, change in cases:
            with self.subTest(code=code):
                self.assertRefused(code, change)

    def test_only_the_one_byte_form_is_taken(self):
        raw = rule_bytes()
        value = json.loads(raw)
        doubled = raw.replace(b'"select": "favourite",', b'"select": "each_active",\n  "select": "favourite",', 1)
        self.assertEqual(json.loads(doubled), value)                        # a duplicate key json.loads lets through
        for code, data in (("file:not_canonical", canonical_json(value)), ("file:not_canonical", raw + b"\n"),
                           ("file:not_canonical", raw[:-1]), ("file:not_canonical", doubled),
                           ("file:not_canonical", json.dumps(dict(reversed(list(value.items()))), indent=2)
                            .encode("ascii") + b"\n"),
                           ("file:not_canonical", json.dumps(dict(value, claim="caf" + chr(233)), sort_keys=True,
                                                             indent=2, ensure_ascii=False).encode("utf-8") + b"\n"),
                           ("file:not_strict_json", raw[:-3]), ("file:not_strict_json", b"\xff"),
                           ("file:not_strict_json", b""),
                           ("file:not_strict_json", raw.replace(b'"parent_sha256": null', b'"parent_sha256": NaN', 1)),
                           ("file:not_bytes_or_too_large", raw.decode("ascii")),
                           ("file:not_bytes_or_too_large", b" " * ((1 << 20) + 1)), ("top:not_an_object", b"[]\n")):
            with self.subTest(code=code, size=len(data)):
                self.assertRefused(code, raw=data)


class RuleDecide(unittest.TestCase):
    BACK, LAY, INPLAY = (RULES[name] for name in ("close-v1-back", "close-v1-lay", "inplay-v1-lay"))

    def rule(self, name="close-v1-back", **changes):
        return parse_rule(rule_bytes(name, lambda v: v.update(changes)))

    def test_a_back_at_the_best_lay_and_a_lay_at_the_best_back(self):
        back, lay = decide(field(), self.BACK), decide(field(), self.LAY)
        self.assertEqual(back, Decision((Intent(11, "BACK", Fraction("2.52"), Fraction(2), -120, "lapse"),), None))
        self.assertEqual(lay, Decision((Intent(11, "LAY", Fraction("2.50"), Fraction(2), -120, "lapse"),), None))
        self.assertEqual([type(value) for value in (back.intents[0].price, back.intents[0].stake)], [Fraction] * 2)

    def test_the_time_must_be_one_of_the_rules_and_gives_its_pull(self):
        self.assertEqual([decide(field(at_s=at_s), self.BACK).intents[0].pull_s for at_s in (-600, -300, -120)],
                         [-120, -120, -10])
        for at_s in (-601, -599, -301, -119, -10, 0, 600, None):
            with self.subTest(at_s=at_s):
                self.assertEqual(decide(field(at_s=at_s), self.BACK), Decision((), "time_not_in_rule"))
        self.assertEqual(decide(field(at_s=-300), self.INPLAY).code, "time_not_in_rule")
        self.assertEqual({intent.pull_s for intent in decide(field(at_s=-600), self.INPLAY).intents}, {None})

    def test_each_feature_at_its_bounds(self):
        def favourite(back="2.50", size="50.00", lay="2.52", ticks=1):
            return decide(field(horse(11, back, size, lay, ticks)), self.BACK).code

        cases = ((decide(field(market_open=0), self.BACK).code, "market_not_open"),
                 (decide(field(market_open=1), self.BACK).code, None),
                 (decide(field(active=4), self.BACK).code, "too_few_active_runners"),
                 (decide(field(active=5), self.BACK).code, None),
                 (favourite(back="1.49", lay="1.50"), "best_back_out_of_range"), (favourite(back="1.50"), None),
                 (favourite(back="4.00", lay="4.10"), None),
                 (favourite(back="4.10", lay="4.20"), "best_back_out_of_range"),
                 (favourite(size="9.99"), "depth_below_min"), (favourite(size="10.00"), None),
                 (favourite(size=None), "depth_below_min"),
                 (favourite(ticks=0), "spread_not_1_or_2_ticks"), (favourite(ticks=1), None),
                 (favourite(ticks=2), None),
                 (favourite(ticks=3), "spread_not_1_or_2_ticks"),
                 (favourite(ticks=None), "spread_not_1_or_2_ticks"),          # a price off the ladder has no tick count
                 (favourite(lay=None, ticks=None), "spread_not_1_or_2_ticks"))
        for index, (found, expected) in enumerate(cases):
            with self.subTest(index=index, expected=expected):
                self.assertEqual(found, expected)
        best_lay = self.rule(when=[{"code": "lay_too_long", "feature": "best_lay", "op": "<=", "value": "2.52"}])
        self.assertIsNone(decide(field(), best_lay).code)
        self.assertEqual(decide(field(horse(11, "2.50", "50.00", "2.54", 2)), best_lay).code, "lay_too_long")
        self.assertEqual(decide(field(horse(11, "2.50", "50.00")), best_lay).code, "lay_too_long")

    def test_the_three_operators(self):
        for op, passing in ((">=", ("2.50", "2.52")), ("<=", ("2.48", "2.50")), ("==", ("2.50",))):
            rule = self.rule(when=[{"code": "not_it", "feature": "best_back", "op": op, "value": "2.50"}])
            for back in ("2.48", "2.50", "2.52"):
                with self.subTest(op=op, back=back):
                    found = decide(field(horse(11, back, "50.00", "2.54", 1)), rule).code
                    self.assertEqual(found, None if back in passing else "not_it")
        count = self.rule(when=[{"code": "not_six", "feature": "active_runners", "op": "==", "value": 6}])
        self.assertEqual([decide(field(active=n), count).code for n in (5, 6, 7)], ["not_six", None, "not_six"])

    def test_one_code_per_failure_in_a_fixed_order(self):
        """A view that fails everything gives the first code only; mending each failure in turn shows the order:
        the time, the ladder, market rows, the selector, then runner rows in file order."""
        bad = dict(at_s=-299, malformed=True, market_open=0, active=4)
        tied = (horse(11, "1.40", "5.00", "1.50", 5), horse(12, "1.40", "5.00", "1.50", 5))
        longer = horse(12, "6.00", "5.00", "6.20", 1)
        seen = []
        for mend in (dict(), dict(at_s=-300), dict(malformed=False), dict(market_open=1), dict(active=5)):
            bad.update(mend)
            seen.append(decide(view_of(*tied, **bad), self.BACK))
        for favourite in (horse(11, "1.39", "5.00", "1.50", 5), horse(11, "1.50", "5.00", "1.60", 5),
                          horse(11, "1.50", "50.00", "1.60", 5), horse(11, "1.50", "50.00", "1.52", 2)):
            seen.append(decide(view_of(favourite, longer, **bad), self.BACK))
        self.assertEqual([found.code for found in seen],
                         ["time_not_in_rule", "ladder_malformed", "market_not_open", "too_few_active_runners",
                          "favourite_tie", "best_back_out_of_range", "depth_below_min", "spread_not_1_or_2_ticks",
                          None])
        self.assertEqual([len(found.intents) for found in seen], [0] * 8 + [1])

    def test_and_every_row_must_hold(self):
        rows = self.BACK.when
        passing = {"market_open": 1, "active_runners": 5, "best_back": Fraction("2.50"),
                   "best_back_size": Fraction(50), "spread_ticks": 1}
        failing = {0: ("market_open", 0), 1: ("active_runners", 4), 2: ("best_back", Fraction("1.40")),
                   3: ("best_back", Fraction("4.10")), 4: ("best_back_size", Fraction(1)), 5: ("spread_ticks", 0),
                   6: ("spread_ticks", 3)}
        for index, (feature, value) in failing.items():
            values = dict(passing, **{feature: value})
            favourite = RunnerView(11, values["best_back"], values["best_back_size"], Fraction("4.20"),
                                   values["spread_ticks"])
            found = decide(view_of(favourite, at_s=-300, market_open=values["market_open"],
                                   active=values["active_runners"]), self.BACK)
            with self.subTest(row=index):
                self.assertEqual(found, Decision((), rows[index].code))

    def test_the_favourite_and_its_ties(self):
        def chosen(*runners):
            found = decide(view_of(*runners, active=5), self.BACK)
            return found.code if found.code else found.intents[0].selection_id

        first, other = horse(11, "2.50", "50.00", "2.52", 1), horse(12, "3.00", "50.00", "3.05", 1)
        unpriced, lay_only = horse(13), horse(14, None, None, "1.20")
        self.assertEqual(chosen(first, other), 11)
        self.assertEqual(chosen(other, unpriced, first), 11)                # the place in the view does not matter
        self.assertEqual(chosen(lay_only, first, other), 11)                # a runner with no best back is not priced
        self.assertEqual(chosen(first, horse(12, "2.50", "5.00", "2.56", 3)), "favourite_tie")
        self.assertEqual(chosen(first, horse(12, "2.50", "50.00", "2.52", 1), other), "favourite_tie")
        self.assertEqual(chosen(other, horse(15, "4.00", "50.00", "4.10", 1), horse(16, "4.00", "50.00", "4.10", 1)),
                         12)                                               # a tie further out is no tie
        self.assertEqual(chosen(unpriced, lay_only), "no_priced_active_runner")
        self.assertEqual(chosen(), "no_priced_active_runner")
        # The favourite is chosen first and then judged: the second runner never stands in for it.
        self.assertEqual(chosen(horse(11, "1.20", "50.00", "1.21", 1), first), "best_back_out_of_range")
        self.assertEqual(chosen(horse(11, Fraction(5, 2), "50.00", "2.52", 1), horse(12, "2.5", "50.00", "2.52", 1)),
                         "favourite_tie")                                   # exact values, however they were written

    def test_each_active_orders_every_fixed_price_on_every_runner(self):
        runners = (horse(21, "1.50", "9.00", "1.51", 1), horse(22), horse(23, "40.00", "2.00"))
        found = decide(view_of(*runners, at_s=-600), self.INPLAY)
        prices = [Fraction(text) for text in ("1.01", "1.02", "1.03", "1.05", "1.10", "1.20", "1.50", "2.00")]
        self.assertIsNone(found.code)
        self.assertEqual(found.intents, tuple(Intent(runner.selection_id, "LAY", price, Fraction(2), None, "keep")
                                              for runner in runners for price in prices))
        self.assertEqual(len(found.intents), 24)                            # an unpriced runner too: no book is read
        self.assertEqual(decide(view_of(*runners, at_s=-600, market_open=0), self.INPLAY),
                         Decision((), "market_not_open"))
        self.assertEqual(decide(view_of(runners[0], at_s=-600), self.INPLAY), Decision((), "too_few_active_runners"))
        self.assertEqual(len(decide(view_of(*runners[:2], at_s=-600), self.INPLAY).intents), 16)
        self.assertEqual(decide(view_of(at_s=-600, active=2), self.INPLAY), Decision((), "no_active_runner"))
        self.assertEqual(decide(view_of(*runners, at_s=-600, malformed=True), self.INPLAY).code, "ladder_malformed")

    def test_each_active_applies_runner_rows_per_runner(self):
        rule = self.rule("inplay-v1-lay", when=[
            {"code": "market_not_open", "feature": "market_open", "op": "==", "value": 1},
            {"code": "too_long", "feature": "best_back", "op": "<=", "value": "3.00"},
            {"code": "too_thin", "feature": "best_back_size", "op": ">=", "value": "10.00"}],
            intents=[{"persist": "lapse", "price": {"ref": "best_back"}, "side": "LAY", "stake": "2.00"},
                     {"persist": "keep", "price": {"ref": "best_lay"}, "side": "BACK", "stake": "3.00"}])
        thin, good, long, bare = (horse(31, "2.00", "5.00", "2.02", 1), horse(32, "2.50", "20.00", "2.52", 1),
                                  horse(33, "9.00", "20.00", "9.20", 1), horse(34))
        found = decide(view_of(thin, good, long, bare, at_s=-600), rule)
        self.assertEqual(found, Decision((Intent(32, "LAY", Fraction("2.50"), Fraction(2), None, "lapse"),
                                          Intent(32, "BACK", Fraction("2.52"), Fraction(3), None, "keep")), None))
        self.assertEqual(decide(view_of(long, thin, bare, at_s=-600), rule), Decision((), "too_long"))
        self.assertEqual(decide(view_of(thin, long, at_s=-600), rule), Decision((), "too_thin"))
        self.assertEqual(decide(view_of(bare, thin, at_s=-600), rule), Decision((), "too_long"))    # no value fails
        # A runner that passes every row but lacks a price an intent asks for gets no intent at all.
        no_lay = horse(35, "2.50", "20.00")
        self.assertEqual(decide(view_of(no_lay, at_s=-600), rule), Decision((), "no_reference_price"))
        self.assertEqual(decide(view_of(no_lay, good, at_s=-600), rule).intents, found.intents)
        self.assertEqual(decide(field(horse(11, "2.50", "50.00")), self.rule(when=[])),
                         Decision((), "no_reference_price"))

    def test_a_fixed_price_needs_no_book_and_a_rule_with_no_rows_orders(self):
        rule = self.rule(when=[], intents=[{"persist": "lapse", "price": {"ref": "fixed", "value": "3.35"},
                                           "side": "BACK", "stake": "2.50"}])
        self.assertEqual(decide(view_of(horse(11, "2.00"), horse(12), market_open=0, active=0), rule),
                         Decision((Intent(11, "BACK", Fraction("3.35"), Fraction(5, 2), -120, "lapse"),), None))

    def test_intents_or_one_code_never_both_and_never_neither(self):
        views = [field(), field(market_open=0), field(at_s=-600), field(at_s=-1), field(malformed=True), view_of(),
                 view_of(horse(1), horse(2)), view_of(horse(1, "2.00", "20.00", "2.02", 1), active=9),
                 view_of(horse(1, "2.00", "20.00", "2.02", 1), horse(2, "2.00"), at_s=-600)]
        for rule in RULES.values():
            for index, view in enumerate(views):
                with self.subTest(rule=rule.id, view=index):
                    found = decide(view, rule)
                    self.assertEqual(bool(found.intents), found.code is None)
                    self.assertIsInstance(found.intents, tuple)
                    self.assertTrue(found.code is None or isinstance(found.code, str))

    def test_decide_is_pure(self):
        view, rule = field(), self.BACK
        before = (copy.deepcopy(view), copy.deepcopy(rule))
        first = decide(view, rule)
        self.assertEqual([decide(view, rule) for _ in range(3)], [first] * 3)
        self.assertEqual(decide(copy.deepcopy(view), parse_rule(rule_bytes())), first)       # equal values, same answer
        self.assertEqual((view, rule), before)
        self.assertEqual(hash(view), hash(before[0]))
        for item, name in ((view, "at_s"), (rule, "select"), (first, "code"), (first.intents[0], "price")):
            with self.subTest(item=type(item).__name__), self.assertRaises(FrozenInstanceError):
                setattr(item, name, None)
        with self.assertRaises(AttributeError):
            view.runners[0].best_back = None
        tree = ast.parse(Path(rulefile.__file__).read_text(encoding="utf-8"))
        self.assertEqual([node.lineno for node in ast.walk(tree) if isinstance(node, (ast.Global, ast.Nonlocal))], [])
        called = {node.func.id for node in ast.walk(tree) if isinstance(node, ast.Call)
                  and isinstance(node.func, ast.Name)}
        self.assertEqual(called & {"open", "print", "input", "exec", "eval", "__import__"}, set())
        # Module-level names are constants and classes: nothing a call to decide could change is kept between calls.
        state = [name for name, value in vars(rulefile).items() if not name.startswith("__")
                 and isinstance(value, (list, set, bytearray))]
        self.assertEqual(state, [])
        self.assertEqual([name for name, value in vars(rulefile).items() if isinstance(value, dict)
                          and not name.startswith("__")], ["OPERATORS"])


class RuleViewHoldsNoOutcome(unittest.TestCase):
    def test_the_fields_are_exactly_the_listed_ones(self):
        self.assertEqual([item.name for item in fields(RuleView)],
                         ["at_s", "market_open", "active_runners", "ladder_malformed", "runners"])
        self.assertEqual(RunnerView._fields, ("selection_id", "best_back", "best_back_size", "best_lay",
                                              "spread_ticks"))
        self.assertEqual([item.name for item in fields(Intent)],
                         ["selection_id", "side", "price", "stake", "pull_s", "persist"])
        self.assertEqual([item.name for item in fields(Decision)], ["intents", "code"])

    def test_every_feature_is_a_field_and_none_is_an_outcome_or_a_later_fact(self):
        names = [item.name for item in fields(RuleView)] + list(RunnerView._fields)
        self.assertEqual(rulefile.MARKET_FEATURES, ("market_open", "active_runners"))
        self.assertEqual(rulefile.RUNNER_FEATURES, ("best_back", "best_back_size", "best_lay", "spread_ticks"))
        self.assertTrue(set(rulefile.MARKET_FEATURES) <= {item.name for item in fields(RuleView)})
        self.assertTrue(set(rulefile.RUNNER_FEATURES) <= set(RunnerView._fields))
        self.assertTrue(set(rulefile.COUNT_FEATURES) <= set(rulefile.FEATURES))
        self.assertTrue({"best_back", "best_lay"} <= set(RunnerView._fields))      # the two price refs that read a book
        for word in ("bsp", "sp_", "ltp", "last", "traded", "volume", "tv", "result", "status", "winner", "settled",
                     "in_play", "after", "final", "outcome", "removal"):
            with self.subTest(word=word):
                self.assertEqual([name for name in names if word in name], [])

    def test_a_view_takes_nothing_else(self):
        with self.assertRaises(TypeError):
            RuleView(-300, 1, 5, False, (), bsp=Fraction(3))
        with self.assertRaises(TypeError):
            RunnerView(11, None, None, None, None, None)
        with self.assertRaises(TypeError):
            RunnerView(11, None, None, None, None, bsp=None)
        self.assertFalse(hasattr(field().runners[0], "__dict__"))               # a plain tuple: no room for more


class RuleFilesFrozen(unittest.TestCase):
    def test_the_files_are_the_reviewed_ones_in_their_one_byte_form(self):
        self.assertEqual(sorted(path.name for path in RULES_DIR.glob("*.json")),
                         sorted(f"{name}.json" for name in RULES))
        for name, digest in RULE_SHA256.items():
            raw = (RULES_DIR / f"{name}.json").read_bytes()
            with self.subTest(name=name):
                self.assertEqual(hashlib.sha256(raw).hexdigest(), digest)
                self.assertEqual(RULES[name].sha256, digest)
                raw.decode("ascii")
                self.assertEqual(raw, rulefile.file_bytes(json.loads(raw)))
                self.assertTrue(raw.endswith(b"}\n") and not raw.endswith(b"\n\n"))
                value = json.loads(raw)
                self.assertEqual((value["schema"], value["status"], value["id"], value["parent_sha256"]),
                                 ("betangel.rule/v1", "frozen", name, None))
                self.assertEqual(sorted(value), sorted(rulefile.TOP_KEYS))

    def test_the_two_close_files_differ_only_by_side(self):
        back, lay = (json.loads((RULES_DIR / f"close-v1-{side}.json").read_bytes()) for side in ("back", "lay"))
        self.assertEqual({key for key in back if back[key] != lay[key]}, {"claim", "counter_claim", "id", "intents"})
        self.assertEqual((back["intents"], lay["intents"]),
                         ([{"persist": "lapse", "price": {"ref": "best_lay"}, "side": "BACK", "stake": "2.00"}],
                          [{"persist": "lapse", "price": {"ref": "best_back"}, "side": "LAY", "stake": "2.00"}]))
        for rule in (RULES["close-v1-back"], RULES["close-v1-lay"]):
            with self.subTest(rule=rule.id):
                self.assertEqual((rule.lineage, rule.select), ("close", "favourite"))
                self.assertEqual(rule.times, ((-600, -120), (-300, -120), (-120, -10)))
                self.assertEqual(rule.market, Market(("GB", "IE"), "7", "WIN", "ODDS", 1, True, None, None))
                self.assertEqual([(row.feature, row.op, row.value, row.code) for row in rule.when], [
                    ("market_open", "==", 1, "market_not_open"),
                    ("active_runners", ">=", 5, "too_few_active_runners"),
                    ("best_back", ">=", Fraction("1.50"), "best_back_out_of_range"),
                    ("best_back", "<=", Fraction("4.00"), "best_back_out_of_range"),
                    ("best_back_size", ">=", Fraction(10), "depth_below_min"),
                    ("spread_ticks", ">=", 1, "spread_not_1_or_2_ticks"),
                    ("spread_ticks", "<=", 2, "spread_not_1_or_2_ticks")])

    def test_the_close_gates_are_movement_v1s(self):
        registered = movement.parse_registration((REPO / "research" / "studies" / "movement-v1.json").read_bytes())
        rule = RULES["close-v1-back"]
        gates = {(row.feature, row.op): row.value for row in rule.when}
        self.assertEqual((gates["active_runners", ">="], gates["best_back", ">="] * 100,
                          gates["best_back", "<="] * 100, gates["best_back_size", ">="] * 100,
                          gates["spread_ticks", ">="], gates["spread_ticks", "<="]),
                         (registered.min_active_runners, registered.best_back_min_h, registered.best_back_max_h,
                          registered.min_available_p, 1, registered.max_lay_ticks))
        self.assertEqual((rule.market.countries, rule.market.event_type_id, rule.market.market_type,
                          rule.market.betting_type, rule.market.number_of_winners),
                         (registered.countries, registered.event_type_id, registered.market_type,
                          registered.betting_type, registered.number_of_winners))
        self.assertTrue(all(-movement.MAX_OFFSET_S <= at_s <= -1 for at_s, _ in rule.times))
        self.assertEqual(rulefile.MAX_OFFSET_S, movement.MAX_OFFSET_S)

    def test_the_in_play_file(self):
        rule = RULES["inplay-v1-lay"]
        self.assertEqual((rule.lineage, rule.select, rule.times), ("inplay", "each_active", ((-600, None),)))
        self.assertEqual(rule.market, Market(("GB", "IE"), "7", "WIN", "ODDS", 1, None, True, True))
        self.assertEqual([(row.feature, row.op, row.value, row.code) for row in rule.when],
                         [("market_open", "==", 1, "market_not_open"),
                          ("active_runners", ">=", 2, "too_few_active_runners")])
        self.assertEqual(list(rule.intents),
                         [Template("LAY", "fixed", Fraction(price), Fraction(2), "keep")
                          for price in ("1.01", "1.02", "1.03", "1.05", "1.10", "1.20", "1.50", "2.00")])
        self.assertEqual([movement.tick_index(row.fixed) is not None for row in rule.intents], [True] * 8)

    def test_claims_are_one_sentence_each_and_lineages_are_two(self):
        for rule in RULES.values():
            for text in (rule.claim, rule.counter_claim):
                with self.subTest(rule=rule.id, text=text[:30]):
                    self.assertTrue(text.endswith(".") and text.count(". ") == 0 and 40 <= len(text) <= 200)
        self.assertEqual(sorted({rule.lineage for rule in RULES.values()}), ["close", "inplay"])
        self.assertEqual(len({rule.claim for rule in RULES.values()} | {r.counter_claim for r in RULES.values()}), 6)


class LaneBoundaries(unittest.TestCase):
    MODULES = ("partition", "study_ledger", "rulefile")
    RULEFILE_IMPORTS = {"collections", "dataclasses", "decimal", "fractions", "hashlib", "json", "operator", "re"}

    def test_the_lane_stays_inside_the_backtest_closure(self):
        from test_betfair_backtest_boundaries import (BACKTEST, BETFAIR, FORBIDDEN_BETFAIR, REAL, TOP,
                                                      backtest_violations)
        closure = REAL.closure([f"{BACKTEST}.{name}" for name in self.MODULES])
        self.assertEqual({module for module in closure if not module.startswith(BACKTEST)},
                         {TOP, BETFAIR, f"{TOP}.temporal"})
        self.assertEqual(backtest_violations(REAL, betfair=BETFAIR, forbidden=FORBIDDEN_BETFAIR), [])
        # rulefile's internal imports are empty: the graph holds only the parent packages that any import of it runs.
        self.assertEqual(REAL.internal[f"{BACKTEST}.rulefile"], {TOP, BETFAIR, BACKTEST})
        self.assertEqual(REAL.external[f"{BACKTEST}.rulefile"], self.RULEFILE_IMPORTS)
        self.assertEqual(set(REAL.closure([f"{BACKTEST}.rulefile"])), {TOP, BETFAIR, BACKTEST, f"{BACKTEST}.rulefile"})

    def test_the_rule_module_imports_nothing_from_the_package(self):
        """As for path_guard: every import statement in rulefile.py is absolute and names a standard-library module,
        and importing it alone loads no other module of the package."""
        tree = ast.parse(Path(rulefile.__file__).read_text(encoding="utf-8"))
        imports = [node for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))]
        self.assertEqual([node.lineno for node in imports if isinstance(node, ast.ImportFrom) and node.level], [])
        self.assertEqual({node.module for node in imports if isinstance(node, ast.ImportFrom)}
                         | {alias.name for node in imports if isinstance(node, ast.Import) for alias in node.names},
                         self.RULEFILE_IMPORTS)
        self.assertEqual([node.lineno for node in imports if node not in tree.body], [])     # none inside a function
        probe = ("import sys; sys.path.insert(0, sys.argv[1]); "
                 "import betangel_research.betfair.backtest.rulefile; "
                 "print('\\n'.join(sorted(m for m in sys.modules if m.startswith('betangel_research'))))")
        result = subprocess.run([sys.executable, "-I", "-B", "-c", probe, str(REPO)], capture_output=True, text=True,
                                check=True)
        self.assertEqual(set(result.stdout.split()),
                         {"betangel_research", "betangel_research.betfair", "betangel_research.betfair.backtest",
                          "betangel_research.betfair.backtest.rulefile"})

    def test_importing_the_lane_loads_no_other_betfair_module(self):
        probe = ("import sys; sys.path.insert(0, sys.argv[1]); "
                 "import betangel_research.betfair.backtest.partition; "
                 "import betangel_research.betfair.backtest.study_ledger; "
                 "import betangel_research.betfair.backtest.rulefile; "
                 "print('\\n'.join(sorted(m for m in sys.modules if m.startswith('betangel_research'))))")
        result = subprocess.run([sys.executable, "-I", "-B", "-c", probe, str(REPO)], capture_output=True, text=True,
                                check=True)
        loaded = set(result.stdout.split())
        self.assertEqual({m for m in loaded if not m.startswith("betangel_research.betfair.backtest")},
                         {"betangel_research", "betangel_research.betfair", "betangel_research.temporal"})

    def test_the_modules_read_no_clock_start_no_process_and_name_no_lab_folder(self):
        for module in (partition, study_ledger, rulefile):
            source = Path(module.__file__).read_text(encoding="utf-8")
            for word in ("import time", "import os", "subprocess", "now(", "today(", "fsync", "random", "lab-runs",
                         ".local"):
                with self.subTest(module=module.__name__, word=word):
                    self.assertNotIn(word, source)

    def test_the_ledger_uses_the_trial_ledgers_chain_helpers_unchanged(self):
        from betangel_research.betfair.backtest import trial_ledger
        for name in ("GENESIS", "MAX_LINE_BYTES", "MAX_LEDGER_BYTES", "LOCK_FILE", "_recorded_at_problem", "_absent",
                     "_read", "_anchor", "_Lock", "_lock_note", "_write_all", "entry_digest", "_loads", "LedgerError",
                     "WriteUncertainError", "UnlockError"):
            with self.subTest(name=name):
                self.assertIs(getattr(study_ledger, name), getattr(trial_ledger, name))
        self.assertIs(study_ledger.canonical_json, canonical_json)
        self.assertNotEqual(LEDGER_FILE, TRIAL_LEDGER_FILE)
        self.assertEqual(LOCK_FILE, TRIAL_LEDGER_FILE + ".lock")


if __name__ == "__main__":
    unittest.main()
