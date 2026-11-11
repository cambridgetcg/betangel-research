"""The research lane's data split and study ledger (betangel_research.betfair.backtest.partition and study_ledger).

Everything here is synthetic: race dates are calendar text, ledger entries carry made-up hashes, and every ledger
lives in a temporary directory that the test removes. No data root is read. These tests show the split rule, the
split file's checks, the ledger's chain, its head check and its budgets. They do not show durability across a power
loss (nothing is synced), or that an operator keeps one ledger file: a new directory starts every budget from zero.
"""

from dataclasses import replace
from datetime import date, timedelta
from fractions import Fraction
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from betangel_research.betfair.backtest import partition, study_ledger
from betangel_research.betfair.backtest.contract import canonical_json
from betangel_research.betfair.backtest.partition import PartitionError, file_bytes, parse_split
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


class LaneBoundaries(unittest.TestCase):
    MODULES = ("partition", "study_ledger")

    def test_the_lane_stays_inside_the_backtest_closure(self):
        from test_betfair_backtest_boundaries import (BACKTEST, BETFAIR, FORBIDDEN_BETFAIR, REAL, TOP,
                                                      backtest_violations)
        closure = REAL.closure([f"{BACKTEST}.{name}" for name in self.MODULES])
        self.assertEqual({module for module in closure if not module.startswith(BACKTEST)},
                         {TOP, BETFAIR, f"{TOP}.temporal"})
        self.assertEqual(backtest_violations(REAL, betfair=BETFAIR, forbidden=FORBIDDEN_BETFAIR), [])

    def test_importing_the_lane_loads_no_other_betfair_module(self):
        probe = ("import sys; sys.path.insert(0, sys.argv[1]); "
                 "import betangel_research.betfair.backtest.partition; "
                 "import betangel_research.betfair.backtest.study_ledger; "
                 "print('\\n'.join(sorted(m for m in sys.modules if m.startswith('betangel_research'))))")
        result = subprocess.run([sys.executable, "-I", "-B", "-c", probe, str(REPO)], capture_output=True, text=True,
                                check=True)
        loaded = set(result.stdout.split())
        self.assertEqual({m for m in loaded if not m.startswith("betangel_research.betfair.backtest")},
                         {"betangel_research", "betangel_research.betfair", "betangel_research.temporal"})

    def test_the_modules_read_no_clock_start_no_process_and_name_no_lab_folder(self):
        for module in (partition, study_ledger):
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
