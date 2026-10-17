"""The append-only trial ledger (betangel_research.betfair.backtest.trial_ledger) in temporary run directories.

These show the hash chain, that verify refuses edits, deletions, reordering, cuts and non-canonical lines, that an
anchor kept elsewhere detects a cut at a line boundary or a rewritten tail, that opening checks first, the trial
lifecycle, the valid_frozen contract gate, that two writers at one head cannot both append, that a handle opened on
a missing ledger reads one that appeared since and refuses one it cannot inspect, and that a write failing part-way
is reported as uncertain and leaves what it wrote for verify to judge. They do not show durability across a power
loss (nothing is synced) or protection against someone who rewrites the whole file and every copy of its head.
"""

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from betangel_research.betfair.backtest import trial_ledger
from betangel_research.betfair.backtest.contract import (EXPOSED_MONTHS, NOT_PROSPECTIVE_DATES, canonical_json,
                                                         contract_sha256)
from betangel_research.betfair.backtest.trial_ledger import (GENESIS, LEDGER_FILE, LOCK_FILE, MAX_LINE_BYTES,
                                                             LedgerError, TrialLedger, UnlockError,
                                                             WriteUncertainError, entry_digest, verify)

REPO = Path(__file__).resolve().parent.parent
DRAFT = REPO / "research" / "experiments" / "backtest-p0-bsp-v1.json"
CODE = "git:1daee5b clean"
INPUTS = "ab" * 32


def draft():
    """A synthetic draft whose state does not depend on the repository contract's freeze."""
    value = json.loads(DRAFT.read_text(encoding="utf-8"))
    value.update(status="draft", frozen_on=None)
    return value


def frozen(max_trials=4):
    """The engineering draft frozen with synthetic operational values (as the contract tests do)."""
    value = draft()
    value.update(status="frozen", frozen_on="2026-10-14")
    value["decision_timing"]["delay_s"] = 1
    value["stopping_rule"]["max_trials"] = max_trials
    return value


def rehashed(entries, start):
    """Canonical lines of entries with every hash from index start on recomputed, as a careful forger would."""
    entries = [dict(entry) for entry in entries]
    for index in range(start, len(entries)):
        if index:
            entries[index]["prev_sha256"] = entries[index - 1]["entry_sha256"]
        entries[index]["entry_sha256"] = entry_digest(entries[index])
    return [canonical_json(entry) for entry in entries]


class LedgerCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.run_dir = Path(self.temp.name)
        self.path = self.run_dir / LEDGER_FILE
        self.contract = frozen()

    def tearDown(self):
        self.temp.cleanup()

    def append(self, ledger, kind, trial_id="t1", variant="ltp_favourite", recorded_at="2026-10-14T12:00:00Z",
               summary=None, **changes):
        arguments = dict(trial_id=trial_id, contract=self.contract, variant=variant, input_sha256=INPUTS,
                         code_identity=CODE, recorded_at=recorded_at, summary={} if summary is None else summary)
        arguments.update(changes)
        return ledger.append(kind, **arguments)

    def three(self):
        ledger = TrialLedger.open(self.run_dir)
        self.append(ledger, "started", "t1")
        self.append(ledger, "started", "t2", "seeded_random_runner")
        head = self.append(ledger, "completed", "t1", summary={"issued_bets": 3, "roi_resolved": "9/70"})
        return ledger, head

    def lines(self):
        return self.path.read_bytes().split(b"\n")[:-1]

    def write_lines(self, lines):
        self.path.write_bytes(b"".join(line + b"\n" for line in lines))

    def assertRefused(self, code, function, *args, **kwargs):
        with self.assertRaises(LedgerError) as caught:
            function(*args, **kwargs)
        self.assertEqual(str(caught.exception), code)


class Chain(LedgerCase):
    def test_entries_chain_and_reopen(self):
        ledger, head = self.three()
        entries = verify(self.path)
        self.assertEqual([e["seq"] for e in entries], [0, 1, 2])
        self.assertEqual(entries[0]["prev_sha256"], GENESIS)
        self.assertEqual([e["prev_sha256"] for e in entries[1:]], [e["entry_sha256"] for e in entries[:-1]])
        self.assertEqual([e["entry_sha256"] for e in entries], [entry_digest(e) for e in entries])
        self.assertEqual(head, (2, entries[-1]["entry_sha256"]))
        self.assertEqual(TrialLedger.open(self.run_dir, anchor=head).head, head)
        self.assertEqual(ledger.entries(), entries)
        self.assertEqual(self.lines(), [canonical_json(e) for e in entries])
        first = entries[0]
        self.assertEqual((first["contract_sha256"], first["seeds"], first["code_identity"], first["recorded_at"]),
                         (contract_sha256(self.contract), {"bootstrap": 2, "selection": 1}, CODE,
                          "2026-10-14T12:00:00Z"))
        self.assertEqual(entries[2]["summary"], {"issued_bets": 3, "roi_resolved": "9/70"})
        self.assertEqual((first["input_sha256"], first["training_cutoff"]), (INPUTS, None))
        self.assertEqual(first["exposure_record"], {"exposed_months": EXPOSED_MONTHS,
                                                    "not_prospective_dates": NOT_PROSPECTIVE_DATES})
        self.assertEqual(first["exposure_record"]["exposed_months"]["2026-09"]["sha256"],
                         "8e999e30a7fc17b2dd981cc8d223071b63d94797fbe08884d7ef67d0ec22ec92")
        self.assertEqual(sorted(p.name for p in self.run_dir.iterdir()), [LEDGER_FILE])

    def test_recorded_at_is_the_callers_and_need_not_increase(self):
        ledger = TrialLedger.open(self.run_dir)
        self.append(ledger, "started", recorded_at="2026-10-14T12:00:00.123456Z")
        self.append(ledger, "failed", recorded_at="2026-10-14T11:00:00Z", summary={"failure": "loader_refused"})
        self.assertEqual([e["recorded_at"] for e in verify(self.path)],
                         ["2026-10-14T12:00:00.123456Z", "2026-10-14T11:00:00Z"])


class Tamper(LedgerCase):
    def test_an_edited_entry(self):
        self.three()
        lines = self.lines()
        lines[1] = lines[1].replace(b"seeded_random_runner", b"ltp_favourite")
        self.write_lines(lines)
        self.assertRefused("line 2:entry_hash_mismatch", verify, self.path)

    def test_an_edit_with_its_own_hash_recomputed_breaks_the_next_link(self):
        self.three()
        lines = self.lines()
        entry = json.loads(lines[1])
        entry["variant"] = "ltp_favourite"
        entry["entry_sha256"] = entry_digest(entry)
        lines[1] = canonical_json(entry)
        self.write_lines(lines)
        self.assertRefused("line 3:prev_sha256_mismatch", verify, self.path)

    def test_a_rewritten_tail_needs_the_anchor(self):
        _, head = self.three()
        lines = self.lines()
        entry = json.loads(lines[2])
        entry["summary"] = {"issued_bets": 4}
        entry["entry_sha256"] = entry_digest(entry)
        lines[2] = canonical_json(entry)
        self.write_lines(lines)
        self.assertEqual(len(verify(self.path)), 3)                 # a valid chain on its own
        self.assertRefused("ledger:anchor_mismatch", verify, self.path, head)
        self.assertRefused("ledger:anchor_mismatch", TrialLedger.open, self.run_dir, head)

    def test_the_head_commits_to_every_earlier_entry(self):
        _, head = self.three()
        entries = [dict(e) for e in verify(self.path)]
        entries[0]["code_identity"] = "git:forged"                   # entry 0 only; t1 closes at entry 2
        entries[2]["code_identity"] = "git:forged"
        self.write_lines(rehashed(entries, 0))
        self.assertEqual(len(verify(self.path)), 3)                 # every later hash recomputed: valid alone
        self.assertRefused("ledger:anchor_mismatch", verify, self.path, head)
        entries = [dict(e) for e in verify(self.path)]
        entry = dict(entries[1], prev_sha256="1" * 64)
        self.assertNotEqual(entry_digest(entry), entry_digest(entries[1]))

    def test_an_anchor_read_back_from_json_is_taken_and_a_malformed_one_refused(self):
        _, head = self.three()
        stored = json.loads(json.dumps({"ledger_head": head}))["ledger_head"]
        self.assertEqual(stored, list(head))
        self.assertEqual(TrialLedger.open(self.run_dir, anchor=stored).head, head)
        self.assertEqual(len(verify(self.path, stored)), 3)
        for bad in ([2], (2, head[1], 0), (-1, head[1]), (True, head[1]), (2, head[1].upper()), "2:" + head[1],
                    {2: head[1]}, (2.0, head[1])):
            with self.subTest(bad=bad):
                self.assertRefused("anchor:malformed", verify, self.path, bad)
                self.assertRefused("anchor:malformed", TrialLedger.open, self.run_dir, bad)
        self.path.unlink()
        self.assertRefused("ledger:truncated_before_anchor", TrialLedger.open, self.run_dir, stored)

    def test_entries_refuses_once_the_file_is_cut_below_the_handles_head(self):
        ledger, _ = self.three()
        self.write_lines(self.lines()[:2])
        self.assertRefused("ledger:truncated_before_anchor", ledger.entries)

    def test_deletion_and_reordering(self):
        self.three()
        lines = self.lines()
        self.write_lines([lines[0], lines[2]])
        self.assertRefused("line 2:seq_out_of_order", verify, self.path)
        self.write_lines([lines[1], lines[0], lines[2]])
        self.assertRefused("line 1:seq_out_of_order", verify, self.path)
        self.write_lines(lines[1:])
        self.assertRefused("line 1:seq_out_of_order", verify, self.path)

    def test_truncation(self):
        _, head = self.three()
        data = self.path.read_bytes()
        self.path.write_bytes(data[:-1])
        self.assertRefused("line 3:truncated_line", verify, self.path)
        self.path.write_bytes(data[:len(data) - 40])
        self.assertRefused("line 3:truncated_line", verify, self.path)
        self.write_lines(data.split(b"\n")[:2])
        self.assertEqual(len(verify(self.path)), 2)                 # a cut at a line boundary is a valid chain
        self.assertRefused("ledger:truncated_before_anchor", verify, self.path, head)
        self.assertRefused("ledger:truncated_before_anchor", TrialLedger.open, self.run_dir, head)
        self.path.write_bytes(b"")
        self.assertRefused("ledger:empty", verify, self.path)
        self.path.unlink()
        self.assertRefused("ledger:missing", verify, self.path)
        self.assertRefused("ledger:truncated_before_anchor", TrialLedger.open, self.run_dir, head)

    def test_non_canonical_and_non_strict_lines(self):
        self.three()
        lines = self.lines()
        entry = json.loads(lines[0])
        variants = {
            "not_canonical": json.dumps(entry, sort_keys=True).encode("utf-8"),            # spaces after separators
            "not_strict_json": lines[0][:-1] + b',"kind":"started"}',                      # a duplicate key
            "keys_not_entry_keys": canonical_json(dict(entry, extra=1)),
        }
        for reason, line in variants.items():
            with self.subTest(reason=reason):
                self.write_lines([line] + lines[1:])
                self.assertRefused(f"line 1:{reason}", verify, self.path)
        for bad in (b"[]", b"NaN", b'{"seq":1.0}', b"\xff\xfe", b""):
            with self.subTest(bad=bad):
                self.write_lines([bad] + lines[1:])
                with self.assertRaises(LedgerError):
                    verify(self.path)

    def test_verify_only_checks_on_rehashed_lines(self):
        self.three()
        entries = verify(self.path)
        bad_exposure = {"exposed_months": {"x" * 17: {}}, "not_prospective_dates": {}}
        cases = ((1, dict(seq=True), "seq_malformed"), (0, dict(ledger_schema=2), "ledger_schema_unsupported"),
                 (0, dict(ledger_schema=True), "ledger_schema_unsupported"),
                 (0, dict(seeds={"bootstrap": -1, "selection": 1}), "seeds_malformed"),
                 (0, dict(seeds={"bootstrap": 2}), "seeds_malformed"),
                 (0, dict(seeds={"bootstrap": 2, "selection": True}), "seeds_malformed"),
                 (0, dict(seeds=[2, 1]), "seeds_malformed"), (0, dict(variant="Bad"), "variant_malformed"),
                 (0, dict(variant=5), "variant_malformed"), (0, dict(kind="paused"), "kind_not_allowed"),
                 (0, dict(trial_id="T 1"), "trial_id_malformed"), (0, dict(input_sha256="AB" * 32),
                                                                     "input_sha256_malformed"),
                 (0, dict(exposure_record=bad_exposure), "exposure_record_malformed"),
                 (1, dict(summary={"a": {"b": {"c": {"d": {"e": 1}}}}}), "summary_too_deep"),
                 (1, dict(summary={"a": [[[[1]]]]}), "summary_too_deep"),
                 (1, dict(summary={"t": "x" * 1025}), "summary_text_too_long"))
        for index, change, reason in cases:
            with self.subTest(reason=reason, change=str(change)[:40]):
                changed = [dict(e) for e in entries]
                changed[index].update(change)
                self.write_lines(rehashed(changed, index))
                self.assertRefused(f"line {index + 1}:{reason}", verify, self.path)
        for change in (dict(summary={"a": {"b": {"c": {"d": 1}}}}), dict(summary={"a": [[[1]]]}),
                       dict(summary={"t": "x" * 1024}),
                       dict(exposure_record={"exposed_months": {"x" * 16: {}}, "not_prospective_dates": {}})):
            with self.subTest(accepted=str(change)[:40]):                # at the limit: t2 never closes, so valid
                changed = [dict(e) for e in entries]
                changed[1].update(change)
                self.write_lines(rehashed(changed, 1))
                self.assertEqual(len(verify(self.path)), 3)

    def test_verify_maps_every_malformed_line_to_a_ledger_error(self):
        self.three()
        lines = self.lines()
        raw = {
            "not_strict_json": lines[1].replace(b'"summary":{}', b'"summary":{"x":NaN}'),
            "not_canonical": lines[1].replace(b'"summary":{}', b'"summary":{"s":"\\ud800"}'),
        }
        for reason, line in raw.items():
            with self.subTest(reason=reason):
                self.assertNotEqual(line, lines[1])
                self.write_lines([lines[0], line, lines[2]])
                self.assertRefused(f"line 2:{reason}", verify, self.path)
                self.assertRefused(f"line 2:{reason}", TrialLedger.open, self.run_dir)
        for line, reason in ((b"[" * 16000, "not_strict_json"), (b"x" * MAX_LINE_BYTES, "not_strict_json"),
                             (b"x" * (MAX_LINE_BYTES + 1), "line_too_long")):
            with self.subTest(length=len(line)):
                self.write_lines([line])
                self.assertRefused(f"line 1:{reason}", verify, self.path)
        # Python before 3.12 raises RecursionError near 1000 levels, well inside one line; 3.14 parses deeper.
        self.write_lines(lines)
        with mock.patch.object(trial_ledger.json, "loads", side_effect=RecursionError):
            self.assertRefused("line 1:not_strict_json", verify, self.path)

    def test_the_file_size_bound_is_exact(self):
        self.three()
        size = len(self.path.read_bytes())
        with mock.patch.object(trial_ledger, "MAX_LEDGER_BYTES", size):
            self.assertEqual(len(verify(self.path)), 3)
        with mock.patch.object(trial_ledger, "MAX_LEDGER_BYTES", size - 1):
            self.assertRefused("ledger:too_large", verify, self.path)

    def test_open_checks_first_and_refuses_to_append_to_a_damaged_ledger(self):
        self.three()
        data = self.path.read_bytes()
        self.path.write_bytes(data.replace(b'"issued_bets":3', b'"issued_bets":5'))
        self.assertRefused("line 3:entry_hash_mismatch", TrialLedger.open, self.run_dir)

    def test_a_symlinked_ledger_and_a_missing_run_dir(self):
        target = self.run_dir / "elsewhere.jsonl"
        TrialLedger.open(self.run_dir)
        target.write_bytes(b"")
        self.path.symlink_to(target)
        self.assertRefused("ledger:symlink", TrialLedger.open, self.run_dir)
        self.assertRefused("run_dir:not_a_directory", TrialLedger.open, self.run_dir / "nothing")


class Concurrency(LedgerCase):
    def test_two_handles_at_one_head_cannot_both_append(self):
        first, second = TrialLedger.open(self.run_dir), TrialLedger.open(self.run_dir)
        self.append(first, "started", "t1")
        before = self.path.read_bytes()
        self.assertRefused("ledger:head_moved", self.append, second, "started", "t2")
        self.assertEqual(self.path.read_bytes(), before)
        third, fourth = TrialLedger.open(self.run_dir), TrialLedger.open(self.run_dir)
        self.append(third, "completed", "t1")
        self.assertRefused("ledger:head_moved", self.append, fourth, "started", "t2")
        self.assertEqual(len(verify(self.path)), 2)
        self.assertFalse((self.run_dir / LOCK_FILE).exists())

    def test_a_held_lock_refuses_and_nothing_is_written(self):
        ledger = TrialLedger.open(self.run_dir)
        (self.run_dir / LOCK_FILE).write_bytes(b"")
        self.assertRefused("ledger:locked", self.append, ledger, "started")
        self.assertFalse(self.path.exists())
        (self.run_dir / LOCK_FILE).unlink()
        self.append(ledger, "started")
        self.assertEqual(len(verify(self.path)), 1)

    def test_a_writer_that_ignores_the_lock_breaks_the_chain_visibly(self):
        ledger = TrialLedger.open(self.run_dir)
        self.append(ledger, "started", "t1")
        stale = verify(self.path)[0]
        rogue = dict(stale, trial_id="t9")                         # built on the head the rogue saw: seq 0
        rogue["entry_sha256"] = entry_digest(rogue)
        self.append(ledger, "started", "t2")
        with self.path.open("ab") as handle:
            handle.write(canonical_json(rogue) + b"\n")
        self.assertRefused("line 3:seq_out_of_order", verify, self.path)
        self.assertRefused("line 3:seq_out_of_order", self.append, ledger, "completed", "t1")

    def test_a_ledger_that_appears_after_open_is_head_moved(self):
        ledger = TrialLedger.open(self.run_dir)
        other = TrialLedger.open(self.run_dir)
        self.append(other, "started")
        self.assertRefused("ledger:head_moved", self.append, ledger, "started", "t2")

    def test_exclusive_create_refuses_a_ledger_made_after_the_presence_check(self):
        late = TrialLedger.open(self.run_dir)
        self.append(TrialLedger.open(self.run_dir), "started")
        before = self.path.read_bytes()
        with mock.patch.object(trial_ledger, "_absent", return_value=True):     # the race window: it appears unseen
            with self.assertRaises(LedgerError) as caught:
                self.append(late, "started", "t2")
        self.assertEqual(str(caught.exception), "ledger:head_moved")
        self.assertNotIsInstance(caught.exception, WriteUncertainError)    # the create wrote nothing
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse((self.run_dir / LOCK_FILE).exists())

    def test_an_entry_that_is_not_the_head_after_writing_is_not_confirmed(self):
        ledger = TrialLedger.open(self.run_dir)
        head = self.append(ledger, "started", "t1")
        write = trial_ledger._write_all

        def racing(handle, data):                                  # another writer lands right after ours
            write(handle, data)
            ours = json.loads(data)
            rogue = dict(ours, seq=ours["seq"] + 1, prev_sha256=ours["entry_sha256"], trial_id="t9")
            rogue["entry_sha256"] = entry_digest(rogue)
            write(handle, canonical_json(rogue) + b"\n")

        with mock.patch.object(trial_ledger, "_write_all", racing):
            with self.assertRaises(WriteUncertainError) as caught:
                self.append(ledger, "started", "t2")
        self.assertEqual(str(caught.exception), "ledger:append_not_confirmed")
        self.assertEqual(ledger.head, head)
        self.assertEqual(len(verify(self.path)), 3)
        self.assertRefused("ledger:head_moved", self.append, ledger, "completed", "t1")

    def test_the_lock_is_released_after_a_refused_entry(self):
        ledger = TrialLedger.open(self.run_dir)
        self.assertRefused("entry:trial_not_started", self.append, ledger, "completed")
        self.assertFalse((self.run_dir / LOCK_FILE).exists())
        self.assertFalse(self.path.exists())

    def test_a_substituted_entry_of_the_right_length_is_not_confirmed(self):
        ledger = TrialLedger.open(self.run_dir)
        head = self.append(ledger, "started", "t1")
        write = trial_ledger._write_all

        def substituting(handle, data):                            # another entry lands where ours should be
            rogue = dict(json.loads(data), trial_id="t9")
            rogue["entry_sha256"] = entry_digest(rogue)
            write(handle, canonical_json(rogue) + b"\n")

        with mock.patch.object(trial_ledger, "_write_all", substituting):
            self.assertRefused("ledger:append_not_confirmed", self.append, ledger, "started", "t2")
        self.assertEqual(ledger.head, head)
        self.assertEqual([e["trial_id"] for e in verify(self.path)], ["t1", "t9"])

    def test_the_lock_names_its_append_while_held(self):
        ledger = TrialLedger.open(self.run_dir)
        seen = []
        write = trial_ledger._write_all

        def peeking(handle, data):
            seen.append((self.run_dir / LOCK_FILE).read_text(encoding="ascii"))
            write(handle, data)

        with mock.patch.object(trial_ledger, "_write_all", peeking):
            self.append(ledger, "started", "t1", recorded_at="2026-10-14T12:00:00Z")
        self.assertEqual(json.loads(seen[0]), {"code_identity": CODE, "kind": "started",
                                               "recorded_at": "2026-10-14T12:00:00Z", "trial_id": "t1"})
        self.assertTrue(seen[0].endswith("\n"))
        self.assertFalse((self.run_dir / LOCK_FILE).exists())

    def test_an_unremovable_lock_after_a_written_entry_reports_the_new_head(self):
        ledger = TrialLedger.open(self.run_dir)
        unlink = Path.unlink

        def stuck(path, *args, **kwargs):
            if path.name == LOCK_FILE:
                raise OSError("synthetic")
            return unlink(path, *args, **kwargs)

        with mock.patch.object(Path, "unlink", stuck):
            with self.assertRaises(UnlockError) as caught:
                self.append(ledger, "started", "t1")
        entries = verify(self.path)
        self.assertEqual(str(caught.exception), "ledger:unlock_failed")
        self.assertIsInstance(caught.exception, LedgerError)
        self.assertEqual(caught.exception.head, (0, entries[0]["entry_sha256"]))
        self.assertEqual(ledger.head, caught.exception.head)
        self.assertRefused("ledger:locked", self.append, ledger, "completed", "t1")
        (self.run_dir / LOCK_FILE).unlink()                           # a person, after checking no writer runs
        self.append(ledger, "completed", "t1")
        self.assertEqual([e["kind"] for e in verify(self.path)], ["started", "completed"])

    def test_an_append_that_would_overflow_the_file_bound_writes_nothing(self):
        sizer = Path(self.temp.name) / "sizer"
        sizer.mkdir()
        self.append(TrialLedger.open(sizer), "started", "t1")
        size = len((sizer / LEDGER_FILE).read_bytes())               # the same entry, byte for byte
        with mock.patch.object(trial_ledger, "MAX_LEDGER_BYTES", size - 1):
            self.assertRefused("entry:ledger_full", self.append, TrialLedger.open(self.run_dir), "started", "t1")
        self.assertFalse(self.path.exists())
        with mock.patch.object(trial_ledger, "MAX_LEDGER_BYTES", size):
            self.append(TrialLedger.open(self.run_dir), "started", "t1")
            self.assertEqual(TrialLedger.open(self.run_dir).head[0], 0)
        self.assertEqual(self.path.read_bytes(), (sizer / LEDGER_FILE).read_bytes())


class Uncertain(LedgerCase):
    """A failure once writing has begun (WriteUncertainError) leaves what was written and the old head; every refusal
    before the write is a plain LedgerError and leaves the file as it was."""

    def failing_write(self, keep):
        """_write_all that writes the first keep(len) bytes of the line, then fails as the OS would."""
        write = trial_ledger._write_all

        def partial(handle, data):
            write(handle, data[:keep(len(data))])
            raise OSError("synthetic")
        return partial

    def assertUncertain(self, code, function, *args, **kwargs):
        with self.assertRaises(WriteUncertainError) as caught:
            function(*args, **kwargs)
        self.assertEqual(str(caught.exception), code)
        self.assertFalse((self.run_dir / LOCK_FILE).exists())
        return caught.exception

    def test_a_failure_after_the_whole_line_leaves_a_started_trial_that_counts(self):
        self.contract = frozen(max_trials=2)
        ledger = TrialLedger.open(self.run_dir)
        with mock.patch.object(trial_ledger, "_write_all", self.failing_write(lambda size: size)):
            self.assertUncertain("ledger:write_failed", self.append, ledger, "started", "t1")
        self.assertIsNone(ledger.head)
        self.assertEqual([(e["kind"], e["trial_id"]) for e in verify(self.path)], [("started", "t1")])
        self.assertRefused("ledger:head_moved", self.append, ledger, "started", "t2")
        reopened = TrialLedger.open(self.run_dir)                       # reconcile: t1 is in, so close it, never
        self.assertRefused("entry:trial_already_started", self.append, reopened, "started", "t1")   # restart it
        self.append(reopened, "failed", "t1", summary={"failure": "write_uncertain"})
        self.append(reopened, "started", "t2", "seeded_random_runner")
        self.assertRefused("entry:max_trials_reached", self.append, reopened, "started", "t3")

    def test_a_failure_part_way_through_the_line_leaves_a_cut_line(self):
        ledger = TrialLedger.open(self.run_dir)
        with mock.patch.object(trial_ledger, "_write_all", self.failing_write(lambda size: size // 2)):
            self.assertUncertain("ledger:write_failed", self.append, ledger, "started", "t1")
        self.assertIsNone(ledger.head)
        self.assertTrue(self.path.read_bytes())
        self.assertRefused("line 1:truncated_line", verify, self.path)
        self.assertRefused("line 1:truncated_line", TrialLedger.open, self.run_dir)
        self.assertRefused("line 1:truncated_line", ledger.entries)
        self.assertRefused("ledger:head_moved", self.append, ledger, "started", "t1")

    def test_a_failure_part_way_through_a_later_line_keeps_the_earlier_entries_and_head(self):
        ledger = TrialLedger.open(self.run_dir)
        head = self.append(ledger, "started", "t1")
        before = self.path.read_bytes()
        with mock.patch.object(trial_ledger, "_write_all", self.failing_write(lambda size: size // 2)):
            self.assertUncertain("ledger:write_failed", self.append, ledger, "completed", "t1")
        self.assertEqual(ledger.head, head)
        self.assertTrue(self.path.read_bytes().startswith(before))
        self.assertRefused("line 2:truncated_line", verify, self.path)
        self.assertRefused("line 2:truncated_line", self.append, ledger, "completed", "t1")

    def test_a_write_that_makes_no_progress_is_uncertain(self):
        class Stalled:
            def write(self, data):
                return 0
        with self.assertRaises(WriteUncertainError) as caught:
            trial_ledger._write_all(Stalled(), b"line\n")
        self.assertEqual(str(caught.exception), "ledger:write_failed")

    def test_a_failing_confirmation_read_is_uncertain_and_keeps_its_cause(self):
        ledger = TrialLedger.open(self.run_dir)
        with mock.patch.object(trial_ledger, "verify", side_effect=LedgerError("ledger:unreadable")):
            error = self.assertUncertain("ledger:append_not_confirmed", self.append, ledger, "started", "t1")
        self.assertEqual(str(error.__cause__), "ledger:unreadable")
        self.assertIsNone(ledger.head)
        self.assertEqual([e["trial_id"] for e in verify(self.path)], ["t1"])

    def test_refusals_before_the_write_are_not_uncertain(self):
        ledger = TrialLedger.open(self.run_dir)
        other = TrialLedger.open(self.run_dir)
        self.append(other, "started", "t1")
        before = self.path.read_bytes()
        refusals = [(ledger, "started", "t2", "ledger:head_moved"),
                    (other, "completed", "t9", "entry:trial_not_started"),
                    (other, "started", "t1", "entry:trial_already_started")]
        for handle, kind, trial_id, code in refusals:
            with self.subTest(code=code):
                with self.assertRaises(LedgerError) as caught:
                    self.append(handle, kind, trial_id)
                self.assertNotIsInstance(caught.exception, WriteUncertainError)
                self.assertEqual(str(caught.exception), code)
                self.assertEqual(self.path.read_bytes(), before)
        (self.run_dir / LOCK_FILE).write_bytes(b"")
        with self.assertRaises(LedgerError) as caught:
            self.append(other, "completed", "t1")
        self.assertNotIsInstance(caught.exception, WriteUncertainError)
        self.assertEqual(str(caught.exception), "ledger:locked")
        self.assertEqual(self.path.read_bytes(), before)


class StaleEmptyHandle(LedgerCase):
    """A handle opened while the ledger was missing returns () only while it is still missing."""

    def test_it_reads_the_entries_another_handle_wrote(self):
        self.contract = frozen(max_trials=2)
        stale, writer = TrialLedger.open(self.run_dir), TrialLedger.open(self.run_dir)
        self.assertEqual(stale.entries(), ())
        self.append(writer, "started", "t1")
        self.append(writer, "started", "t2", "seeded_random_runner")
        self.assertEqual(len(writer.entries()), 2)
        self.assertEqual(stale.entries(), verify(self.path))
        self.assertEqual(len(stale.entries()), 2)
        self.assertIsNone(stale.head)                                   # reading never moves the append head
        self.assertRefused("ledger:head_moved", self.append, stale, "started", "t3")
        self.assertRefused("entry:max_trials_reached", self.append, writer, "started", "t3")

    def test_it_refuses_a_malformed_cut_or_empty_ledger_that_appeared(self):
        stale = TrialLedger.open(self.run_dir)
        sizer = Path(self.temp.name) / "sizer"
        sizer.mkdir()
        self.append(TrialLedger.open(sizer), "started", "t1")
        line = (sizer / LEDGER_FILE).read_bytes()
        for data, code in ((b"not json\n", "line 1:not_strict_json"), (line[:len(line) // 2], "line 1:truncated_line"),
                           (b"", "ledger:empty")):
            with self.subTest(code=code):
                self.path.write_bytes(data)
                self.assertRefused(code, stale.entries)
        self.path.unlink()
        self.assertEqual(stale.entries(), ())

    def test_it_refuses_a_symlink_that_appeared_even_a_dangling_one(self):
        stale = TrialLedger.open(self.run_dir)
        target = Path(self.temp.name) / "elsewhere"
        self.path.symlink_to(target)
        self.assertRefused("ledger:symlink", stale.entries)
        target.write_bytes(b"")
        self.assertRefused("ledger:symlink", stale.entries)


class Uninspectable(LedgerCase):
    """A ledger path that cannot be inspected is ledger:uninspectable, never an empty or missing ledger: Path.exists
    and is_symlink both answer False when the directory has lost search permission."""

    def test_a_directory_without_search_permission_is_never_an_empty_ledger(self):
        self.contract = frozen(max_trials=2)
        stale, writer = TrialLedger.open(self.run_dir), TrialLedger.open(self.run_dir)
        self.append(writer, "started", "t1")
        self.append(writer, "started", "t2", "seeded_random_runner")
        self.run_dir.chmod(0o600)
        try:
            try:
                self.path.lstat()
            except PermissionError:
                pass
            else:
                self.skipTest("this user can search a directory without permission (root)")
            self.assertRefused("ledger:uninspectable", stale.entries)
            self.assertRefused("ledger:uninspectable", writer.entries)
            self.assertRefused("ledger:uninspectable", verify, self.path)
            self.assertRefused("ledger:uninspectable", TrialLedger.open, self.run_dir)
            self.assertRefused("ledger:lock_failed", self.append, stale, "started", "t3")
        finally:
            self.run_dir.chmod(0o700)
        self.assertEqual(len(stale.entries()), 2)
        self.assertIsNone(stale.head)                                   # reading never moves the append head
        self.assertEqual(stale.entries(), verify(self.path))

    def test_every_inspection_error_but_file_not_found_is_uninspectable(self):
        stale = TrialLedger.open(self.run_dir)
        self.append(TrialLedger.open(self.run_dir), "started", "t1")
        kind = type(self.path)
        lstat = kind.lstat
        for error in (PermissionError(13, "synthetic"), NotADirectoryError(20, "synthetic"), OSError(5, "synthetic")):
            def failing(path, *args, **kwargs):
                if path.name == LEDGER_FILE:
                    raise error
                return lstat(path, *args, **kwargs)
            with self.subTest(error=type(error).__name__), mock.patch.object(kind, "lstat", failing):
                self.assertRefused("ledger:uninspectable", stale.entries)
                self.assertRefused("ledger:uninspectable", verify, self.path)
                self.assertRefused("ledger:uninspectable", TrialLedger.open, self.run_dir)
        self.assertEqual([e["trial_id"] for e in stale.entries()], ["t1"])
        self.path.unlink()
        self.assertEqual(stale.entries(), ())                           # FileNotFoundError alone is absence
        self.assertIsNone(TrialLedger.open(self.run_dir).head)


class Entries(LedgerCase):
    def test_lifecycle(self):
        ledger = TrialLedger.open(self.run_dir)
        self.assertRefused("entry:trial_not_started", self.append, ledger, "aborted")
        self.append(ledger, "started")
        self.assertRefused("entry:trial_already_started", self.append, ledger, "started")
        self.assertRefused("entry:trial_mismatch", self.append, ledger, "completed", variant="seeded_random_runner")
        self.assertRefused("entry:trial_mismatch", self.append, ledger, "completed", code_identity="git:other")
        self.assertRefused("entry:trial_mismatch", self.append, ledger, "completed", input_sha256="cd" * 32)
        self.assertRefused("entry:trial_mismatch", self.append, ledger, "completed", contract=frozen(max_trials=5))
        self.append(ledger, "aborted", summary={"reason": "operator_stop"})
        for kind in ("completed", "failed", "aborted", "started"):
            with self.subTest(kind=kind):
                self.assertRefused("entry:trial_already_closed" if kind != "started" else
                                   "entry:trial_already_started", self.append, ledger, kind)
        self.assertEqual([e["kind"] for e in verify(self.path)], ["started", "aborted"])

    def test_verify_refuses_a_closing_entry_that_differs_from_its_start_in_any_fixed_key(self):
        self.three()
        entries = verify(self.path)
        exposure = {"exposed_months": {}, "not_prospective_dates": {}}             # well formed, but not the start's
        for key, value in (("contract_sha256", "cd" * 32), ("seeds", {"bootstrap": 3, "selection": 1}),
                           ("exposure_record", exposure), ("variant", "seeded_random_runner"),
                           ("input_sha256", "cd" * 32), ("code_identity", "git:other")):
            with self.subTest(key=key):
                changed = [dict(e) for e in entries]
                changed[2][key] = value                                     # entry 2 closes t1, started at 0
                self.write_lines(rehashed(changed, 2))
                self.assertRefused("line 3:trial_mismatch", verify, self.path)
        # training_cutoff is fixed too, but schema v1 allows only null in every entry, so it cannot differ here.

    def test_the_trial_budget_counts_started_entries_of_one_contract(self):
        ledger = TrialLedger.open(self.run_dir)
        self.contract = frozen(max_trials=2)
        self.append(ledger, "started", "t1")
        self.append(ledger, "failed", "t1", summary={"failure": "loader_refused"})
        self.append(ledger, "started", "t2", "seeded_random_runner")
        before = self.path.read_bytes()
        self.assertRefused("entry:max_trials_reached", self.append, ledger, "started", "t3")
        self.assertEqual(self.path.read_bytes(), before)                  # a failed trial still spent its place
        self.append(ledger, "aborted", "t2", "seeded_random_runner")      # closing is never refused by the budget
        other = frozen(max_trials=3)                                     # another contract has its own budget
        for trial_id in ("t4", "t5", "t6"):
            self.append(ledger, "started", trial_id, contract=other)
        self.assertRefused("entry:max_trials_reached", self.append, ledger, "started", "t7", contract=other)
        self.assertEqual([e["trial_id"] for e in verify(self.path)], ["t1", "t1", "t2", "t2", "t4", "t5", "t6"])

    def test_values_that_change_when_serialised_are_refused_before_writing(self):
        ledger = TrialLedger.open(self.run_dir)
        for summary, code in (({"interval": (1, 2)}, "entry:summary_value_not_allowed"),
                              ({"a": [(1,)]}, "entry:summary_value_not_allowed"),
                              ({True: 1}, "entry:summary_key_malformed"), ({None: 1}, "entry:summary_key_malformed"),
                              ({1: "x"}, "entry:summary_key_malformed"),
                              ({"prior_head": (0, "ab" * 32)}, "entry:summary_value_not_allowed")):
            with self.subTest(summary=summary):
                self.assertRefused(code, self.append, ledger, "started", summary=summary)
                self.assertFalse(self.path.exists())
                self.assertIsNone(ledger.head)
                self.assertFalse((self.run_dir / LOCK_FILE).exists())
        head = self.append(ledger, "started", summary={"prior_head": [0, "ab" * 32]})       # the list form is fine
        self.assertEqual(ledger.head, head)

    def test_only_a_valid_frozen_contract_and_its_variants(self):
        ledger = TrialLedger.open(self.run_dir)
        self.assertRefused("contract:not_valid_frozen", self.append, ledger, "started", contract=draft())
        broken = frozen()
        broken["seeds"]["bootstrap"] = -1
        self.assertRefused("contract:not_valid_frozen", self.append, ledger, "started", contract=broken)
        self.assertRefused("variant:not_in_contract", self.append, ledger, "started", variant="other")
        self.assertFalse(self.path.exists())

    def test_field_refusals_write_nothing(self):
        ledger = TrialLedger.open(self.run_dir)
        cases = ((dict(kind="paused"), "entry:kind_not_allowed"),
                 (dict(trial_id="T 1"), "entry:trial_id_malformed"),
                 (dict(recorded_at="2026-10-14T12:00:00"), "entry:recorded_at_malformed"),
                 (dict(recorded_at="2026-02-30T12:00:00Z"), "entry:recorded_at_malformed"),
                 (dict(code_identity="git:abc\nforged"), "entry:code_identity_malformed"),
                 (dict(code_identity=""), "entry:code_identity_malformed"),
                 (dict(input_sha256="AB" * 32), "entry:input_sha256_malformed"),
                 (dict(input_sha256=None), "entry:input_sha256_malformed"),
                 (dict(summary={"roi": 0.1}), "entry:not_strict_json"),
                 (dict(summary={"roi": float("nan")}), "entry:not_canonical_json"),
                 (dict(summary={"Bad Key": 1}), "entry:summary_key_malformed"),
                 (dict(summary={"a": {"b": {"c": {"d": {"e": [1]}}}}}), "entry:summary_too_deep"),
                 (dict(summary={"text": "x" * 1025}), "entry:summary_text_too_long"),
                 (dict(summary={f"k{i}": "x" * 1000 for i in range(9)}), "entry:summary_too_large"),
                 (dict(summary=[1]), "entry:summary_not_an_object"),
                 (dict(summary={"n": 2 ** 64}), "entry:not_strict_json"))
        for change, code in cases:
            with self.subTest(code=code, change=str(change)[:40]):
                kind = change.pop("kind", "started")
                self.assertRefused(code, self.append, ledger, kind, **change)
        self.assertFalse(self.path.exists())
        self.assertEqual(list(self.run_dir.iterdir()), [])

    def test_verify_refuses_a_rehashed_entry_with_a_bad_exposure_record_or_cutoff(self):
        self.three()
        lines = self.lines()
        for change, reason in ((dict(training_cutoff="2015-12-31"), "training_cutoff_not_null"),
                               (dict(exposure_record={"exposed_months": {}}), "exposure_record_malformed"),
                               (dict(exposure_record={"exposed_months": {}, "not_prospective_dates": {"x": 1}}),
                                "exposure_record_malformed")):
            with self.subTest(reason=reason):
                entry = dict(json.loads(lines[0]), **change)
                entry["entry_sha256"] = entry_digest(entry)
                self.write_lines([canonical_json(entry)] + lines[1:])
                self.assertRefused(f"line 1:{reason}", verify, self.path)

    def test_a_fitted_contract_is_refused_as_not_valid_frozen(self):
        self.contract["fitting"]["kind"] = "walk_forward"                 # validate_contract: deferred
        self.assertRefused("contract:not_valid_frozen", self.append, TrialLedger.open(self.run_dir), "started")

    def test_module_reads_no_clock_and_starts_no_process(self):
        source = Path(trial_ledger.__file__).read_text(encoding="utf-8")
        for word in ("import time", "import os", "subprocess", "now(", "today(", "fsync"):
            self.assertNotIn(word, source)


if __name__ == "__main__":
    unittest.main()
