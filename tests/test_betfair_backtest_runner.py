"""The runner core (betangel_research.betfair.backtest.runner) on synthetic archives in temporary roots.

Every archive is synthetic and goes through the real ingest and index (helpers from
tests/test_betfair_backtest_replay.py); the contract is the P0 draft frozen with test values. These show preflight
refusals before any trial starts (guarded paths, roots, contract, run id, inputs, duplicates, lock, ledger anchors,
unfinished trials, budget), a completed run's artifacts, ledger entries and receipt, a failure closing its trials, an
uncertain ledger write keeping the lock, reconciliation, and the CLI. They say nothing about real files or results.
"""

from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import os
import io
import json
from pathlib import Path
import contextlib
import dataclasses
import hashlib
import shutil
import tempfile
import unittest
from unittest import mock

from betangel_research.betfair.backtest import certificate, engineering, replay, runner, trial_ledger
from betangel_research.betfair.backtest.__main__ import main
from betangel_research.betfair.backtest.archive import DEFAULT_LIMITS, ingest_archive, safe_target
from betangel_research.betfair.backtest.cohort import CohortRules, decide_records, ledger_lines
from betangel_research.betfair.backtest.contract import contract_sha256
from betangel_research.betfair.backtest.market_index import index_archives, load_manifest
from betangel_research.betfair.backtest.replay import replay_view
from betangel_research.betfair.backtest.trial_ledger import LEDGER_FILE, TrialLedger, WriteUncertainError, verify

import test_betfair_backtest_replay as fx
from test_betfair_backtest_replay import OFF, path

CODE = "git:test clean"
IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0


def clock():
    clock.calls += 1
    return f"2026-10-15T00:00:{clock.calls % 60:02d}Z"


clock.calls = 0


def canned_certificate(descriptor=None, interpreter=None, results=None, evidence="case"):
    """A certified certificate for these sources and this interpreter (or those given), from canned case results:
    the runner cannot tell it from a real one; the fresh-process control in tests/test_betfair_backtest_certificate.py
    binds a real one."""
    cases = [{"id": case_id, "kind": kind, "result": (results or {}).get(case_id, certificate.AS_EXPECTED),
              "observed": {evidence: case_id}, "expected": {evidence: case_id}}
             for case_id, kind, _ in certificate.CASES]
    return certificate.build_certificate(cases, descriptor or certificate.source_descriptor()[0],
                                         interpreter or certificate.interpreter_descriptor())


CERTIFICATE = canned_certificate()


def market(market_id, off=OFF):
    return fx.standard_market(market_id, off=off)


class RunnerCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.data = self.tmp / "data"
        self.lab = self.tmp / "lab"
        self.data.mkdir()
        self.lab.mkdir()
        self.contract_value = fx.frozen_contract()
        self.contract = self.write_contract(self.contract_value)
        self.digest = contract_sha256(self.contract_value)
        self.tars = 0
        self.install_certificate()

    def install_certificate(self, value=CERTIFICATE):
        """Store a certificate in the lab-state root as certify-engine would; its sha256 is kept."""
        self.certificate_sha256 = certificate.store_certificate(self.lab, value)[2]

    def write_contract(self, value, name="contract.json"):
        target = self.tmp / name
        target.write_bytes(json.dumps(value, indent=1).encode())
        return target

    def ingest(self, members, root=None):
        self.tars += 1
        tar = fx.make_tar(self.tmp / f"archive{self.tars}.tar",
                          [(name, fx.bz2.compress(fx.lines(messages))) for name, messages in members])
        result = ingest_archive(tar, root or self.data)
        self.assertEqual(result["status"], "ingested", result)
        self.assertEqual(index_archives(root or self.data)["status"], "indexed")
        return result["archive_sha256"]

    def run_once(self, run_id="r1", **kwargs):
        arguments = dict(contract_path=self.contract, data_root=self.data, lab_state_root=self.lab, run_id=run_id,
                         code_identity=CODE, clock=clock)
        arguments.update(kwargs)
        return runner.run_backtest(**arguments)

    def ledger(self):
        return verify(self.lab / "contracts" / self.digest / LEDGER_FILE)

    def assertRefused(self, result, problem):
        self.assertEqual(result["status"], "refused", result)
        self.assertTrue(result["problems"][0].startswith(problem), result)

    def standard_inputs(self):
        self.ingest([(path("1.100000001"), market("1.100000001")),
                     (path("1.100000002"), market("1.100000002", off=OFF + timedelta(hours=1)))])

    def tied_inputs(self, markets=2):
        """standard_inputs with the favourite tied in every market: ltp_favourite issues nothing, so engineering
        acceptance is incomplete and the contract may run again (a pass would stop it)."""
        tie = {1: 2.5, 2: 2.5, 3: 6.0}
        self.ingest([(path(f"1.10000000{n}"), fx.standard_market(f"1.10000000{n}", off=OFF + timedelta(hours=n - 1),
                                                                  ltps=tie)) for n in range(1, markets + 1)])

    def lab_entries(self):
        """Every entry of the lab-state root but the stored certificates (the runner only reads those)."""
        return sorted(str(item.relative_to(self.lab)) for item in self.lab.rglob("*")
                      if item.relative_to(self.lab).parts[0] != certificate.CERTIFICATIONS_DIR)

    def fresh_roots(self, name):
        """A new data root and lab-state root, so one case never sees another's archives or starts."""
        self.data, self.lab = self.tmp / f"data-{name}", self.tmp / f"lab-{name}"
        self.data.mkdir()
        self.lab.mkdir()
        self.install_certificate()

    def other_sources(self):
        """certificate.source_descriptor patched to describe other sources (one file's digest changed): a new key."""
        descriptor, _ = certificate.source_descriptor()
        changed = dict(descriptor, files=[dict(descriptor["files"][0], sha256="0" * 64), *descriptor["files"][1:]])
        return mock.patch.object(certificate, "source_descriptor",
                                 lambda root=None: (changed, certificate.descriptor_sha256(changed))), changed

    def refused_unchanged(self, problem, run_id="r2", **kwargs):
        """The run refuses with exactly problem and writes nothing."""
        before = self.lab_files()
        self.assertEqual(self.run_once(run_id, **kwargs), {"status": "refused", "problems": [problem]})
        self.assertEqual(self.lab_files(), before)

    def rewrite_records(self, sha, edit):
        """Rewrite one archive's markets file through edit(list of records) and re-point its head at the bytes."""
        markets, head_path = self.data / "index" / sha / "markets-v1.jsonl", self.data / "index" / sha / "head-v1.json"
        records = [json.loads(line) for line in markets.read_bytes().splitlines()]
        edit(records)
        data = b"".join(runner.canonical_json(record) + b"\n" for record in records)
        head = json.loads(head_path.read_bytes())
        for item, value in ((markets, data), (head_path, runner.canonical_json(
                dict(head, markets=len(records), markets_sha256=runner._sha256_bytes(data))) + b"\n")):
            item.chmod(0o600)
            item.write_bytes(value)

    def run_file(self, name, run_id="r1"):
        return self.lab / "runs" / self.digest / run_id / name

    def lab_files(self):
        return {str(item): item.read_bytes() for item in self.lab.rglob("*") if item.is_file()}


class CompletedRunTests(RunnerCase):
    def test_a_completed_run_writes_its_artifacts_ledger_and_receipt(self):
        self.standard_inputs()
        result = self.run_once()
        self.assertEqual(result["status"], "completed", result)
        run_dir = self.lab / "runs" / self.digest / "r1"
        results = json.loads((run_dir / "results.json").read_bytes())
        receipt = json.loads((run_dir / "receipt.json").read_bytes())
        self.assertEqual(result["results_sha256"], runner._sha256_bytes((run_dir / "results.json").read_bytes()))
        self.assertEqual(results["contract_sha256"], self.digest)
        self.assertEqual(results["population"]["records"], 2)
        self.assertEqual(results["population"]["by_class"], {"eligible": 2})
        self.assertEqual(results["engineering_acceptance"]["status"], "passed")
        for variant in ("ltp_favourite", "seeded_random_runner"):
            self.assertEqual(results["issuance"]["by_variant"][variant]["issued"], 2)
            self.assertEqual(results["settlement"]["unresolved_reasons"][variant], {"void_unknown": 2})
        entries = self.ledger()
        self.assertEqual([(e["kind"], e["trial_id"]) for e in entries],
                         [("started", "r1.ltp_favourite"), ("started", "r1.seeded_random_runner"),
                          ("completed", "r1.ltp_favourite"), ("completed", "r1.seeded_random_runner")])
        self.assertEqual({e["summary"].get("results_sha256") for e in entries[2:]}, {result["results_sha256"]})
        self.assertEqual({e["input_sha256"] for e in entries}, {results["input_sha256"]})
        self.assertEqual(receipt["ledger_head"], [3, entries[3]["entry_sha256"]])
        self.assertEqual(receipt["cumulative_starts"], 2)
        self.assertEqual([t["started"][0] for t in receipt["trials"]], [0, 1])
        self.assertFalse((self.lab / "contracts" / self.digest / runner.LOCK_NAME).exists())
        # The issuance ledger is the cohort's, decided independently here.
        rules = CohortRules.from_contract(self.contract_value)
        records, manifests = [], {}
        for line in (self.data / "index").glob("*/markets-v1.jsonl"):
            records += [json.loads(item) for item in line.read_bytes().splitlines()]
        sha = records[0]["provenance"]["archive_sha256"]
        manifest, _ = load_manifest(self.data, sha)
        decisions = decide_records(rules, records, lambda r: replay_view(self.data, manifest, r, rules.offset_s))
        self.assertEqual((run_dir / "issuance-ledger.jsonl").read_bytes(), b"".join(ledger_lines(decisions)))

    def test_results_hold_no_wall_clock_and_repeat_but_for_run_id(self):
        self.tied_inputs()                                  # incomplete: a pass would stop the contract
        self.contract = self.write_contract(fx.frozen_contract(max_trials=6), "six.json")
        self.digest = contract_sha256(fx.frozen_contract(max_trials=6))
        first, second = self.run_once("ra"), self.run_once("rb")
        self.assertEqual((first["status"], second["status"]), ("completed", "completed"))
        a = json.loads((self.lab / "runs" / self.digest / "ra" / "results.json").read_bytes())
        b = json.loads((self.lab / "runs" / self.digest / "rb" / "results.json").read_bytes())
        self.assertEqual(dict(a, run_id=None), dict(b, run_id=None))
        self.assertEqual(second["cumulative_starts"], 4)


class BudgetAndLedgerTests(RunnerCase):
    def test_the_budget_counts_every_start_and_a_run_id_is_never_reused(self):
        self.tied_inputs()
        self.assertEqual(self.run_once("r1")["status"], "completed")
        self.assertRefused(self.run_once("r1"), "run_id:already_used")
        self.assertEqual(self.run_once("r2")["status"], "completed")
        before = self.ledger()
        self.assertRefused(self.run_once("r3"), "ledger:trial_budget_exhausted")
        self.assertEqual(self.ledger(), before)                         # a refusal starts nothing
        self.assertFalse((self.lab / "runs" / self.digest / "r3").exists())

    def test_a_held_lock_refuses_and_starts_nothing(self):
        self.standard_inputs()
        contract_dir = self.lab / "contracts" / self.digest
        contract_dir.mkdir(parents=True)
        (contract_dir / runner.LOCK_NAME).write_bytes(b"someone\n")
        self.assertRefused(self.run_once(), "runner_lock:held")
        self.assertFalse((contract_dir / LEDGER_FILE).exists())

    def test_a_ledger_deleted_or_cut_behind_a_receipt_refuses(self):
        self.contract = self.write_contract(fx.frozen_contract(max_trials=6), "six.json")
        self.digest = contract_sha256(fx.frozen_contract(max_trials=6))
        self.tied_inputs()
        self.assertEqual(self.run_once("r1")["status"], "completed")
        ledger = self.lab / "contracts" / self.digest / LEDGER_FILE
        kept = ledger.read_bytes()
        ledger.write_bytes(b"".join(kept.splitlines(keepends=True)[:2]))
        self.assertRefused(self.run_once("r2"), "ledger:ledger:truncated_before_anchor")
        ledger.unlink()
        self.assertRefused(self.run_once("r2"), "ledger:ledger:truncated_before_anchor")
        ledger.write_bytes(kept)
        self.assertEqual(self.run_once("r2")["status"], "completed")

    def test_unfinished_trials_refuse_until_reconciled(self):
        self.contract = self.write_contract(fx.frozen_contract(max_trials=6), "six.json")
        self.contract_value = fx.frozen_contract(max_trials=6)
        self.digest = contract_sha256(self.contract_value)
        self.tied_inputs()
        contract_dir = self.lab / "contracts" / self.digest
        contract_dir.mkdir(parents=True)
        ledger = TrialLedger.open(contract_dir)
        ledger.append("started", trial_id="crashed.ltp_favourite", contract=self.contract_value,
                      variant="ltp_favourite",
                      input_sha256="ab" * 32, code_identity="git:old", recorded_at="2026-10-15T00:00:00Z", summary={})
        self.assertRefused(self.run_once(), "ledger:unfinished_trials:crashed.ltp_favourite")
        (self.lab / "runs" / self.digest / "crashed").mkdir(parents=True)
        reconciled = runner.reconcile(self.contract, self.lab, "reconciler", clock=clock)
        self.assertEqual((reconciled["status"], reconciled["aborted"], reconciled["receipted"]),
                         ("reconciled", ["crashed.ltp_favourite"], ["crashed"]))
        aborted = self.ledger()[-1]
        self.assertEqual((aborted["kind"], aborted["code_identity"], aborted["input_sha256"]),
                         ("aborted", "git:old", "ab" * 32))
        self.assertEqual(aborted["summary"], {"reason": "reconciled_unfinished", "reconciled_by": "reconciler",
                                              "pending": ["crashed.ltp_favourite"], "certificate_sha256": None})
        receipt = json.loads((self.lab / "runs" / self.digest / "crashed" / "receipt.json").read_bytes())
        self.assertEqual((receipt["status"], receipt["reconciled_by"], receipt["results_sha256"]),
                         ("aborted", "reconciler", None))
        result = self.run_once()
        self.assertEqual((result["status"], result["cumulative_starts"]), ("completed", 3))   # the aborted one counts
        self.assertEqual(self.run_once("r9")["cumulative_starts"], 5)
        self.assertRefused(self.run_once("r10"), "ledger:trial_budget_exhausted")             # 5 + 2 > 6

    def test_an_uncertain_ledger_write_keeps_the_lock(self):
        self.standard_inputs()
        real = TrialLedger.append

        def failing(ledger, kind, **kwargs):
            if kind == "completed":
                raise WriteUncertainError("ledger:write_failed")
            return real(ledger, kind, **kwargs)

        with mock.patch.object(TrialLedger, "append", failing):
            result = self.run_once()
        self.assertEqual(result["status"], "uncertain", result)
        self.assertTrue((self.lab / "contracts" / self.digest / runner.LOCK_NAME).exists())
        self.assertRefused(self.run_once("r2"), "runner_lock:held")


class ReceiptTests(RunnerCase):
    def setUp(self):
        super().setUp()
        self.contract_value = fx.frozen_contract(max_trials=8)
        self.contract = self.write_contract(self.contract_value, "eight.json")
        self.digest = contract_sha256(self.contract_value)
        self.tied_inputs()
        self.assertEqual(self.run_once("r1")["status"], "completed")
        self.run_dir = self.lab / "runs" / self.digest / "r1"

    def test_a_missing_receipt_refuses_until_reconcile_publishes_it(self):
        kept = (self.run_dir / "receipt.json").read_bytes()
        (self.run_dir / "receipt.json").unlink()
        self.assertRefused(self.run_once("r2"), "receipt:missing:r1")
        result = runner.reconcile(self.contract, self.lab, "reconciler", clock=clock)
        self.assertEqual((result["status"], result["aborted"], result["receipted"]), ("reconciled", [], ["r1"]))
        receipt = json.loads((self.run_dir / "receipt.json").read_bytes())
        original = json.loads(kept)
        self.assertEqual((receipt["status"], receipt["results_sha256"], receipt["reconciled_by"]),
                         ("completed", original["results_sha256"], "reconciler"))
        self.assertEqual([(t["started"], t["closed"]) for t in receipt["trials"]],
                         [(t["started"], t["closed"]) for t in original["trials"]])
        self.assertEqual(self.run_once("r2")["status"], "completed")

    def test_a_receipt_that_does_not_match_its_ledger_or_results_refuses(self):
        receipt_path = self.run_dir / "receipt.json"
        kept = receipt_path.read_bytes()
        receipt = json.loads(kept)
        cases = {"receipt:r1:malformed": kept + b" ",
                 # the highest head is the one the ledger is opened at, so a wrong one refuses there
                 "ledger:ledger:anchor_mismatch": runner.canonical_json(dict(receipt, ledger_head=[3, "0" * 64]))
                 + b"\n",
                 "receipt:r1:trial_not_in_ledger": runner.canonical_json(dict(receipt, trials=[
                     dict(receipt["trials"][0], closed=receipt["trials"][0]["started"]), receipt["trials"][1]]))
                 + b"\n"}
        for problem, data in cases.items():
            with self.subTest(problem=problem):
                receipt_path.write_bytes(data)
                self.assertRefused(self.run_once("r2"), problem)
        receipt_path.write_bytes(kept)
        self.assertEqual(self.run_once("r2")["status"], "completed")         # now r2's receipt holds the top head
        receipt_path.write_bytes(runner.canonical_json(dict(receipt, ledger_head=[3, "0" * 64])) + b"\n")
        self.assertRefused(self.run_once("r3"), "receipt:r1:head_not_in_ledger")
        receipt_path.write_bytes(kept)
        results = self.run_dir / "results.json"
        kept_results = results.read_bytes()
        results.write_bytes(kept_results + b" ")
        self.assertRefused(self.run_once("r3"), "receipt:r1:results_sha256_mismatch")
        results.write_bytes(kept_results)
        self.assertEqual(self.run_once("r3")["status"], "completed")

    def test_a_receipt_write_failure_is_uncertain_and_keeps_the_lock(self):
        real = runner._write_new

        def failing(path, data, where):
            if where == "receipt":
                raise OSError("receipt:write_failed")
            return real(path, data, where)

        with mock.patch.object(runner, "_write_new", failing):
            result = self.run_once("r2")
        self.assertEqual(result["status"], "uncertain", result)
        lock = self.lab / "contracts" / self.digest / runner.LOCK_NAME
        self.assertTrue(lock.exists())
        lock.unlink()                                                    # a person, after checking no runner runs
        self.assertRefused(self.run_once("r3"), "receipt:missing:r2")
        self.assertEqual(runner.reconcile(self.contract, self.lab, "reconciler", clock=clock)["receipted"], ["r2"])


class FailureTests(RunnerCase):
    def test_a_results_write_failure_closes_both_trials_as_failed(self):
        self.standard_inputs()
        real = runner._write_new

        def failing(path, data, where):
            if where == "results":
                raise OSError("results:write_failed")
            return real(path, data, where)

        with mock.patch.object(runner, "_write_new", failing):
            result = self.run_once()
        self.assertEqual(result["status"], "failed", result)
        self.assertEqual([e["kind"] for e in self.ledger()], ["started", "started", "failed", "failed"])
        receipt = json.loads((self.lab / "runs" / self.digest / "r1" / "receipt.json").read_bytes())
        self.assertEqual((receipt["status"], receipt["results_sha256"]), ("failed", None))

    def test_only_the_p0_model_with_no_delay_runs(self):
        self.standard_inputs()
        delayed = self.write_contract(fx.frozen_contract(delay_s=1), "delayed.json")
        self.assertRefused(self.run_once(contract_path=delayed), "contract:not_supported:delay_s_other_than_0")

    def test_a_failure_after_the_starts_closes_both_trials_as_failed(self):
        sha = self.ingest([(path("1.100000001"), market("1.100000001"))])
        raw = safe_target(self.data, sha, path("1.100000001"))
        raw.chmod(0o600)
        raw.write_bytes(fx.bz2.compress(fx.lines(market("1.100000001", off=OFF))) + b"x")
        result = self.run_once()
        self.assertEqual(result["status"], "failed", result)
        self.assertIn("member_hash_mismatch", result["problems"][0])
        entries = self.ledger()
        self.assertEqual([e["kind"] for e in entries], ["started", "started", "failed", "failed"])
        self.assertIn("member_hash_mismatch", entries[2]["summary"]["failure"])
        receipt = json.loads((self.lab / "runs" / self.digest / "r1" / "receipt.json").read_bytes())
        self.assertEqual((receipt["status"], receipt["results_sha256"]), ("failed", None))
        self.assertFalse((self.lab / "runs" / self.digest / "r1" / "results.json").exists())
        self.assertFalse((self.lab / "contracts" / self.digest / runner.LOCK_NAME).exists())


class PreflightTests(RunnerCase):
    def test_roots_contract_and_identifiers(self):
        self.standard_inputs()
        draft = json.loads(fx.DRAFT.read_text(encoding="utf-8"))
        cases = [
            (dict(lab_state_root=self.data), "roots:not_distinct"),
            (dict(lab_state_root=self.data / "index"), "roots:not_distinct"),
            (dict(data_root=self.tmp), "roots:not_distinct"),
            (dict(contract_path=self.write_contract(draft, "draft.json")), "contract:not_valid_frozen"),
            (dict(contract_path=self.tmp / "missing.json"), "contract:missing_path"),
            (dict(run_id="../x"), "run_id:malformed"),
            (dict(run_id="R1"), "run_id:malformed"),
            (dict(code_identity="ünicode"), "code_identity:malformed"),
            (dict(lab_state_root=self.tmp / "nothing"), "lab_state_root:missing_path"),
        ]
        for changes, problem in cases:
            with self.subTest(problem=problem, changes=sorted(changes)):
                self.assertRefused(self.run_once(**changes), problem)
        self.assertFalse((self.lab / "contracts").exists())

    def test_denied_trees_are_refused_before_anything_reads_them(self):
        self.standard_inputs()
        denied = self.tmp.joinpath("Library", "Application Support", "betangel-research", "betfair")
        denied.mkdir(parents=True)
        (self.tmp / "lablink").symlink_to(denied)
        for changes, problem in ((dict(lab_state_root=denied), "lab_state_root:denied_live_state_tree"),
                                 (dict(lab_state_root=self.tmp / "lablink"), "lab_state_root:denied_live_state_tree"),
                                 (dict(contract_path=denied), "contract:denied_live_state_tree")):
            with self.subTest(problem=problem):
                with mock.patch.object(runner, "load_contract", side_effect=AssertionError("read")):
                    self.assertRefused(self.run_once(**changes), problem)
        (self.data / "manifests").rename(self.tmp / "manifests-real")
        (self.data / "manifests").symlink_to(denied)
        self.assertRefused(self.run_once(), "manifests:denied_live_state_tree")

    def test_inputs_are_verified_before_any_start(self):
        sha = self.ingest([(path("1.100000001"), market("1.100000001"))])
        markets = self.data / "index" / sha / "markets-v1.jsonl"
        kept = markets.read_bytes()
        markets.chmod(0o600)
        markets.write_bytes(kept + b"\n")
        self.assertRefused(self.run_once(), f"input:{sha}:markets_mismatch")
        markets.write_bytes(kept)
        head = self.data / "index" / sha / "head-v1.json"
        head_bytes = head.read_bytes()
        head.chmod(0o600)
        head.write_bytes(head_bytes.replace(b'"markets":1', b'"markets":2'))
        self.assertRefused(self.run_once(), f"input:{sha}:markets_mismatch")
        head.write_bytes(head_bytes + b" ")
        self.assertRefused(self.run_once(), f"input:{sha}:head_not_canonical")
        head.write_bytes(head_bytes)
        self.assertFalse((self.lab / "contracts" / self.digest / LEDGER_FILE).exists())

    def test_no_archive_or_a_duplicate_market_across_archives_refuses(self):
        self.assertRefused(self.run_once(), "inputs:no_archive")
        self.ingest([(path("1.100000001"), market("1.100000001"))])
        other = self.tmp / "second.tar"
        tar = fx.make_tar(other, [(path("1.100000001", event="27000002"),
                                   fx.bz2.compress(fx.lines(market("1.100000001"))))])
        self.assertEqual(ingest_archive(tar, self.data)["status"], "ingested")
        self.assertEqual(index_archives(self.data)["status"], "indexed")
        self.assertRefused(self.run_once(), "cohort:1.100000001:duplicate_market")
        self.assertFalse((self.lab / "contracts" / self.digest / LEDGER_FILE).exists())

    def test_of_any_two_preflight_problems_the_earlier_in_the_agreed_order_refuses(self):
        value = fx.frozen_contract()
        value["population"]["countries"] = ["GB"]              # valid, frozen and runnable, but not P0's profile
        outside = self.write_contract(value, "gb.json")
        order = [("profile", "contract:not_supported:not_the_certified_profile"), ("roots", "roots:not_distinct"),
                 ("run_id", "run_id:malformed"), ("certificate", "certification:missing"),
                 ("known_answers", "engineering:known_accounting_answers_error"), ("inputs", "inputs:no_archive")]
        changes = {"profile": dict(contract_path=outside), "roots": dict(data_root=self.tmp),   # holds every lab
                   "run_id": dict(run_id="R1")}
        for index, (first, problem) in enumerate(order):
            for second, _ in order[index + 1:]:
                with self.subTest(first=first, second=second):
                    self.fresh_roots(f"{first}-{second}")
                    both = {first, second}
                    if "inputs" not in both:                    # else no archive
                        self.standard_inputs()
                    if "certificate" in both:
                        (self.lab / certificate.CERTIFICATIONS_DIR / f"{CERTIFICATE['key']}.json").unlink()
                    arguments = {key: item for name in both for key, item in changes.get(name, {}).items()}
                    answers = (mock.patch.object(runner, "known_accounting_answers", side_effect=ArithmeticError)
                               if "known_answers" in both else contextlib.nullcontext())
                    with answers:
                        self.assertEqual(self.run_once(**arguments), {"status": "refused", "problems": [problem]})
                    self.assertEqual(self.lab_entries(), [])

    def test_the_source_recheck_before_the_start_comes_after_the_run_id_and_the_budget(self):
        self.tied_inputs()                                  # incomplete: a pass would stop the contract
        real = certificate.current_key

        def changed_at_the_recheck():                       # the certificate's key, then other sources
            calls = []

            def key(root=None):
                calls.append(1)
                value = real(root)
                return ("0" * 64, *value[1:]) if len(calls) >= 2 else value
            return mock.patch.object(runner, "current_key", key)
        self.assertEqual(self.run_once("r1")["status"], "completed")
        for run_id, problem in (("r2", "source:changed_before_start"), ("r1", "run_id:already_used")):
            with self.subTest(run_id=run_id, starts=2), changed_at_the_recheck():
                self.refused_unchanged(problem, run_id=run_id)
        self.assertEqual(self.run_once("r2")["status"], "completed")             # four starts of four
        for run_id, problem in (("r1", "run_id:already_used"), ("r3", "ledger:trial_budget_exhausted")):
            with self.subTest(run_id=run_id, starts=4), changed_at_the_recheck():
                self.refused_unchanged(problem, run_id=run_id)


class EmptyInputTests(RunnerCase):
    """Astra's design B (2026-10-15): a corpus with no record, or with no record eligible under the contract's own
    population rules, refuses before the lock or any write, and only after the whole first pass; one eligible record
    starts the trials, which are charged whatever their decisions."""


    def test_no_record_or_no_eligible_record_refuses_before_anything_is_written(self):
        later = datetime(2023, 6, 1, 14, 0, tzinfo=timezone.utc)
        heartbeats = [{"op": "mcm", "clk": "h", "pt": fx.ms(OFF), "ct": "HEARTBEAT"}]
        no_view = [fx.md("1.100000005", OFF - timedelta(hours=1), fx.definition(OFF, [fx.runner(1)], marketTime=None))]
        cases = {
            "an index with no record": ([(path("1.100000001"), heartbeats)], "inputs:no_records"),
            "records only outside the evaluated split": (
                [(path("1.100000002", when=later), market("1.100000002", off=later))], "inputs:no_eligible_records"),
            "records only in another tier": ([(path("1.100000003", plan="PRO"), market("1.100000003"))],
                                             "inputs:no_eligible_records"),
            "records only excluded or unclassifiable": (
                [(path("1.100000004"), [fx.md("1.100000004", OFF - timedelta(hours=1),
                                              fx.definition(OFF, [fx.runner(1)], countryCode="FR"))]),
                 (path("1.100000006"), [fx.md("1.100000006", OFF - timedelta(hours=1),
                                              fx.definition(OFF, [fx.runner(1)], marketType=fx.DROP))])],
                "inputs:no_eligible_records"),
            "a record with no view": ([(path("1.100000005"), no_view)], "inputs:no_eligible_records"),
            "records only with an anchor published after t": (
                [(path("1.100000007"), [fx.md("1.100000007", OFF - timedelta(minutes=4),
                                              fx.definition(OFF, [fx.runner(1), fx.runner(2)]))])],
                "inputs:no_eligible_records"),
        }
        for n, (name, (members, problem)) in enumerate(cases.items()):
            with self.subTest(case=name):
                self.fresh_roots(f"case{n}")
                self.ingest(members)
                self.assertEqual(self.run_once(), {"status": "refused", "problems": [problem]})
                self.assertEqual(self.lab_entries(), [])                 # no lock, ledger or run directory
                out = io.StringIO()
                with redirect_stdout(out):
                    code = main(["run-backtest", "--contract", str(self.contract), "--data-root", str(self.data),
                                 "--lab-state-root", str(self.lab), "--run-id", "cli", "--code-identity", CODE])
                self.assertEqual((code, json.loads(out.getvalue())["problems"]), (2, [problem]))
                self.assertEqual(self.lab_entries(), [])

    def test_one_eligible_record_starts_the_trials_whatever_its_decisions_and_outcomes(self):
        later = datetime(2023, 6, 1, 14, 0, tzinfo=timezone.utc)
        opening = fx.standard_market("1.100000001")[:1]                           # no price before t
        heartbeat = {"op": "mcm", "clk": "h", "pt": fx.ms(OFF + timedelta(minutes=10)), "ct": "HEARTBEAT"}
        eligible = {"settled": (opening + fx.standard_market("1.100000001")[4:], (True, True)),
                    "unread after its definition": (opening + [heartbeat, b"bad line\n"], (False, False))}
        for n, (name, (messages, (closed, complete))) in enumerate(eligible.items()):
            with self.subTest(eligible=name):
                self.fresh_roots(f"eligible{n}")
                sha = self.ingest([(path("1.100000002", when=later), market("1.100000002", off=later)),
                                   (path("1.100000001"), messages)])
                outcome = json.loads((self.data / "index" / sha / "markets-v1.jsonl").read_bytes().splitlines()[0])
                self.assertEqual((outcome["market_id"], outcome["evaluator_only_outcome"]["closed_reached"],
                                  outcome["evaluator_only_outcome"]["read_complete"]),
                                 ("1.100000001", closed, complete))
                result = self.run_once()
                self.assertEqual(result["status"], "completed", result)
                results = json.loads((self.lab / "runs" / self.digest / "r1" / "results.json").read_bytes())
                self.assertEqual((results["population"]["records"], results["population"]["by_class"]),
                                 (2, {"eligible": 1, "excluded": 1}))
                self.assertEqual({variant: counts["issued"]
                                  for variant, counts in results["issuance"]["by_variant"].items()},
                                 {"ltp_favourite": 0, "seeded_random_runner": 0})
                self.assertEqual([entry["kind"] for entry in self.ledger()],
                                 ["started", "started", "completed", "completed"])

    def test_a_failure_after_an_eligible_record_names_itself(self):
        from betangel_research.betfair.backtest import archive, market_index
        late = datetime(2025, 12, 31, 23, 0, tzinfo=timezone.utc)
        stale = [fx.md("1.100000009", late - timedelta(hours=2), fx.definition(late, [fx.runner(1), fx.runner(2)])),
                 fx.md("1.100000009", late - timedelta(hours=1),                  # revised into 2026-01 (sealed)
                       fx.definition(late + timedelta(hours=2), [fx.runner(1), fx.runner(2)]))]
        real_gate = archive.month_gate

        def older(month):                                   # an older gate let 2026-01 through
            return None if month == "2026-01" else real_gate(month)

        self.fresh_roots("stale")
        with mock.patch.object(archive, "month_gate", older), mock.patch.object(market_index, "month_gate", older):
            sha = self.ingest([(path("1.100000001"), market("1.100000001")), (path("1.100000009", when=late), stale)])
        order = [json.loads(line)["market_id"]
                 for line in (self.data / "index" / sha / "markets-v1.jsonl").read_bytes().splitlines()]
        self.assertEqual(order, ["1.100000001", "1.100000009"])                 # the eligible record comes first
        self.assertEqual(self.run_once(), {"status": "refused", "problems": [f"input:{sha}:stale_gate:1.100000009"]})
        self.assertEqual(self.lab_entries(), [])
        self.fresh_roots("malformed")
        sha = self.ingest([(path("1.100000001"), market("1.100000001")),
                           (path("1.100000002"), market("1.100000002", off=OFF + timedelta(hours=1)))])
        self.rewrite_records(sha, lambda records: records[1]["checks"].update(path_month="2015-13"))
        self.assertEqual(self.run_once(), {"status": "refused", "problems": [f"input:{sha}:malformed_record"]})
        self.assertEqual(self.lab_entries(), [])

    def test_a_record_whose_member_the_run_cannot_trust_refuses_before_any_start(self):
        def bad_sha(records):
            records[0]["provenance"]["member_sha256"] = "x"

        def other_date(records):
            records[0]["provenance"]["path_date"] = "2015-06-02"

        def other_date_without_view(records):           # never read: no prefix, no replay, no refusal
            other_date(records)
            records[0]["decision_time"]["first_published_market_time"] = None

        for name, edit, problem in (("a malformed member sha256", bad_sha, "malformed_record"),
                                    ("provenance that is not the manifest's", other_date,
                                     "member_not_the_manifests:1.100000001")):
            with self.subTest(case=name):
                self.fresh_roots(name.replace(" ", "-").replace("'", ""))
                sha = self.ingest([(path("1.100000001"), market("1.100000001")),
                                   (path("1.100000002"), market("1.100000002"))])
                self.rewrite_records(sha, edit)
                self.assertEqual(self.run_once(), {"status": "refused", "problems": [f"input:{sha}:{problem}"]})
                self.assertEqual(self.lab_entries(), [])
        self.fresh_roots("no-view")
        sha = self.ingest([(path("1.100000001"), market("1.100000001")), (path("1.100000002"), market("1.100000002"))])
        self.rewrite_records(sha, other_date_without_view)
        self.assertEqual(self.run_once()["status"], "completed")

    def test_a_record_the_population_rules_cannot_read_refuses_as_malformed(self):
        def unreadable_anchor(records):                     # its shape passes the first check; classify fails
            records[0]["decision_time"]["first_published_market_time"]["pt"] = "x"

        for name, members in (("alone", [(path("1.100000001"), market("1.100000001"))]),
                              ("beside an eligible record", [(path("1.100000001"), market("1.100000001")),
                                                             (path("1.100000002"), market("1.100000002"))])):
            with self.subTest(case=name):
                self.fresh_roots(name.replace(" ", "-"))
                sha = self.ingest(members)
                self.rewrite_records(sha, unreadable_anchor)
                self.assertEqual(self.run_once(), {"status": "refused", "problems": [f"input:{sha}:malformed_record"]})
                self.assertEqual(self.lab_entries(), [])

    def test_a_failure_anywhere_in_the_pass_names_itself_not_an_empty_population(self):
        later = datetime(2023, 6, 1, 14, 0, tzinfo=timezone.utc)
        self.ingest([(path("1.100000002", when=later), market("1.100000002", off=later))])
        self.ingest([(path("1.100000002", when=later, event="27000002"), market("1.100000002", off=later))])
        self.assertRefused(self.run_once(), "cohort:1.100000002:duplicate_market")    # in the second archive
        self.data = self.tmp / "data-changed"
        self.data.mkdir()
        sha = self.ingest([(path("1.100000002", when=later), market("1.100000002", off=later))])
        real = runner._records

        def then_change(archive):                           # the markets file changes while the pass reads it
            yield from real(dict(archive, markets_sha256="0" * 64))

        with mock.patch.object(runner, "_records", then_change):
            self.assertRefused(self.run_once(), f"input:{sha}:markets_changed")
        self.assertEqual(self.lab_entries(), [])

    def test_a_refusal_leaves_the_history_as_it_was(self):
        self.standard_inputs()
        self.assertEqual(self.run_once()["status"], "completed")
        before = {str(item): item.read_bytes() for item in self.lab.rglob("*") if item.is_file()}
        later = datetime(2023, 6, 1, 14, 0, tzinfo=timezone.utc)
        self.data = self.tmp / "data-other"
        self.data.mkdir()
        self.ingest([(path("1.100000002", when=later), market("1.100000002", off=later))])
        self.assertRefused(self.run_once(run_id="r2"), "inputs:no_eligible_records")
        self.assertEqual({str(item): item.read_bytes() for item in self.lab.rglob("*") if item.is_file()}, before)


class ReviewRegressionTests(RunnerCase):
    """The pre-submission review's reproductions (B1 escapes, S1 refunds, S2 forged receipts, S3 reconcile status, S4
    malformed heads), each now a refusal, a failure or an uncertain result, never an escaping exception."""

    def setUp(self):
        super().setUp()
        self.contract_value = fx.frozen_contract(max_trials=12)
        self.contract = self.write_contract(self.contract_value, "twelve.json")
        self.digest = contract_sha256(self.contract_value)
        self.tied_inputs()
        self.contract_dir = self.lab / "contracts" / self.digest
        self.runs = self.lab / "runs" / self.digest

    def test_a_refused_first_start_removes_the_run_directory_and_releases_the_lock(self):
        self.contract_dir.mkdir(parents=True)
        (self.contract_dir / (LEDGER_FILE + ".lock")).write_bytes(b"")
        self.assertRefused(self.run_once(), "ledger:ledger:locked")
        self.assertFalse((self.runs / "r1").exists())
        self.assertFalse((self.contract_dir / runner.LOCK_NAME).exists())

    def test_a_refused_second_start_is_uncertain_and_keeps_the_lock(self):
        real = TrialLedger.append

        def second_refused(ledger, kind, **kwargs):
            if kind == "started" and kwargs["trial_id"].endswith("seeded_random_runner"):
                raise trial_ledger.LedgerError("ledger:head_moved")
            return real(ledger, kind, **kwargs)

        with mock.patch.object(TrialLedger, "append", second_refused):
            result = self.run_once()
        self.assertEqual((result["status"], result["started"]), ("uncertain", ["ltp_favourite"]), result)
        self.assertEqual(result["trial_ids"], ["r1.ltp_favourite", "r1.seeded_random_runner"])
        self.assertTrue((self.contract_dir / runner.LOCK_NAME).exists())
        self.assertEqual([e["kind"] for e in self.ledger()], ["started"])

    def test_an_unexpected_error_after_the_starts_fails_the_run(self):
        with mock.patch.object(runner, "decide_market", side_effect=AttributeError("synthetic")):
            result = self.run_once()
        self.assertEqual(result["status"], "failed", result)
        self.assertEqual([e["kind"] for e in self.ledger()], ["started", "started", "failed", "failed"])
        self.assertFalse((self.contract_dir / runner.LOCK_NAME).exists())

    def test_a_receipt_without_a_canonical_form_refuses(self):
        self.assertEqual(self.run_once("r1")["status"], "completed")
        (self.runs / "r1" / "receipt.json").write_bytes(b'{"kind":"\\udc80"}\n')   # a lone surrogate
        self.assertRefused(self.run_once("r2"), "receipt:r1:malformed")
        self.assertFalse((self.contract_dir / runner.LOCK_NAME).exists())

    def test_reconcile_refused_before_any_append_releases_the_lock(self):
        self.contract_dir.mkdir(parents=True)
        ledger = TrialLedger.open(self.contract_dir)
        ledger.append("started", trial_id="x.ltp_favourite", contract=self.contract_value, variant="ltp_favourite",
                      input_sha256="ab" * 32, code_identity="git:old", recorded_at="2026-10-15T00:00:00Z", summary={})
        (self.contract_dir / (LEDGER_FILE + ".lock")).write_bytes(b"")
        result = runner.reconcile(self.contract, self.lab, "reconciler", clock=clock)
        self.assertEqual(result["status"], "refused", result)
        self.assertFalse((self.contract_dir / runner.LOCK_NAME).exists())

    def test_a_malformed_record_or_head_refuses_before_any_start(self):
        sha = next(iter(sorted(p.name for p in (self.data / "index").iterdir())))
        head_path = self.data / "index" / sha / "head-v1.json"
        head = json.loads(head_path.read_bytes())
        head_path.chmod(0o600)
        kept = head_path.read_bytes()
        head_path.write_bytes(runner.canonical_json(dict(head, refused_markets=[{"oops": 1}])) + b"\n")
        self.assertRefused(self.run_once(), f"input:{sha}:head_mismatch")
        head_path.write_bytes(kept)
        markets = self.data / "index" / sha / "markets-v1.jsonl"
        markets.chmod(0o600)
        markets.write_bytes(b"[]\n")
        head_path.write_bytes(runner.canonical_json(dict(head, markets=1,
                                                         markets_sha256=runner._sha256_bytes(b"[]\n"))) + b"\n")
        self.assertRefused(self.run_once(), f"input:{sha}:malformed_record")
        self.assertFalse((self.contract_dir / LEDGER_FILE).exists())

    def test_a_crashed_run_cut_out_of_the_ledger_is_unaccounted_until_recorded(self):
        self.assertEqual(self.run_once("r1")["status"], "completed")
        ledger = self.contract_dir / LEDGER_FILE
        after_r1 = ledger.read_bytes()
        with mock.patch.object(runner, "_evaluate", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.run_once("r2")                                   # a crash after both starts
        (self.contract_dir / runner.LOCK_NAME).unlink()              # a person, after checking no runner runs
        ledger.write_bytes(after_r1)                                 # cut back to r1's receipt head: a valid chain
        self.assertRefused(self.run_once("r3"), "run:unaccounted:r2")
        result = runner.reconcile(self.contract, self.lab, "reconciler", clock=clock)
        self.assertEqual((result["status"], result["recorded_runs"], result["receipted"]),
                         ("reconciled", ["r2"], ["r2"]), result)
        recorded = [e for e in self.ledger() if e["trial_id"].startswith("r2.")]
        self.assertEqual([(e["kind"], e["input_sha256"]) for e in recorded],
                         [("started", runner.UNKNOWN_INPUT_SHA256), ("aborted", runner.UNKNOWN_INPUT_SHA256)] * 2)
        self.assertEqual(self.run_once("r3")["cumulative_starts"], 6)    # the refunded starts are spent again

    def test_deleting_the_ledger_and_receipts_does_not_refund_the_budget(self):
        self.assertEqual(self.run_once("r1")["status"], "completed")
        (self.contract_dir / LEDGER_FILE).unlink()
        (self.runs / "r1" / "receipt.json").unlink()
        self.assertRefused(self.run_once("r2"), "run:unaccounted:r1")

    def test_forged_receipts_refuse(self):
        self.assertEqual(self.run_once("r1")["status"], "completed")
        path_ = self.runs / "r1" / "receipt.json"
        kept = path_.read_bytes()
        receipt = json.loads(kept)
        twice = [receipt["trials"][0], dict(receipt["trials"][0])]
        early = dict(receipt, ledger_head=receipt["trials"][0]["started"])
        cases = {"receipt:r1:status_not_the_ledgers": dict(receipt, status="aborted", results_sha256=None,
                                                           issuance_ledger_sha256=None, issuance_ledger_bytes=None,
                                                           engineering_acceptance=None, failure="reconciled"),
                 "receipt:r1:trials_not_the_ledgers": dict(receipt, trials=twice),
                 "receipt:r1:head_before_its_closes": early}
        for problem, value in cases.items():
            with self.subTest(problem=problem):
                path_.write_bytes(runner.canonical_json(value) + b"\n")
                self.assertRefused(self.run_once("r2"), problem)
        path_.write_bytes(kept)
        issuance = self.runs / "r1" / "issuance-ledger.jsonl"
        issuance.write_bytes(issuance.read_bytes() + b"\n")
        self.assertRefused(self.run_once("r2"), "receipt:r1:issuance_ledger_sha256_mismatch")

    def test_reconcile_after_appending_never_reports_refused(self):
        self.contract_dir.mkdir(parents=True)
        ledger = TrialLedger.open(self.contract_dir)
        ledger.append("started", trial_id="gone.ltp_favourite", contract=self.contract_value, variant="ltp_favourite",
                      input_sha256="ab" * 32, code_identity="git:old", recorded_at="2026-10-15T00:00:00Z", summary={})
        result = runner.reconcile(self.contract, self.lab, "reconciler", clock=clock)  # no run dir to receipt in
        self.assertEqual((result["status"], result["trial_ids"]), ("uncertain", ["gone.ltp_favourite"]), result)
        self.assertTrue((self.contract_dir / runner.LOCK_NAME).exists())
        self.assertEqual(self.ledger()[-1]["kind"], "aborted")

    def test_a_markets_file_changed_after_preflight_fails_the_run(self):
        real = runner._first_pass

        def then_change(archives, rules):
            count = real(archives, rules)
            for archive in archives:                                # still valid JSON, other bytes
                archive["markets_path"].chmod(0o600)
                data = archive["markets_path"].read_bytes()
                archive["markets_path"].write_bytes(data.replace(b'{"', b'{ "', 1))
            return count

        with mock.patch.object(runner, "_first_pass", then_change):
            result = self.run_once()
        self.assertEqual(result["status"], "failed", result)
        self.assertIn("markets_changed", result["problems"][0])
        self.assertEqual([e["kind"] for e in self.ledger()], ["started", "started", "failed", "failed"])

    def test_stray_entries_in_the_runs_directory(self):
        self.assertEqual(self.run_once("r1")["status"], "completed")
        (self.runs / ".DS_Store").write_bytes(b"")                    # not a run id: ignored
        self.assertEqual(self.run_once("r2")["status"], "completed")
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        (self.runs / "r9").symlink_to(elsewhere)
        self.assertRefused(self.run_once("r3"), "run:r9:not_a_run_directory")


class SecondReviewTests(RunnerCase):
    """Astra's c2b2 review (R1-R4, N1, N2), each reproduction a regression."""

    def setUp(self):
        super().setUp()
        self.contract_value = fx.frozen_contract(max_trials=4)
        self.contract = self.write_contract(self.contract_value, "four.json")
        self.digest = contract_sha256(self.contract_value)
        self.contract_dir = self.lab / "contracts" / self.digest
        self.runs = self.lab / "runs" / self.digest

    def lose_ledger_and_receipts(self):
        (self.contract_dir / LEDGER_FILE).unlink()
        for receipt in self.runs.glob("*/receipt.json"):
            receipt.unlink()

    def interrupting(self, kind, variant):
        real = TrialLedger.append

        def append(ledger, entry_kind, **kwargs):
            if entry_kind == kind and kwargs["variant"] == variant:
                raise trial_ledger.LedgerError("ledger:head_moved")
            return real(ledger, entry_kind, **kwargs)
        return mock.patch.object(TrialLedger, "append", append)

    def remove_lock(self):
        (self.contract_dir / runner.LOCK_NAME).unlink()          # a person, after checking nothing runs

    # R1
    def test_a_refused_second_append_after_a_persisted_start_is_uncertain(self):
        self.standard_inputs()
        self.assertEqual(self.run_once("r1")["status"], "completed")
        self.lose_ledger_and_receipts()
        with self.interrupting("aborted", "ltp_favourite"):
            result = runner.reconcile(self.contract, self.lab, "reconciler", clock=clock)
        self.assertEqual(result["status"], "uncertain", result)
        self.assertEqual(result["trial_ids"], ["r1.ltp_favourite:started"])
        self.assertTrue((self.contract_dir / runner.LOCK_NAME).exists())
        self.assertEqual([(e["kind"], e["trial_id"]) for e in self.ledger()], [("started", "r1.ltp_favourite")])

    def test_a_receipt_write_that_fails_after_creation_is_uncertain(self):
        self.standard_inputs()
        self.assertEqual(self.run_once("r1")["status"], "completed")
        for keep in ("half", "all"):
            with self.subTest(keep=keep):
                receipt = self.runs / "r1" / "receipt.json"
                receipt.unlink(missing_ok=True)
                real_open = Path.open

                def opening(path, mode="r", *args, **kwargs):
                    handle = real_open(path, mode, *args, **kwargs)
                    if mode != "xb" or path.name != "receipt.json":
                        return handle

                    class Failing:
                        def __enter__(self):
                            return self

                        def __exit__(self, *exc):
                            handle.close()
                            return False

                        def write(self, data):
                            handle.write(data[:len(data) // 2] if keep == "half" else data)
                            raise OSError(28, "synthetic: no space left on device")
                    return Failing()

                with mock.patch.object(Path, "open", opening):
                    result = runner.reconcile(self.contract, self.lab, "reconciler", clock=clock)
                self.assertEqual((result["status"], result["runs_touched"]), ("uncertain", ["r1"]), result)
                self.assertTrue(receipt.exists())                         # kept as evidence, never deleted
                self.assertTrue((self.contract_dir / runner.LOCK_NAME).exists())
                self.remove_lock()

    def test_a_receipt_that_cannot_be_created_is_a_plain_refusal(self):
        self.standard_inputs()
        self.assertEqual(self.run_once("r1")["status"], "completed")
        (self.runs / "r1" / "receipt.json").unlink()
        real = runner._write_new

        def refusing(path, chunks, where):
            if where.startswith("receipt:"):
                raise runner.RunRefused(f"{where}:not_created")
            return real(path, chunks, where)

        with mock.patch.object(runner, "_write_new", refusing):
            result = runner.reconcile(self.contract, self.lab, "reconciler", clock=clock)
        self.assertEqual(result["status"], "refused", result)
        self.assertFalse((self.contract_dir / runner.LOCK_NAME).exists())

    # R2
    def test_interrupted_recoveries_never_refund_the_budget(self):
        self.tied_inputs(markets=1)
        self.assertEqual(self.run_once("old_a")["status"], "completed")
        self.assertEqual(self.run_once("old_b")["status"], "completed")      # 4 starts: the budget is spent
        self.lose_ledger_and_receipts()
        for attempt in range(2):                                              # interrupted at the seeded start
            with self.interrupting("started", "seeded_random_runner"):
                result = runner.reconcile(self.contract, self.lab, "reconciler", clock=clock)
            if attempt == 0:                     # intents written and old_a's first pair appended: uncertain
                self.assertEqual(result["status"], "uncertain", result)
                self.assertRefused(self.run_once("new_run"), "runner_lock:held")
                self.remove_lock()
            else:                                # nothing written before the interruption: a plain refusal
                self.assertEqual(result["status"], "refused", result)
            self.assertRefused(self.run_once("new_run"), "run:recovery_incomplete")
        result = runner.reconcile(self.contract, self.lab, "reconciler", clock=clock)
        self.assertEqual((result["status"], result["receipted"]), ("reconciled", ["old_a", "old_b"]), result)
        starts = [e for e in self.ledger() if e["kind"] == "started"]
        self.assertEqual(len(starts), 4)
        for run_id in ("old_a", "old_b"):
            receipt = json.loads((self.runs / run_id / "receipt.json").read_bytes())
            self.assertEqual(sorted(t["variant"] for t in receipt["trials"]),
                             ["ltp_favourite", "seeded_random_runner"])
        self.assertRefused(self.run_once("new_run"), "ledger:trial_budget_exhausted")

    def test_budget_exhaustion_mid_recovery_stays_unresolved(self):
        self.contract_value = fx.frozen_contract(max_trials=2)
        self.contract = self.write_contract(self.contract_value, "two.json")
        self.digest = contract_sha256(self.contract_value)
        self.contract_dir = self.lab / "contracts" / self.digest
        self.runs = self.lab / "runs" / self.digest
        self.ingest([(path("1.100000001"), market("1.100000001"))])
        self.assertEqual(self.run_once("r1")["status"], "completed")
        self.lose_ledger_and_receipts()
        (self.runs / "r0").mkdir()                                          # another crashed, unaccounted run
        result = runner.reconcile(self.contract, self.lab, "reconciler", clock=clock)
        self.assertEqual(result["status"], "uncertain", result)
        self.assertIn("max_trials_reached", result["problems"][0])
        self.remove_lock()
        self.assertRefused(self.run_once("r2"), "run:recovery_incomplete:r1")
        again = runner.reconcile(self.contract, self.lab, "reconciler", clock=clock)
        self.assertEqual(again["status"], "refused", again)                 # nothing more can be written
        self.assertIn("max_trials_reached", again["problems"][0])
        self.assertRefused(self.run_once("r2"), "run:recovery_incomplete:r1")   # explicit, never shrunk

    def test_a_run_that_crashed_between_its_starts_keeps_its_one_trial(self):
        self.ingest([(path("1.100000001"), market("1.100000001"))])
        with self.interrupting("started", "seeded_random_runner"):
            result = self.run_once("r1")
        self.assertEqual(result["status"], "uncertain", result)
        self.remove_lock()
        reconciled = runner.reconcile(self.contract, self.lab, "reconciler", clock=clock)
        self.assertEqual((reconciled["aborted"], reconciled["receipted"]), (["r1.ltp_favourite"], ["r1"]))
        receipt = json.loads((self.runs / "r1" / "receipt.json").read_bytes())
        self.assertEqual([t["variant"] for t in receipt["trials"]], ["ltp_favourite"])
        self.assertEqual(self.run_once("r2")["cumulative_starts"], 3)

    def test_a_receipt_for_an_incomplete_recovery_refuses_and_is_never_counted_missing(self):
        self.standard_inputs()
        self.assertEqual(self.run_once("r1")["status"], "completed")
        self.lose_ledger_and_receipts()
        with self.interrupting("started", "seeded_random_runner"):
            self.assertEqual(runner.reconcile(self.contract, self.lab, "reconciler", clock=clock)["status"],
                             "uncertain")
        self.remove_lock()
        history = runner._History(self.contract_dir, self.runs, self.digest, ("ltp_favourite", "seeded_random_runner"))
        self.assertEqual((history.incomplete_recovery, history.missing), (["r1"], []))
        start, close = [e for e in self.ledger() if e["trial_id"] == "r1.ltp_favourite"]
        forged = runner._receipt(self.digest, "r1", "aborted", "reconciled", None,
                                 [("ltp_favourite", "r1.ltp_favourite", (start["seq"], start["entry_sha256"]),
                                   (close["seq"], close["entry_sha256"]), None)],
                                 (close["seq"], close["entry_sha256"]), 1, "2026-10-15T00:00:00Z", "forger")
        (self.runs / "r1" / "receipt.json").write_bytes(runner.canonical_json(forged) + b"\n")
        self.assertRefused(self.run_once("r2"), "receipt:r1:trials_not_the_ledgers")

    def test_a_record_with_a_malformed_month_refuses(self):
        sha = self.ingest([(path("1.100000001"), market("1.100000001"))])
        markets = self.data / "index" / sha / "markets-v1.jsonl"
        head_path = self.data / "index" / sha / "head-v1.json"
        record = json.loads(markets.read_bytes())
        record["checks"]["path_month"] = "2015-13"
        line = runner.canonical_json(record) + b"\n"
        markets.chmod(0o600)
        head_path.chmod(0o600)
        markets.write_bytes(line)
        head = json.loads(head_path.read_bytes())
        head_path.write_bytes(runner.canonical_json(dict(head, markets_sha256=runner._sha256_bytes(line))) + b"\n")
        self.assertRefused(self.run_once(), f"input:{sha}:malformed_record")

    # R3
    @unittest.skipIf(IS_ROOT, "root ignores file modes")
    def test_an_uninspectable_receipt_refuses_without_writing(self):
        self.ingest([(path("1.100000001"), market("1.100000001"))])
        self.assertEqual(self.run_once("r1")["status"], "completed")
        (self.contract_dir / LEDGER_FILE).unlink()
        run = self.runs / "r1"
        run.chmod(0o600)
        self.addCleanup(run.chmod, 0o700)
        try:
            (run / "receipt.json").lstat()
        except PermissionError:
            pass
        else:
            self.skipTest("this user can search a directory without permission")
        self.assertRefused(self.run_once("r2"), "receipt:r1:uninspectable")
        result = runner.reconcile(self.contract, self.lab, "reconciler", clock=clock)
        self.assertEqual(result["status"], "refused", result)
        self.assertFalse((self.contract_dir / LEDGER_FILE).exists())        # no replacement ledger entries
        self.assertFalse((self.contract_dir / runner.LOCK_NAME).exists())

    def test_every_inspection_error_but_absence_refuses(self):
        self.ingest([(path("1.100000001"), market("1.100000001"))])
        self.assertEqual(self.run_once("r1")["status"], "completed")
        real = Path.lstat
        for error in (OSError(5, "synthetic EIO"), NotADirectoryError(20, "synthetic")):
            def lstat(path, *args, **kwargs):
                if path.name == "receipt.json":
                    raise error
                return real(path, *args, **kwargs)
            with self.subTest(error=type(error).__name__), mock.patch.object(Path, "lstat", lstat):
                self.assertRefused(self.run_once("r2"), "receipt:r1:uninspectable")

    # R4
    def test_records_are_checked_against_the_current_month_gate(self):
        from betangel_research.betfair.backtest import archive, market_index
        late = datetime(2025, 12, 31, 23, 0, tzinfo=timezone.utc)
        revised = late + timedelta(hours=2)
        messages = [fx.md("1.100000001", late - timedelta(hours=2),
                          fx.definition(late, [fx.runner(1), fx.runner(2)])),
                    fx.md("1.100000001", late - timedelta(hours=1),
                          fx.definition(revised, [fx.runner(1), fx.runner(2)]))]
        real_gate = archive.month_gate

        def older(month):                                   # a gate from before 2026-01 was refused
            return None if month == "2026-01" else real_gate(month)

        with mock.patch.object(archive, "month_gate", older), mock.patch.object(market_index, "month_gate", older):
            self.ingest([(path("1.100000001", when=late), messages)])
        sha = next(iter(p.name for p in (self.data / "index").iterdir()))
        self.assertRefused(self.run_once(), f"input:{sha}:stale_gate:1.100000001")
        self.assertFalse((self.contract_dir / LEDGER_FILE).exists())

    def test_an_older_gate_snapshot_with_allowed_months_is_only_reported(self):
        from betangel_research.betfair.backtest import archive
        older = dict(archive.gate_snapshot(), refused_from_month="2027-01")
        with mock.patch.object(archive, "gate_snapshot", lambda: older):
            self.ingest([(path("1.100000001"), market("1.100000001"))])
        result = self.run_once()
        self.assertEqual(result["status"], "completed", result)
        results = json.loads((self.runs / "r1" / "results.json").read_bytes())
        self.assertEqual([a["manifest_gate_current"] for a in results["input"]["archives"]], [False])

    # N1, N2
    def test_refusals_are_reported_at_every_level(self):
        held = datetime(2026, 1, 10, 14, 0, tzinfo=timezone.utc)
        self.ingest([(path("1.100000001"), market("1.100000001")),
                     (path("1.100000002", when=held), market("1.100000002", off=held))])
        self.assertEqual(self.run_once()["status"], "completed")
        refusals = json.loads((self.runs / "r1" / "results.json").read_bytes())["population"]["source_refusals"]
        self.assertEqual(refusals["members_refused_at_ingest_by_reason"], {"month_sealed_holdout": 1})
        self.assertEqual(refusals["market_rows_by_reason"], {})

    def test_receipt_metadata_is_checked(self):
        self.tied_inputs(markets=1)
        self.assertEqual(self.run_once("r1")["status"], "completed")
        receipt_path = self.runs / "r1" / "receipt.json"
        kept = receipt_path.read_bytes()
        receipt = json.loads(kept)
        trials = [dict(receipt["trials"][0], issued=-99), receipt["trials"][1]]
        for value, problem in ((dict(receipt, cumulative_starts=-99), "receipt:r1:malformed"),
                               (dict(receipt, cumulative_starts=3), "receipt:r1:cumulative_starts_not_the_ledgers"),
                               (dict(receipt, recorded_at={"x": 1}), "receipt:r1:malformed"),
                               (dict(receipt, trials=trials), "receipt:r1:malformed")):
            with self.subTest(problem=problem):
                receipt_path.write_bytes(runner.canonical_json(value) + b"\n")
                self.assertRefused(self.run_once("r2"), problem)
        receipt_path.write_bytes(kept)
        self.assertEqual(self.run_once("r2")["status"], "completed")


class EngineeringWiringTests(RunnerCase):
    """Task c2b3b1: the three criteria inside the run, under Astra's design decision (journal rows 599 and 600)."""

    def results(self, run_id="r1"):
        return json.loads(self.run_file("results.json", run_id).read_bytes())

    def test_a_completed_run_reports_and_binds_its_engineering_acceptance(self):
        self.standard_inputs()
        result = self.run_once()
        self.assertEqual((result["status"], result["engineering_acceptance"]), ("completed", "passed"))
        results = self.results()
        acceptance, issuance = results["engineering_acceptance"], self.run_file("issuance-ledger.jsonl").read_bytes()
        stored = (self.lab / "certifications" / f"{CERTIFICATE['key']}.json").read_bytes()
        sha = runner._sha256_bytes(stored)
        self.assertEqual(sha, self.certificate_sha256)
        self.assertEqual((results["schema_version"], results["input"]["version"], results["certification"]),
                         (2, 1, {"key": CERTIFICATE["key"], "sha256": sha, "certificate": CERTIFICATE}))
        self.assertEqual((acceptance["status"], acceptance["rule"], acceptance["criteria_version"]),
                         ("passed", self.contract_value["engineering_acceptance"]["rule"], 1))
        criteria = acceptance["criteria"]
        self.assertEqual({name: item["status"] for name, item in criteria.items()},
                         {"issuance_exposure_reconciliation": "passed", "known_accounting_answers": "passed",
                          "prefix_invariance": "passed"})
        self.assertEqual(criteria["known_accounting_answers"],
                         {"status": "passed", "cases": len(engineering.KAT_CASES), "failed_cases": [],
                          "case_ids": [case["id"] for case in engineering.KAT_CASES]})
        components = criteria["prefix_invariance"]["components"]
        self.assertEqual(components["synthetic_certification"], {"status": "passed", "certificate_sha256": sha})
        tally = components["primary_source_prefix_consistency"]
        self.assertEqual((tally["status"], tally["counts"]["records"], tally["counts"]["compared"],
                          tally["counts"]["missing"], tally["prefix_limit_bytes"]),
                         ("passed", 2, 2, 0, engineering.PREFIX_LIMIT_BYTES))
        ledger_sha = runner._sha256_bytes(issuance)
        self.assertEqual((results["issuance"]["ledger_sha256"], results["issuance"]["ledger_bytes"],
                          results["issuance"]["writer_sha256"]), (ledger_sha, len(issuance), ledger_sha))
        binding = {"status": "passed", "rule": acceptance["rule"], "criteria_version": 1,
                   "acceptance_sha256": hashlib.sha256(runner.ACCEPTANCE_TAG
                                                       + runner.canonical_json(acceptance)).hexdigest(),
                   "certificate_sha256": sha}
        entries = self.ledger()
        self.assertEqual([e["summary"] for e in entries if e["kind"] == "started"], [{"certificate_sha256": sha}] * 2)
        for close in [entry for entry in entries if entry["kind"] == "completed"]:
            self.assertEqual(close["summary"], {"results_sha256": result["results_sha256"],
                                                "issuance_ledger_sha256": ledger_sha,
                                                "issuance_ledger_bytes": len(issuance),
                                                "engineering_acceptance": binding, "certificate_sha256": sha})
        receipt = json.loads(self.run_file("receipt.json").read_bytes())
        self.assertEqual((receipt["schema_version"], receipt["engineering_acceptance"], receipt["certificate_sha256"],
                          receipt["issuance_ledger_sha256"], receipt["issuance_ledger_bytes"]),
                         (2, binding, sha, ledger_sha, len(issuance)))
        before = self.lab_files()
        self.assertEqual(self.run_once("r2"), {"status": "refused",               # the history check accepts it,
                                               "problems": ["contract:engineering_passed:r1"]})   # and it stops
        self.assertEqual(self.lab_files(), before)

    def test_known_answers_must_pass_over_the_known_case_set_before_any_start(self):
        self.standard_inputs()
        failed = {"status": "failed", "cases": len(engineering.KAT_CASES), "failed_cases": ["x"]}
        short = {"status": "passed", "cases": len(engineering.KAT_CASES) - 1, "failed_cases": []}
        def answers(**kwargs):
            return lambda: mock.patch.object(runner, "known_accounting_answers", **kwargs)

        def case_set(changed):                  # changed in engineering as a source edit would change it
            @contextlib.contextmanager
            def patched():
                with mock.patch.object(engineering, "KAT_CASES", changed), \
                        mock.patch.object(runner, "KAT_CASES", changed):
                    yield
            return patched

        cases = {"a failed case": (answers(return_value=failed), "engineering:known_accounting_answers_not_passed"),
                 "another case count": (answers(return_value=short),
                                        "engineering:known_accounting_answers_not_passed"),
                 "an error": (answers(side_effect=ArithmeticError), "engineering:known_accounting_answers_error")}
        for name, changed in (("no case", ()), ("a repeated case", engineering.KAT_CASES + engineering.KAT_CASES[:1]),
                              ("a case left out", engineering.KAT_CASES[1:]),
                              ("reordered", engineering.KAT_CASES[::-1])):
            with mock.patch.object(engineering, "KAT_CASES", changed):        # only the registry refuses these
                self.assertEqual(engineering.known_accounting_answers()["status"], "passed")
            cases[name] = (case_set(changed), "engineering:known_accounting_answers_not_the_known_set")
        for name, (patch, problem) in cases.items():
            with self.subTest(case=name), patch():
                self.assertEqual(self.run_once(), {"status": "refused", "problems": [problem]})
                self.assertEqual(self.lab_entries(), [])

    def test_the_checked_known_answers_are_the_ones_reported(self):
        self.standard_inputs()
        real = engineering.known_accounting_answers()
        later = dict(real, status="failed", failed_cases=["x"])
        with mock.patch.object(runner, "known_accounting_answers", side_effect=[real, later]) as spy:
            self.assertEqual(self.run_once()["status"], "completed")
        self.assertEqual(spy.call_count, 1)
        self.assertEqual(self.results()["engineering_acceptance"]["criteria"]["known_accounting_answers"],
                         dict(real, case_ids=list(runner.KAT_CASE_IDS)))

    def test_the_manifest_limits_must_be_the_runs(self):
        sha = self.ingest([(path("1.100000001"), market("1.100000001"))])
        stricter = DEFAULT_LIMITS.stricter(max_line_bytes=1 << 20)
        self.assertEqual(self.run_once(limits=stricter),
                         {"status": "refused", "problems": [f"input:{sha}:limits_not_the_runs"]})
        self.assertEqual(self.lab_entries(), [])

    def test_a_future_leak_fails_the_prefix_criterion_and_the_run_completes(self):
        self.standard_inputs()
        real_fold = replay._fold
        with mock.patch.object(replay, "_fold", lambda raw, mid, t, limits: real_fold(raw, mid, t + 180000, limits)), \
                mock.patch.object(replay, "_upto", lambda timeline, t, real=replay._upto: real(timeline, t + 180000)):
            result = self.run_once()
        self.assertEqual((result["status"], result["engineering_acceptance"]), ("completed", "failed"))
        criteria = self.results()["engineering_acceptance"]["criteria"]
        tally = criteria["prefix_invariance"]["components"]["primary_source_prefix_consistency"]
        self.assertEqual((criteria["prefix_invariance"]["status"], tally["status"], tally["counts"]["failed"]),
                         ("failed", "failed", 2))
        self.assertEqual([entry["kind"] for entry in self.ledger()], ["started", "started", "completed", "completed"])

    def test_a_record_left_out_fails_the_run(self):
        excluded = [fx.md("1.100000003", OFF - timedelta(hours=1),
                          fx.definition(OFF, [fx.runner(1)], countryCode="FR"))]
        cases = {
            "an excluded record's member changed": (None, "member_hash_mismatch"),
            "an excluded record's member path denied": (
                lambda real: mock.patch.object(runner, "_guard", lambda target, where, prospective=False: (
                    (_ for _ in ()).throw(runner.RunRefused(f"{where}:denied")) if where == "raw:1.100000003"
                    else real(target, where, prospective))), "raw:1.100000003:denied"),
            "a member that fails its integrity checks": (
                lambda real: mock.patch.object(runner, "check_prefix",
                                               side_effect=replay.ReplayProblem("prefix:x:member_hash_mismatch")),
                "member_hash_mismatch"),
            "a tally refusal": (lambda real: mock.patch.object(engineering.PrefixTally, "add",
                                                                side_effect=ValueError("prefix_tally:x:duplicate")),
                                "prefix_tally:x:duplicate")}
        for n, (name, (patch, problem)) in enumerate(cases.items()):
            with self.subTest(case=name):
                self.fresh_roots(f"left{n}")
                sha = self.ingest([(path("1.100000001"), market("1.100000001")), (path("1.100000003"), excluded)])
                if patch is None:
                    raw = safe_target(self.data, sha, path("1.100000003"))
                    raw.chmod(0o600)
                    raw.write_bytes(raw.read_bytes() + b"x")
                    result = self.run_once()
                else:
                    with patch(runner._guard):
                        result = self.run_once()
                self.assertEqual(result["status"], "failed", result)
                self.assertIn(problem, result["problems"][0])
                self.assertEqual([entry["kind"] for entry in self.ledger()], ["started", "started", "failed", "failed"])

    def test_the_issuance_ledger_is_read_back_in_full(self):
        real_lines, real_write = runner.ledger_lines, runner._write_new

        def longer(target, chunks, where):
            return real_write(target, list(chunks) + ([b"extra\n"] if where == "issuance_ledger" else []), where)

        def unreadable(target, chunks, where):
            digest = real_write(target, chunks, where)
            if where == "issuance_ledger":
                target.chmod(0)
            return digest

        self.fresh_roots("other-bytes")
        self.standard_inputs()
        with mock.patch.object(runner, "ledger_lines", lambda decisions: real_lines(decisions)[:-1]):
            result = self.run_once()                     # a complete read of other bytes fails the criterion
        self.assertEqual((result["status"], result["engineering_acceptance"]), ("completed", "failed"))
        results = self.results()
        reconciliation = results["engineering_acceptance"]["criteria"]["issuance_exposure_reconciliation"]
        self.assertIn("ledger:not_the_decisions", reconciliation["problems"])
        self.assertEqual(results["issuance"]["ledger_sha256"],
                         runner._sha256_bytes(self.run_file("issuance-ledger.jsonl").read_bytes()))
        operational = [("longer than written", longer, "issuance_ledger:read_back_longer_than_written")]
        if not IS_ROOT:
            operational.append(("unreadable", unreadable, "issuance_ledger:read_back_failed"))
        for name, writer, problem in operational:
            with self.subTest(case=name):
                self.fresh_roots(name.replace(" ", "-"))
                self.standard_inputs()
                with mock.patch.object(runner, "_write_new", writer):
                    result = self.run_once()
                self.assertEqual(result["status"], "failed", result)
                self.assertIn(problem, result["problems"][0])

    def test_a_completed_runs_evidence_must_agree_with_its_binding(self):
        self.tied_inputs()
        self.assertEqual(self.run_once()["status"], "completed")
        receipt_path = self.run_file("receipt.json")
        kept = receipt_path.read_bytes()
        receipt = json.loads(kept)
        other = dict(receipt["engineering_acceptance"], acceptance_sha256="0" * 64)
        cases = {"receipt:r1:engineering_acceptance_mismatch": dict(receipt, engineering_acceptance=other),
                 "receipt:r1:issuance_ledger_bytes_mismatch": dict(receipt, issuance_ledger_bytes=1),
                 "receipt:r1:schema_not_supported": dict(receipt, schema_version=1),
                 "receipt:r1:malformed": dict(receipt, certificate_sha256="0" * 64)}
        for problem, value in cases.items():
            with self.subTest(problem=problem):
                receipt_path.write_bytes(runner.canonical_json(value) + b"\n")
                self.assertRefused(self.run_once("r2"), problem)
        receipt_path.write_bytes(kept)
        self.assertEqual(self.run_once("r2")["status"], "completed")

    def test_reconcile_publishes_a_completed_runs_evidence_from_its_files_and_closes(self):
        self.tied_inputs()
        self.assertEqual(self.run_once()["status"], "completed")
        kept = self.run_file("receipt.json").read_bytes()
        self.run_file("receipt.json").unlink()
        self.assertRefused(self.run_once("r2"), "receipt:missing:r1")
        self.assertEqual(runner.reconcile(self.contract, self.lab, "reconciler", clock=clock)["receipted"], ["r1"])
        published, original = json.loads(self.run_file("receipt.json").read_bytes()), json.loads(kept)
        for key in ("results_sha256", "issuance_ledger_sha256", "issuance_ledger_bytes", "engineering_acceptance",
                    "certificate_sha256", "schema_version"):
            self.assertEqual(published[key], original[key], key)
        self.assertEqual(self.run_once("r2")["status"], "completed")

    def test_the_observed_ledger_not_the_writers_is_compared_and_named(self):
        real_write = runner._write_new

        def changed_after_writing(target, chunks, where):
            digest = real_write(target, chunks, where)
            if where == "issuance_ledger":                  # another complete file by the time it is read back
                target.chmod(0o600)
                target.write_bytes(b"".join(target.read_bytes().splitlines(keepends=True)[:-1]))
            return digest

        self.standard_inputs()
        with mock.patch.object(runner, "_write_new", changed_after_writing):
            result = self.run_once()
        self.assertEqual((result["status"], result["engineering_acceptance"]), ("completed", "failed"))
        results, observed = self.results(), self.run_file("issuance-ledger.jsonl").read_bytes()
        self.assertEqual((results["issuance"]["ledger_sha256"], results["issuance"]["ledger_bytes"]),
                         (runner._sha256_bytes(observed), len(observed)))
        self.assertNotEqual(results["issuance"]["writer_sha256"], results["issuance"]["ledger_sha256"])
        self.assertIn("ledger:not_the_decisions",
                      results["engineering_acceptance"]["criteria"]["issuance_exposure_reconciliation"]["problems"])
        self.assertEqual(self.run_once("r2")["status"], "completed")         # its own evidence agrees

    def test_a_binding_that_is_not_its_results_refuses(self):
        self.tied_inputs()                                  # incomplete results, so the claimed pass is false
        real = runner._binding

        def forged(acceptance, certificate_sha256):
            return dict(real(acceptance, certificate_sha256), status="passed")

        with mock.patch.object(runner, "_binding", forged):
            self.assertEqual(self.run_once()["status"], "completed")         # closes and receipt claim a pass
        self.assertRefused(self.run_once("r2"), "receipt:r1:engineering_acceptance_mismatch")
        self.run_file("receipt.json").unlink()            # reconcile never publishes the closes' wrong claim either
        self.assertEqual(runner.reconcile(self.contract, self.lab, "reconciler", clock=clock),
                         {"status": "refused", "problems": ["receipt:r1:engineering_acceptance_mismatch"]})
        self.assertFalse(self.run_file("receipt.json").exists())

    def test_a_receipt_that_is_not_completed_carries_no_acceptance(self):
        self.standard_inputs()
        integrity = replay.ReplayProblem("prefix:x:member_hash_mismatch")
        with mock.patch.object(runner, "check_prefix", side_effect=integrity):
            self.assertEqual(self.run_once()["status"], "failed")
        receipt = json.loads(self.run_file("receipt.json").read_bytes())
        self.assertEqual((receipt["engineering_acceptance"], receipt["issuance_ledger_bytes"],
                          receipt["results_sha256"]), (None, None, None))
        binding = {"status": "failed", "rule": "x", "criteria_version": 1, "acceptance_sha256": "0" * 64,
                   "certificate_sha256": None}
        for key, value in (("engineering_acceptance", binding), ("issuance_ledger_bytes", 0),
                           ("issuance_ledger_sha256", "0" * 64), ("failure", None)):
            with self.subTest(key=key):
                self.run_file("receipt.json").write_bytes(runner.canonical_json(dict(receipt, **{key: value})) + b"\n")
                self.assertRefused(self.run_once("r2"), "receipt:r1:malformed")

    def test_results_written_otherwise_refuse_by_name(self):
        real_write, real_append = runner._write_new, runner._append

        def results_as(change):
            def write(target, chunks, where):
                if where == "results":
                    value = json.loads(b"".join(chunks))
                    change(value)
                    chunks = [runner.canonical_json(value) + b"\n"]
                return real_write(target, chunks, where)
            return write

        def schema_one(value):
            value["schema_version"] = 1

        def other_bytes(value):
            value["issuance"]["ledger_bytes"] += 1

        def closes_as_schema_one(ledger, kind, *args):               # a schema 1 close names two digests only
            summary = args[-1]
            if kind == "completed":
                summary = {key: summary[key] for key in ("results_sha256", "issuance_ledger_sha256")}
            return real_append(ledger, kind, *args[:-1], summary)

        cases = {"schema 1 results": ([mock.patch.object(runner, "_write_new", results_as(schema_one))],
                                      "results:r1:schema_not_supported"),
                 "schema 1 results and closes": ([mock.patch.object(runner, "_write_new", results_as(schema_one)),
                                                  mock.patch.object(runner, "_append", closes_as_schema_one)],
                                                 "results:r1:schema_not_supported"),
                 "results naming another ledger": ([mock.patch.object(runner, "_write_new", results_as(other_bytes))],
                                                   "results:r1:issuance_not_the_ledgers")}
        for n, (name, (patches, problem)) in enumerate(cases.items()):
            with self.subTest(case=name):
                self.fresh_roots(f"written{n}")
                self.standard_inputs()
                with contextlib.ExitStack() as stack:
                    for patch in patches:
                        stack.enter_context(patch)
                    self.run_once()
                self.assertRefused(self.run_once("r2"), problem)
                self.run_file("receipt.json").unlink()
                self.assertEqual(runner.reconcile(self.contract, self.lab, "reconciler", clock=clock),
                                 {"status": "refused", "problems": [problem]})

    def test_closes_that_claim_another_acceptance_refuse(self):
        real = runner._append

        def passed_closes(ledger, kind, *args):
            summary = args[-1]
            if kind == "completed":
                summary = dict(summary, engineering_acceptance=dict(summary["engineering_acceptance"], status="passed"))
            return real(ledger, kind, *args[:-1], summary)

        self.tied_inputs()
        with mock.patch.object(runner, "_append", passed_closes):
            self.assertEqual(self.run_once()["status"], "completed")
        self.assertRefused(self.run_once("r2"), "receipt:r1:engineering_acceptance_mismatch")
        self.run_file("receipt.json").unlink()
        self.assertEqual(runner.reconcile(self.contract, self.lab, "reconciler", clock=clock),
                         {"status": "refused", "problems": ["receipt:r1:engineering_acceptance_mismatch"]})
        self.assertFalse(self.run_file("receipt.json").exists())

    def test_reconcile_never_publishes_evidence_its_files_do_not_hold(self):
        self.standard_inputs()
        self.assertEqual(self.run_once()["status"], "completed")
        self.run_file("receipt.json").unlink()
        results = self.run_file("results.json")
        results.chmod(0o600)
        results.write_bytes(results.read_bytes() + b" ")
        self.assertEqual(runner.reconcile(self.contract, self.lab, "reconciler", clock=clock),
                         {"status": "refused", "problems": ["receipt:r1:results_sha256_mismatch"]})
        self.assertFalse(self.run_file("receipt.json").exists())

    def test_receipt_fields_follow_its_status(self):
        self.standard_inputs()
        self.assertEqual(self.run_once()["status"], "completed")
        completed = json.loads(self.run_file("receipt.json").read_bytes())
        for name, value in (("a failure on a completed run", dict(completed, failure="x")),
                            ("an extra key", dict(completed, certified_acceptance="passed")),
                            ("a missing key", {k: v for k, v in completed.items() if k != "certificate_sha256"})):
            with self.subTest(case=name):
                self.run_file("receipt.json").write_bytes(runner.canonical_json(value) + b"\n")
                self.assertRefused(self.run_once("r2"), "receipt:r1:malformed")
        self.run_file("receipt.json").write_bytes(runner.canonical_json(completed) + b"\n")

    def test_the_whole_input_population_is_tallied(self):
        later = datetime(2023, 6, 1, 14, 0, tzinfo=timezone.utc)
        no_view = [fx.md("1.100000005", OFF - timedelta(hours=1), fx.definition(OFF, [fx.runner(1)], marketTime=None))]
        late = [fx.md("1.100000007", OFF - timedelta(minutes=4), fx.definition(OFF, [fx.runner(1), fx.runner(2)]))]
        foreign = [fx.md("1.100000003", OFF - timedelta(hours=1), fx.definition(OFF, [fx.runner(1)], countryCode="FR"))]
        self.ingest([(path("1.100000001"), market("1.100000001")),
                     (path("1.100000002", when=later), market("1.100000002", off=later)),
                     (path("1.100000003"), foreign), (path("1.100000005"), no_view), (path("1.100000007"), late)])
        self.assertEqual(self.run_once()["status"], "completed")
        criteria = self.results()["engineering_acceptance"]["criteria"]
        tally = criteria["prefix_invariance"]["components"]["primary_source_prefix_consistency"]
        self.assertEqual((tally["status"], tally["counts"]["records"], tally["counts"]["candidates"],
                          tally["counts"]["compared"], tally["counts"]["not_candidate"], tally["counts"]["missing"]),
                         ("passed", 5, 3, 3, 2, 0))
        self.assertEqual(tally["not_candidate_by_reason"], {"anchor_not_known_by_decision": 1, "no_view": 1})
        self.assertEqual((criteria["issuance_exposure_reconciliation"]["status"],
                          criteria["issuance_exposure_reconciliation"]["problems"]), ("passed", []))

    def test_one_limits_object_reaches_replay_the_prefix_and_the_tally(self):
        stricter = DEFAULT_LIMITS.stricter(max_line_bytes=1 << 20)
        tar = fx.make_tar(self.tmp / "strict.tar", [(path(mid), fx.bz2.compress(fx.lines(market(mid, off=off))))
                                                   for mid, off in (("1.100000001", OFF),
                                                                    ("1.100000002", OFF + timedelta(hours=1)))])
        self.assertEqual(ingest_archive(tar, self.data, stricter)["status"], "ingested")
        self.assertEqual(index_archives(self.data, limits=stricter)["status"], "indexed")
        self.assertEqual(self.run_once(limits=stricter),             # the default certificate is not for them
                         {"status": "refused", "problems": ["certification:limits_not_the_runs"]})
        self.assertEqual(self.lab_entries(), [])
        certified = dict(certificate.LIMITS, read=dataclasses.asdict(stricter))
        with mock.patch.object(certificate, "LIMITS", certified):      # a certificate made under those limits
            self.install_certificate(canned_certificate())
            with mock.patch.object(runner, "check_prefix", wraps=runner.check_prefix) as prefix, \
                    mock.patch.object(runner, "replay_view", wraps=runner.replay_view) as view:
                self.assertEqual(self.run_once(limits=stricter)["status"], "completed")
        self.assertTrue(prefix.call_args_list and view.call_args_list)
        self.assertTrue(all(call.args[5] is stricter for call in prefix.call_args_list))
        self.assertTrue(all(call.args[4] is stricter for call in view.call_args_list))
        tally = self.results()["engineering_acceptance"]["criteria"]["prefix_invariance"]["components"][
            "primary_source_prefix_consistency"]
        self.assertEqual(tally["read_limits"], dataclasses.asdict(stricter))

    def test_a_manifest_without_limits_refuses(self):
        sha = self.ingest([(path("1.100000001"), market("1.100000001"))])
        manifest_path, head_path = self.data / "manifests" / f"{sha}.json", self.data / "index" / sha / "head-v1.json"
        manifest = json.loads(manifest_path.read_bytes())
        del manifest["limits"]
        data = runner.canonical_json(manifest) + b"\n"
        head = json.loads(head_path.read_bytes())
        for item, value in ((manifest_path, data), (head_path, runner.canonical_json(
                dict(head, manifest_sha256=runner._sha256_bytes(data))) + b"\n")):
            item.chmod(0o600)
            item.write_bytes(value)
        self.assertEqual(self.run_once(), {"status": "refused", "problems": [f"input:{sha}:limits_not_the_runs"]})

    def test_a_primary_is_guarded_before_its_prefix_is_read(self):
        records = {"excluded": [fx.md("1.100000003", OFF - timedelta(hours=1),
                                      fx.definition(OFF, [fx.runner(1)], countryCode="FR"))],
                   "unclassifiable": [fx.md("1.100000003", OFF - timedelta(hours=1),
                                            fx.definition(OFF, [fx.runner(1)], marketType=fx.DROP))]}
        real_guard = runner._guard

        def deny(target, where, prospective=False):
            if where == "raw:1.100000003":
                raise runner.RunRefused(f"{where}:denied")
            return real_guard(target, where, prospective)

        for n, (name, messages) in enumerate(records.items()):
            with self.subTest(record=name):
                self.fresh_roots(f"guard{n}")
                self.ingest([(path("1.100000001"), market("1.100000001")), (path("1.100000003"), messages)])
                with mock.patch.object(runner, "_guard", deny), \
                        mock.patch.object(runner, "check_prefix", wraps=runner.check_prefix) as prefix:
                    result = self.run_once()
                self.assertEqual(result["status"], "failed", result)
                self.assertIn("raw:1.100000003:denied", result["problems"][0])
                self.assertNotIn("1.100000003", [call.args[1]["market_id"] for call in prefix.call_args_list])

    def test_the_read_back_goes_through_the_guard(self):
        real_guard = runner._guard

        def deny(target, where, prospective=False):
            if where == "issuance_ledger" and not prospective:
                raise runner.RunRefused(f"{where}:denied")
            return real_guard(target, where, prospective)

        self.standard_inputs()
        with mock.patch.object(runner, "_guard", deny):
            result = self.run_once()
        self.assertEqual(result["status"], "failed", result)
        self.assertIn("issuance_ledger:read_back_failed", result["problems"][0])

    def test_the_read_back_asks_for_at_most_one_byte_more_than_was_written(self):
        """Astra's c2b3b1 acceptance note: the resource contract, not only the outcome (an unbounded read passes the
        small-file outcome tests)."""
        sizes, real_open = [], Path.open

        class Recording:
            def __init__(self, handle):
                self.handle = handle

            def __enter__(self):
                self.handle.__enter__()
                return self

            def __exit__(self, *exc):
                return self.handle.__exit__(*exc)

            def read(self, size=-1):
                sizes.append(size)
                return self.handle.read(size)

        def opening(target, mode="r", *args, **kwargs):
            handle = real_open(target, mode, *args, **kwargs)
            return Recording(handle) if target.name == runner.ISSUANCE_FILE and mode == "rb" else handle

        oversized = self.tmp / runner.ISSUANCE_FILE
        oversized.write_bytes(b"x" * 1024)
        with mock.patch.object(Path, "open", opening):
            with self.assertRaisesRegex(RuntimeError, "^issuance_ledger:read_back_longer_than_written$"):
                runner._read_back(oversized, 64, "issuance_ledger")
        self.assertEqual(sizes, [65])
        sizes.clear()
        self.standard_inputs()
        with mock.patch.object(Path, "open", opening):
            self.assertEqual(self.run_once()["status"], "completed")
        self.assertEqual(sizes, [len(self.run_file(runner.ISSUANCE_FILE).read_bytes()) + 1])


class CertificateBindingTests(RunnerCase):
    """Task c2b3b3: the engine certificate bound into runs (Astra's decisions 3 to 5, 11 and 12, controls C and D)."""

    def setUp(self):
        super().setUp()
        self.contract_value = fx.frozen_contract(max_trials=8)
        self.contract = self.write_contract(self.contract_value, "eight.json")
        self.digest = contract_sha256(self.contract_value)
        self.contract_dir = self.lab / "contracts" / self.digest
        self.runs = self.lab / "runs" / self.digest

    def results(self, run_id="r1"):
        return json.loads(self.run_file("results.json", run_id).read_bytes())

    def test_a_run_needs_a_certified_certificate_for_these_sources_and_this_interpreter(self):
        self.standard_inputs()
        stored = self.lab / "certifications" / f"{CERTIFICATE['key']}.json"
        kept = stored.read_bytes()
        other = canned_certificate(results={"nc_ledger_byte": certificate.NOT_AS_EXPECTED})
        foreign = canned_certificate(interpreter=dict(certificate.interpreter_descriptor(), version="other"))
        cases = {"no certificate": (None, "certification:missing"),
                 "not canonical": (kept + b" ", "certification:malformed:not_canonical"),
                 "a case not as expected": (certificate.certificate_bytes(other), "certification:not_all_passed"),
                 "another interpreter's": (certificate.certificate_bytes(foreign), "certification:not_this_key")}
        stored.chmod(0o600)
        for name, (data, problem) in cases.items():
            with self.subTest(case=name):
                stored.unlink(missing_ok=True)
                if data is not None:
                    stored.write_bytes(data)
                self.assertEqual(self.run_once(), {"status": "refused", "problems": [problem]})
                self.assertEqual(self.lab_entries(), [])
        stored.unlink()
        stored.write_bytes(kept)
        unreadable = mock.patch.object(certificate, "source_descriptor",
                                       side_effect=certificate.CertificateProblem("source:x.py:unreadable"))
        with unreadable:
            self.assertEqual(self.run_once(), {"status": "refused", "problems": ["source:x.py:unreadable"]})
        self.assertEqual(self.lab_entries(), [])

    def test_a_contract_outside_the_certified_profile_refuses(self):
        self.standard_inputs()
        value = fx.frozen_contract()
        value["population"]["countries"] = ["GB"]              # valid, frozen and runnable, but not P0's profile
        self.assertEqual(self.run_once(contract_path=self.write_contract(value, "gb.json")),
                         {"status": "refused", "problems": ["contract:not_supported:not_the_certified_profile"]})
        self.assertEqual(self.lab_entries(), [])

    def test_the_profile_refusal_covers_unreadable_profiles(self):
        self.standard_inputs()
        for error in (KeyError("x"), TypeError("x"), AttributeError("x"), ValueError("x")):
            with self.subTest(error=type(error).__name__), mock.patch.object(runner, "profile_of", side_effect=error):
                self.assertEqual(self.run_once(), {"status": "refused",
                                                   "problems": ["contract:not_supported:not_the_certified_profile"]})
        value = fx.frozen_contract()
        value["costs"]["commission_rate"] = 0.02                 # a published parameter: still the profile
        self.assertEqual(self.run_once(contract_path=self.write_contract(value, "rate.json"))["status"], "completed")

    def test_sources_changed_before_the_starts_refuse_and_at_finalisation_fail_the_run(self):
        self.standard_inputs()
        real, calls = certificate.current_key, []

        def changing_at(n):
            def key(root=None):
                calls.append(1)
                value = real(root)
                return ("0" * 64, *value[1:]) if len(calls) >= n else value
            return key
        with mock.patch.object(runner, "current_key", changing_at(2)):
            self.assertEqual(self.run_once(), {"status": "refused",
                                               "problems": ["source:changed_before_start"]})
        self.assertEqual(self.lab_entries(), ["contracts", f"contracts/{self.digest}", "runs", f"runs/{self.digest}"])
        calls.clear()
        with mock.patch.object(runner, "current_key", changing_at(3)):
            result = self.run_once()
        self.assertEqual(result["status"], "failed", result)
        self.assertEqual(result["problems"], ["RuntimeError:source:changed_during_run"])
        entries = self.ledger()
        self.assertEqual([e["kind"] for e in entries], ["started", "started", "failed", "failed"])
        self.assertEqual([e["summary"]["certificate_sha256"] for e in entries], [self.certificate_sha256] * 4)
        receipt = json.loads(self.run_file("receipt.json").read_bytes())
        self.assertEqual((receipt["status"], receipt["certificate_sha256"], receipt["engineering_acceptance"]),
                         ("failed", self.certificate_sha256, None))
        self.assertFalse(self.run_file("results.json").exists())
        unreadable = certificate.CertificateProblem("source:x.py:unreadable")
        calls.clear()

        def unreadable_at_finalise(root=None):
            calls.append(1)
            if len(calls) >= 3:
                raise unreadable
            return real(root)
        with mock.patch.object(runner, "current_key", unreadable_at_finalise):
            result = self.run_once("r2")
        self.assertEqual(result["status"], "failed", result)
        self.assertEqual(result["problems"], ["RuntimeError:source:changed_during_run:source:x.py:unreadable"])
        self.assertEqual(self.run_once("r3")["engineering_acceptance"], "passed")   # failed runs do not stop it

    def test_history_is_checked_against_its_own_certificate_not_todays(self):
        self.tied_inputs()
        self.assertEqual(self.run_once("r1")["engineering_acceptance"], "incomplete")
        patch, changed = self.other_sources()
        with patch:
            self.assertEqual(self.run_once("r2"), {"status": "refused", "problems": ["certification:missing"]})
            self.install_certificate(canned_certificate(descriptor=changed))
            self.assertEqual(self.run_once("r2")["status"], "completed")
        first, second = self.results("r1")["certification"], self.results("r2")["certification"]
        self.assertNotEqual(first["key"], second["key"])
        self.assertEqual(self.run_once("r3")["status"], "completed")          # back to the first sources

    def test_history_never_reads_a_certificate_again(self):
        self.tied_inputs()
        self.assertEqual(self.run_once()["status"], "completed")
        stored = self.lab / "certifications" / f"{CERTIFICATE['key']}.json"
        kept = stored.read_bytes()
        stored.unlink()
        self.refused_unchanged("certification:missing")
        self.run_file("receipt.json").unlink()
        result = runner.reconcile(self.contract, self.lab, "reconciler", clock=clock)
        self.assertEqual((result["status"], result["receipted"]), ("reconciled", ["r1"]))
        self.assertEqual(json.loads(self.run_file("receipt.json").read_bytes())["certificate_sha256"],
                         runner._sha256_bytes(kept))
        stored.write_bytes(kept)
        self.assertEqual(self.run_once("r2")["status"], "completed")

    def test_every_binding_names_the_same_certificate(self):
        real_write, real_append = runner._write_new, runner._append

        def results_as(change):
            def write(target, chunks, where):
                if where == "results":
                    value = json.loads(b"".join(chunks))
                    change(value["certification"])
                    chunks = [runner.canonical_json(value) + b"\n"]
                return real_write(target, chunks, where)
            return write

        def other_sha(certification):
            certification["sha256"] = "0" * 64

        def not_certified(certification):
            certification["certificate"]["status"] = "not_certified"
            certification["sha256"] = runner._sha256_bytes(runner.canonical_json(certification["certificate"]) + b"\n")

        def second_start_other(ledger, kind, *args):
            summary = args[-1]
            if kind == "started" and args[0].endswith("seeded_random_runner"):
                summary = {"certificate_sha256": "0" * 64}
            return real_append(ledger, kind, *args[:-1], summary)

        def closes_other(ledger, kind, *args):
            summary = args[-1]
            if kind == "completed":
                summary = dict(summary, certificate_sha256="0" * 64)
            return real_append(ledger, kind, *args[:-1], summary)

        def starts_and_closes_other(ledger, kind, *args):       # they agree; the results and binding do not
            summary = args[-1]
            if kind in ("started", "completed"):
                summary = dict(summary, certificate_sha256="0" * 64)
            return real_append(ledger, kind, *args[:-1], summary)

        cases = {"results naming other bytes": (mock.patch.object(runner, "_write_new", results_as(other_sha)),
                                                "results:r1:certificate_not_its_sha256"),
                 "results naming an uncertified certificate": (
                     mock.patch.object(runner, "_write_new", results_as(not_certified)),
                     "results:r1:certificate_not_certified"),
                 "a start naming another certificate": (mock.patch.object(runner, "_append", second_start_other),
                                                        "ledger:r1:certificates_disagree"),
                 "closes naming another certificate": (mock.patch.object(runner, "_append", closes_other),
                                                       "receipt:r1:certificate_sha256_mismatch"),
                 "starts and closes naming another certificate than the results": (
                     mock.patch.object(runner, "_append", starts_and_closes_other),
                     "receipt:r1:certificate_sha256_mismatch")}
        for n, (name, (patch, problem)) in enumerate(cases.items()):
            with self.subTest(case=name):
                self.fresh_roots(f"bound{n}")
                self.tied_inputs()
                with patch:
                    self.assertEqual(self.run_once()["status"], "completed")
                before = self.lab_files()
                self.assertRefused(self.run_once("r2"), problem)
                self.assertEqual(self.lab_files(), before)
                self.run_file("receipt.json").unlink()
                del before[str(self.run_file("receipt.json"))]
                self.assertEqual(runner.reconcile(self.contract, self.lab, "reconciler", clock=clock),
                                 {"status": "refused", "problems": [problem]})
                self.assertEqual(self.lab_files(), before)                      # nothing written by either
        self.fresh_roots("receipt")
        self.tied_inputs()
        self.assertEqual(self.run_once()["status"], "completed")
        receipt = json.loads(self.run_file("receipt.json").read_bytes())
        other = dict(receipt["engineering_acceptance"], certificate_sha256="0" * 64)
        for value, problem in ((dict(receipt, certificate_sha256="0" * 64), "receipt:r1:malformed"),
                               (dict(receipt, certificate_sha256="0" * 64, engineering_acceptance=other),
                                "receipt:r1:engineering_acceptance_mismatch"),
                               (dict(receipt, certificate_sha256=None), "receipt:r1:malformed")):
            with self.subTest(receipt=problem):
                self.run_file("receipt.json").write_bytes(runner.canonical_json(value) + b"\n")
                self.assertRefused(self.run_once("r2"), problem)
        self.fresh_roots("failed")
        self.tied_inputs()
        problem = replay.ReplayProblem("prefix:x:member_hash_mismatch")
        with mock.patch.object(runner, "check_prefix", side_effect=problem):
            self.assertEqual(self.run_once()["status"], "failed")
        receipt = json.loads(self.run_file("receipt.json").read_bytes())
        self.assertEqual(receipt["certificate_sha256"], self.certificate_sha256)
        for value in ("0" * 64, None):
            with self.subTest(failed_receipt=value):
                self.run_file("receipt.json").write_bytes(runner.canonical_json(dict(receipt, certificate_sha256=value))
                                                          + b"\n")
                self.assertRefused(self.run_once("r2"), "receipt:r1:certificate_sha256_mismatch")

    def test_component_ii_must_name_the_bound_certificate(self):
        real_write = runner._write_new

        def write(target, chunks, where):
            if where == "results":
                value = json.loads(b"".join(chunks))
                value["engineering_acceptance"]["criteria"]["prefix_invariance"]["components"][
                    "synthetic_certification"]["certificate_sha256"] = "0" * 64
                chunks = [runner.canonical_json(value) + b"\n"]
            return real_write(target, chunks, where)

        self.tied_inputs()
        with mock.patch.object(runner, "_write_new", write):
            self.assertEqual(self.run_once()["status"], "completed")
        self.refused_unchanged("results:r1:certification_not_the_acceptances")
        self.run_file("receipt.json").unlink()
        self.assertEqual(runner.reconcile(self.contract, self.lab, "reconciler", clock=clock),
                         {"status": "refused", "problems": ["results:r1:certification_not_the_acceptances"]})
        for forged in ([], {"criteria": []}, {"criteria": {"prefix_invariance": "x"}}):
            with self.subTest(forged=forged):
                self.assertIsNone(runner._component_ii(forged))

    def test_an_embedded_certificate_of_another_kind_refuses(self):
        real_write = runner._write_new

        def other_kind(target, chunks, where):
            if where == "results":
                value = json.loads(b"".join(chunks))
                value["certification"]["certificate"]["kind"] = "something_else"
                value["certification"]["sha256"] = runner._sha256_bytes(
                    runner.canonical_json(value["certification"]["certificate"]) + b"\n")
                chunks = [runner.canonical_json(value) + b"\n"]
            return real_write(target, chunks, where)

        self.tied_inputs()
        with mock.patch.object(runner, "_write_new", other_kind):
            self.assertEqual(self.run_once()["status"], "completed")
        self.refused_unchanged("results:r1:certificate_not_certified")

    def test_failed_closes_naming_another_certificate_than_their_starts_refuse(self):
        real_append = runner._append

        def failed_other(ledger, kind, *args):
            summary = args[-1]
            if kind == "failed":
                summary = dict(summary, certificate_sha256="0" * 64)
            return real_append(ledger, kind, *args[:-1], summary)

        self.tied_inputs()
        integrity = replay.ReplayProblem("prefix:x:member_hash_mismatch")
        with mock.patch.object(runner, "_append", failed_other), \
                mock.patch.object(runner, "check_prefix", side_effect=integrity):
            self.assertEqual(self.run_once()["status"], "failed")
        self.refused_unchanged("ledger:r1:certificates_disagree")

    def test_reconcile_keeps_a_started_certificate_and_never_invents_one(self):
        self.tied_inputs()
        real = TrialLedger.append

        def crash_at_completed(ledger, kind, **kwargs):
            if kind == "completed":
                raise trial_ledger.LedgerError("ledger:head_moved")
            return real(ledger, kind, **kwargs)
        with mock.patch.object(TrialLedger, "append", crash_at_completed):
            self.assertEqual(self.run_once("r1")["status"], "uncertain")
        (self.lab / "contracts" / self.digest / runner.LOCK_NAME).unlink()
        self.assertEqual(runner.reconcile(self.contract, self.lab, "reconciler", clock=clock)["status"], "reconciled")
        aborted = [e for e in self.ledger() if e["kind"] == "aborted"]
        self.assertEqual([e["summary"]["certificate_sha256"] for e in aborted], [self.certificate_sha256] * 2)
        receipt = json.loads(self.run_file("receipt.json").read_bytes())
        self.assertEqual((receipt["status"], receipt["certificate_sha256"]), ("aborted", self.certificate_sha256))
        (self.lab / "runs" / self.digest / "x1").mkdir()                 # an unaccounted run directory
        self.assertEqual(runner.reconcile(self.contract, self.lab, "reconciler", clock=clock)["recorded_runs"], ["x1"])
        recorded = [e for e in self.ledger() if e["trial_id"].startswith("x1.")]
        self.assertEqual({e["summary"]["certificate_sha256"] for e in recorded}, {None})
        self.assertIsNone(json.loads(self.run_file("receipt.json", "x1").read_bytes())["certificate_sha256"])
        self.assertEqual(self.run_once("r2")["status"], "completed")

    def test_reconcile_refuses_disagreeing_starts_before_writing(self):
        self.tied_inputs()
        contract_dir = self.lab / "contracts" / self.digest
        contract_dir.mkdir(parents=True)
        ledger = TrialLedger.open(contract_dir)
        for variant, sha in (("ltp_favourite", "a" * 64), ("seeded_random_runner", "b" * 64)):
            ledger.append("started", trial_id=f"x.{variant}", contract=self.contract_value, variant=variant,
                          input_sha256="ab" * 32, code_identity="git:old", recorded_at="2026-10-15T00:00:00Z",
                          summary={"certificate_sha256": sha})
        before = self.lab_files()
        self.assertEqual(runner.reconcile(self.contract, self.lab, "reconciler", clock=clock),
                         {"status": "refused", "problems": ["ledger:x:certificates_disagree"]})
        self.assertEqual(self.lab_files(), before)

    def test_a_malformed_started_certificate_refuses_reconcile_before_any_append(self):
        self.tied_inputs()
        self.contract_dir.mkdir(parents=True)
        ledger = TrialLedger.open(self.contract_dir)
        ledger.append("started", trial_id="crashed.ltp_favourite", contract=self.contract_value,
                      variant="ltp_favourite", input_sha256="ab" * 32, code_identity="git:old",
                      recorded_at="2026-10-15T00:00:00Z", summary={"certificate_sha256": "x"})
        before = self.lab_files()
        self.assertEqual(runner.reconcile(self.contract, self.lab, "reconciler", clock=clock),
                         {"status": "refused", "problems": ["ledger:crashed:certificate_malformed"]})
        self.assertEqual(self.lab_files(), before)

    def test_a_reconstructed_run_with_a_forged_certificate_refuses(self):
        self.tied_inputs()
        self.assertEqual(self.run_once()["status"], "completed")
        (self.runs / "x1").mkdir()
        self.assertEqual(runner.reconcile(self.contract, self.lab, "reconciler", clock=clock)["recorded_runs"], ["x1"])
        receipt_path = self.run_file("receipt.json", "x1")
        receipt = json.loads(receipt_path.read_bytes())
        receipt_path.write_bytes(runner.canonical_json(dict(receipt, certificate_sha256=self.certificate_sha256))
                                 + b"\n")
        self.refused_unchanged("receipt:x1:certificate_sha256_mismatch")

    def test_sources_that_cannot_be_described_before_the_starts_refuse(self):
        self.standard_inputs()
        real, calls = certificate.current_key, []

        def unreadable_before_start(root=None):
            calls.append(1)
            if len(calls) >= 2:                                   # the preflight call, then the before-start one
                raise certificate.CertificateProblem("source:x.py:unreadable")
            return real(root)
        with mock.patch.object(runner, "current_key", unreadable_before_start):
            self.refused_unchanged("source:changed_before_start:source:x.py:unreadable", run_id="r1")
        self.assertEqual(self.lab_entries(), ["contracts", f"contracts/{self.digest}", "runs", f"runs/{self.digest}"])

    def test_sources_changed_while_the_run_evaluates_fail_it_after_its_issuance_is_published(self):
        self.standard_inputs()
        real_settle, real_key, settled = runner.settle, certificate.current_key, []

        def settle(*args):
            settled.append(1)                                     # the sources change while the run settles
            return real_settle(*args)

        def key(root=None):
            value = real_key(root)
            return ("0" * 64, *value[1:]) if settled else value
        with mock.patch.object(runner, "settle", settle), mock.patch.object(runner, "current_key", key), \
                mock.patch.object(runner, "_read_back", wraps=runner._read_back) as read_back:
            result = self.run_once()
        self.assertEqual((result["status"], result["problems"]), ("failed", ["RuntimeError:source:changed_during_run"]))
        self.assertEqual([e["kind"] for e in self.ledger()], ["started", "started", "failed", "failed"])
        self.assertEqual([call.args[2] for call in read_back.call_args_list], ["issuance_ledger"])
        self.assertTrue(self.run_file(runner.ISSUANCE_FILE).exists())       # published and read back: evidence
        self.assertFalse(self.run_file(runner.RESULTS_FILE).exists())

    def test_results_naming_another_key_than_their_certificate_refuse(self):
        real_write = runner._write_new

        def results_as(change):
            def write(target, chunks, where):
                if where == "results":
                    value = json.loads(b"".join(chunks))
                    change(value["certification"])
                    chunks = [runner.canonical_json(value) + b"\n"]
                return real_write(target, chunks, where)
            return write

        def results_key(certification):
            certification["key"] = "0" * 64

        def certificate_key(certification):
            certification["certificate"]["key"] = "0" * 64
            certification["sha256"] = runner._sha256_bytes(runner.canonical_json(certification["certificate"]) + b"\n")

        problem = "results:r1:certificate_not_certified"
        for n, change in enumerate((results_key, certificate_key)):
            with self.subTest(forged=change.__name__):
                self.fresh_roots(f"key{n}")
                self.tied_inputs()
                with mock.patch.object(runner, "_write_new", results_as(change)):
                    self.assertEqual(self.run_once()["status"], "completed")
                self.refused_unchanged(problem)
                self.run_file("receipt.json").unlink()
                before = self.lab_files()
                self.assertEqual(runner.reconcile(self.contract, self.lab, "reconciler", clock=clock),
                                 {"status": "refused", "problems": [problem]})
                self.assertEqual(self.lab_files(), before)

    def test_a_run_naming_no_certificate_beside_one_that_names_a_sha256_refuses(self):
        real_append = runner._append

        def starts_as(summary):
            def append(ledger, kind, *args):
                return real_append(ledger, kind, *args[:-1], summary if kind == "started" else args[-1])
            return append

        problem = "ledger:r1:certificates_disagree"
        for n, summary in enumerate(({}, {"certificate_sha256": None})):     # a start as before c2b3b3, or null
            with self.subTest(started=summary):
                self.fresh_roots(f"none{n}")
                self.tied_inputs()
                with mock.patch.object(runner, "_append", starts_as(summary)):
                    self.assertEqual(self.run_once()["status"], "completed")
                self.refused_unchanged(problem)
                self.run_file("receipt.json").unlink()
                before = self.lab_files()
                self.assertEqual(runner.reconcile(self.contract, self.lab, "reconciler", clock=clock),
                                 {"status": "refused", "problems": [problem]})
                self.assertEqual(self.lab_files(), before)

    def test_a_results_certification_of_another_shape_is_malformed(self):
        real_write = runner._write_new

        def results_as(change):
            def write(target, chunks, where):
                if where == "results":
                    value = json.loads(b"".join(chunks))
                    value["certification"] = change(value["certification"])
                    chunks = [runner.canonical_json(value) + b"\n"]
                return real_write(target, chunks, where)
            return write

        cases = {"with another key": lambda certification: dict(certification, extra=1),
                 "a key that is not a sha256": lambda certification: dict(certification, key="x"),
                 "a sha256 that is not a sha256": lambda certification: dict(certification, sha256="x"),
                 "a certificate that is not an object": lambda certification: dict(certification, certificate="x"),
                 "null, as c2b3b1 wrote it": lambda certification: None}
        for n, (name, change) in enumerate(cases.items()):
            with self.subTest(certification=name):
                self.fresh_roots(f"shape{n}")
                self.tied_inputs()
                with mock.patch.object(runner, "_write_new", results_as(change)):
                    self.assertEqual(self.run_once()["status"], "completed")
                self.refused_unchanged("results:r1:malformed")
                self.run_file("receipt.json").unlink()
                before = self.lab_files()
                self.assertEqual(runner.reconcile(self.contract, self.lab, "reconciler", clock=clock),
                                 {"status": "refused", "problems": ["results:r1:malformed"]})
                self.assertEqual(self.lab_files(), before)

    def test_a_receipt_certificate_that_is_not_a_sha256_is_malformed(self):
        self.tied_inputs()
        self.assertEqual(self.run_once()["status"], "completed")
        receipt = json.loads(self.run_file("receipt.json").read_bytes())
        era = dict(receipt, certificate_sha256=None,               # as c2b3b1 wrote it: no certificate anywhere
                   engineering_acceptance=dict(receipt["engineering_acceptance"], certificate_sha256=None))
        self.run_file("receipt.json").write_bytes(runner.canonical_json(era) + b"\n")
        self.refused_unchanged("receipt:r1:malformed")
        self.fresh_roots("failed")
        self.tied_inputs()
        problem = replay.ReplayProblem("prefix:x:member_hash_mismatch")
        with mock.patch.object(runner, "check_prefix", side_effect=problem):
            self.assertEqual(self.run_once()["status"], "failed")
        receipt = json.loads(self.run_file("receipt.json").read_bytes())
        for value in ("x", 7, "A" * 64):
            with self.subTest(failed_receipt=value):
                self.run_file("receipt.json").write_bytes(runner.canonical_json(dict(receipt, certificate_sha256=value))
                                                          + b"\n")
                self.refused_unchanged("receipt:r1:malformed")

    def test_reconcile_checks_every_unfinished_runs_certificate_before_any_append(self):
        self.contract_dir.mkdir(parents=True)
        ledger = TrialLedger.open(self.contract_dir)
        for trial_id, sha in (("a.ltp_favourite", "a" * 64), ("b.ltp_favourite", "x")):     # a's first: it is fine
            ledger.append("started", trial_id=trial_id, contract=self.contract_value, variant="ltp_favourite",
                          input_sha256="ab" * 32, code_identity="git:old", recorded_at="2026-10-15T00:00:00Z",
                          summary={"certificate_sha256": sha})
        before = self.lab_files()
        self.assertEqual(runner.reconcile(self.contract, self.lab, "reconciler", clock=clock),
                         {"status": "refused", "problems": ["ledger:b:certificate_malformed"]})
        self.assertEqual(self.lab_files(), before)

    def test_reconcile_aborts_each_unfinished_run_under_its_own_certificate(self):
        self.contract_dir.mkdir(parents=True)
        ledger = TrialLedger.open(self.contract_dir)
        shas, variants = {"a": "a" * 64, "b": "b" * 64}, ("ltp_favourite", "seeded_random_runner")
        for run_id, sha in shas.items():
            (self.runs / run_id).mkdir(parents=True)                         # for the receipt reconcile publishes
            for variant in variants:
                ledger.append("started", trial_id=f"{run_id}.{variant}", contract=self.contract_value, variant=variant,
                              input_sha256="ab" * 32, code_identity="git:old", recorded_at="2026-10-15T00:00:00Z",
                              summary={"certificate_sha256": sha})
        result = runner.reconcile(self.contract, self.lab, "reconciler", clock=clock)
        aborted = {e["trial_id"]: e["summary"]["certificate_sha256"] for e in self.ledger() if e["kind"] == "aborted"}
        self.assertEqual(aborted, {f"{run_id}.{variant}": sha for run_id, sha in shas.items() for variant in variants})
        self.assertEqual((result["status"], result.get("receipted")), ("reconciled", ["a", "b"]), result)
        self.assertEqual({run_id: json.loads(self.run_file("receipt.json", run_id).read_bytes())["certificate_sha256"]
                          for run_id in shas}, shas)

    def test_history_never_needs_a_certificate_file(self):
        self.tied_inputs()
        self.assertEqual(self.run_once()["status"], "completed")
        shutil.rmtree(self.lab / certificate.CERTIFICATIONS_DIR)
        before = self.lab_files()
        result = runner.reconcile(self.contract, self.lab, "reconciler", clock=clock)     # r1 checked whole
        self.assertEqual(result["status"], "reconciled", result)
        self.assertEqual((result["aborted"], result["recorded_runs"], result["receipted"]), ([], [], []))
        self.assertEqual(self.lab_files(), before)
        self.assertFalse((self.lab / certificate.CERTIFICATIONS_DIR).exists())


class StopRuleTests(RunnerCase):
    """A pass stops the contract (Astra's decision 7 and control D): verified passes and surviving unbound claims."""

    def setUp(self):
        super().setUp()
        self.contract_value = fx.frozen_contract(max_trials=8)
        self.contract = self.write_contract(self.contract_value, "eight.json")
        self.digest = contract_sha256(self.contract_value)
        self.contract_dir = self.lab / "contracts" / self.digest
        self.runs = self.lab / "runs" / self.digest

    def passed(self, run_id="r1"):
        self.standard_inputs()
        self.assertEqual(self.run_once(run_id)["engineering_acceptance"], "passed")

    def test_a_verified_pass_stops_the_contract_whatever_changes(self):
        self.passed()
        self.refused_unchanged("contract:engineering_passed:r1")
        self.refused_unchanged("contract:engineering_passed:r1", run_id="r1")          # before the run id check
        self.refused_unchanged("contract:engineering_passed:r1", code_identity="git:other")
        patch, changed = self.other_sources()
        with patch:
            self.install_certificate(canned_certificate(descriptor=changed))
            self.refused_unchanged("contract:engineering_passed:r1")                    # new sources and certificate
        stored = self.lab / "certifications" / f"{CERTIFICATE['key']}.json"
        stored.unlink()                                       # another certificate for today's key
        self.install_certificate(canned_certificate(evidence="other"))
        self.assertNotEqual(self.certificate_sha256, json.loads(self.run_file("receipt.json").read_bytes())[
            "certificate_sha256"])
        self.refused_unchanged("contract:engineering_passed:r1")
        result = runner.reconcile(self.contract, self.lab, "reconciler", clock=clock)    # available, changes nothing
        self.assertEqual((result["status"], result["aborted"], result["receipted"]), ("reconciled", [], []))
        self.refused_unchanged("contract:engineering_passed:r1")

    def test_the_stop_comes_before_the_budget(self):
        self.contract_value = fx.frozen_contract(max_trials=2)
        self.contract = self.write_contract(self.contract_value, "two.json")
        self.digest = contract_sha256(self.contract_value)
        self.passed()
        self.refused_unchanged("contract:engineering_passed:r1")

    def test_the_stop_holds_within_one_root_and_one_contract(self):
        self.standard_inputs()
        self.assertEqual(self.run_once()["engineering_acceptance"], "passed")
        self.refused_unchanged("contract:engineering_passed:r1")
        six = self.write_contract(fx.frozen_contract(max_trials=6), "six.json")
        self.assertEqual(self.run_once("r2", contract_path=six)["engineering_acceptance"], "passed")
        self.lab = self.tmp / "lab-new"
        self.lab.mkdir()
        self.install_certificate()
        self.assertEqual(self.run_once("r2")["engineering_acceptance"], "passed")

    def test_a_failed_or_incomplete_run_does_not_stop_it(self):
        self.tied_inputs()
        self.assertEqual(self.run_once("r1")["engineering_acceptance"], "incomplete")
        self.assertEqual(self.run_once("r2")["engineering_acceptance"], "incomplete")

    def test_missing_evidence_after_a_pass_never_frees_the_contract(self):
        self.passed()
        receipt = self.run_file("receipt.json").read_bytes()
        self.run_file("receipt.json").unlink()
        self.refused_unchanged("receipt:missing:r1")
        self.assertEqual(runner.reconcile(self.contract, self.lab, "reconciler", clock=clock)["receipted"], ["r1"])
        published = json.loads(self.run_file("receipt.json").read_bytes())
        self.assertEqual((published["certificate_sha256"], published["engineering_acceptance"]),
                         (json.loads(receipt)["certificate_sha256"], json.loads(receipt)["engineering_acceptance"]))
        self.refused_unchanged("contract:engineering_passed:r1")
        results = self.run_file("results.json")
        kept = results.read_bytes()
        results.unlink()
        self.refused_unchanged("results:r1:missing_path")
        before = self.lab_files()
        self.assertEqual(runner.reconcile(self.contract, self.lab, "reconciler", clock=clock),
                         {"status": "refused", "problems": ["results:r1:missing_path"]})
        self.assertEqual(self.lab_files(), before)
        results.write_bytes(kept)
        shutil.rmtree(self.runs / "r1")                       # the whole run directory
        self.refused_unchanged("receipt:missing:r1")
        before = self.lab_files()
        self.assertEqual(runner.reconcile(self.contract, self.lab, "reconciler", clock=clock),
                         {"status": "refused", "problems": ["receipt:r1:no_run_directory"]})
        self.assertEqual(self.lab_files(), before)

    def test_a_pass_interrupted_before_its_closes_still_stops_it(self):
        self.standard_inputs()
        real = runner._append

        def killed_at_the_closes(ledger, kind, *args):
            if kind == "completed":
                raise KeyboardInterrupt                      # results.json is written; no close is
            return real(ledger, kind, *args)

        with mock.patch.object(runner, "_append", killed_at_the_closes), self.assertRaises(KeyboardInterrupt):
            self.run_once("r1")
        self.assertEqual(json.loads(self.run_file("results.json").read_bytes())["engineering_acceptance"]["status"],
                         "passed")
        (self.contract_dir / runner.LOCK_NAME).unlink()       # a person, after checking no runner runs
        self.refused_unchanged("ledger:unfinished_trials:r1.ltp_favourite,r1.seeded_random_runner")
        result = runner.reconcile(self.contract, self.lab, "reconciler", clock=clock)
        self.assertEqual((result["status"], result["aborted"], result["receipted"]),
                         ("reconciled", ["r1.ltp_favourite", "r1.seeded_random_runner"], ["r1"]))
        self.refused_unchanged("contract:engineering_pass_claim_unbound:r1")

    def test_a_pass_between_its_closes_still_stops_it(self):
        self.standard_inputs()
        real = TrialLedger.append

        def second_close_refused(ledger, kind, **kwargs):
            if kind == "completed" and kwargs["variant"] == "seeded_random_runner":
                raise trial_ledger.LedgerError("ledger:head_moved")
            return real(ledger, kind, **kwargs)
        with mock.patch.object(TrialLedger, "append", second_close_refused):
            self.assertEqual(self.run_once("r1")["status"], "uncertain")
        (self.contract_dir / runner.LOCK_NAME).unlink()
        self.refused_unchanged("ledger:unfinished_trials:r1.seeded_random_runner")
        self.assertEqual(runner.reconcile(self.contract, self.lab, "reconciler", clock=clock)["receipted"], ["r1"])
        self.assertEqual(json.loads(self.run_file("receipt.json").read_bytes())["status"], "aborted")
        self.refused_unchanged("contract:engineering_passed:r1")
        self.run_file("results.json").unlink()                # the completed close's claim survives alone
        self.refused_unchanged("contract:engineering_passed:r1")

    def test_a_pass_in_a_run_cut_out_of_the_ledger_still_stops_it(self):
        self.passed()
        (self.contract_dir / LEDGER_FILE).unlink()
        self.run_file("receipt.json").unlink()
        self.refused_unchanged("run:unaccounted:r1")
        result = runner.reconcile(self.contract, self.lab, "reconciler", clock=clock)
        self.assertEqual((result["status"], result["recorded_runs"], result["receipted"]),
                         ("reconciled", ["r1"], ["r1"]))
        self.assertEqual({e["summary"]["certificate_sha256"] for e in self.ledger()}, {None})
        self.refused_unchanged("contract:engineering_pass_claim_unbound:r1")

    def test_an_unbound_pass_survives_interrupted_recovery(self):
        self.standard_inputs()
        self.assertEqual(self.run_once()["engineering_acceptance"], "passed")
        results = self.run_file("results.json").read_bytes()
        (self.contract_dir / LEDGER_FILE).unlink()
        self.run_file("receipt.json").unlink()
        self.refused_unchanged("run:unaccounted:r1")
        real = TrialLedger.append

        def interrupted(ledger, kind, **kwargs):
            if kind == "started" and kwargs["variant"] == "seeded_random_runner":
                raise trial_ledger.LedgerError("ledger:head_moved")
            return real(ledger, kind, **kwargs)

        with mock.patch.object(TrialLedger, "append", interrupted):
            self.assertEqual(runner.reconcile(self.contract, self.lab, "reconciler", clock=clock)["status"],
                             "uncertain")
        (self.contract_dir / runner.LOCK_NAME).unlink()
        self.refused_unchanged("run:recovery_incomplete:r1")
        result = runner.reconcile(self.contract, self.lab, "reconciler", clock=clock)
        self.assertEqual((result["status"], result["receipted"]), ("reconciled", ["r1"]))
        self.refused_unchanged("contract:engineering_pass_claim_unbound:r1")
        self.assertEqual([(e["kind"], e["summary"]["certificate_sha256"]) for e in self.ledger()],
                         [("started", None), ("aborted", None)] * 2)
        receipt = json.loads(self.run_file("receipt.json").read_bytes())
        self.assertEqual((receipt["status"], receipt["certificate_sha256"], receipt["engineering_acceptance"]),
                         ("aborted", None, None))
        self.assertEqual(self.run_file("results.json").read_bytes(), results)

    def test_partial_and_unreadable_results_of_a_failed_run(self):
        self.standard_inputs()
        real = runner._write_new

        def results_then_fail(keep):
            def write(target, chunks, where):
                if where != "results":
                    return real(target, chunks, where)
                data = b"".join(chunks)
                real(target, [data[:len(data) // 2] if keep == "half" else data], where)
                raise runner.PublicationUncertain(f"{where}:synthetic")
            return write
        with mock.patch.object(runner, "_write_new", results_then_fail("half")):
            self.assertEqual(self.run_once("r1")["status"], "failed")
        real_open = Path.open

        def unreadable(target, mode="r", *args, **kwargs):
            if target.name == "results.json" and target.parent.name == "r1":
                raise PermissionError(13, "synthetic")
            return real_open(target, mode, *args, **kwargs)
        before = self.lab_files()
        with mock.patch.object(Path, "open", unreadable):
            self.assertRefused(self.run_once("r2"), "results:r1:unreadable")
        self.assertEqual(self.lab_files(), before)
        self.assertEqual(self.run_once("r2")["engineering_acceptance"], "passed")      # a cut write claims nothing
        self.fresh_roots("whole")
        self.standard_inputs()
        with mock.patch.object(runner, "_write_new", results_then_fail("all")):
            self.assertEqual(self.run_once("r1")["status"], "failed")
        self.refused_unchanged("contract:engineering_pass_claim_unbound:r1")                 # a whole one blocks

    def test_a_results_file_that_is_not_canonical_claims_nothing(self):
        real_write = runner._write_new

        def results_then_fail(target, chunks, where):
            if where != "results":
                return real_write(target, chunks, where)
            value = json.loads(b"".join(chunks))
            real_write(target, [json.dumps(value, indent=1).encode()], where)      # valid JSON, not canonical
            raise runner.PublicationUncertain(f"{where}:synthetic")

        self.standard_inputs()
        with mock.patch.object(runner, "_write_new", results_then_fail):
            self.assertEqual(self.run_once()["status"], "failed")
        self.assertEqual(json.loads(self.run_file("results.json").read_bytes())["engineering_acceptance"]["status"],
                         "passed")
        self.assertEqual(self.run_once("r2")["engineering_acceptance"], "passed")       # it claimed nothing

    def test_a_results_file_that_is_not_regular_refuses_and_never_blocks(self):
        self.tied_inputs()
        problem = replay.ReplayProblem("prefix:x:member_hash_mismatch")
        with mock.patch.object(runner, "check_prefix", side_effect=problem):
            self.assertEqual(self.run_once()["status"], "failed")
        target = self.run_file("results.json")
        elsewhere = self.tmp / "elsewhere.json"
        elsewhere.write_bytes(b"{}\n")
        target.symlink_to(elsewhere)
        self.refused_unchanged("results:r1:not_a_regular_file")
        target.unlink()
        os.mkfifo(target)
        self.refused_unchanged("results:r1:not_a_regular_file")
        target.unlink()
        self.assertEqual(self.run_once("r2")["status"], "completed")

    def test_a_forged_pass_in_a_failed_close_is_an_unbound_claim(self):
        real = runner._append

        def failed_claiming(ledger, kind, *args):
            summary = args[-1]
            if kind == "failed":
                summary = dict(summary, engineering_acceptance={"status": "passed"})
            return real(ledger, kind, *args[:-1], summary)

        self.tied_inputs()
        with mock.patch.object(runner, "_append", failed_claiming), mock.patch.object(
                runner, "check_prefix", side_effect=replay.ReplayProblem("prefix:x:member_hash_mismatch")):
            self.assertEqual(self.run_once()["status"], "failed")
        self.refused_unchanged("contract:engineering_pass_claim_unbound:r1")

    def test_only_this_contracts_claims_count(self):
        other = fx.frozen_contract(max_trials=6)                 # another contract's passing results, copied here
        self.standard_inputs()
        self.assertEqual(self.run_once("r1", contract_path=self.write_contract(other, "six.json"))[
            "engineering_acceptance"], "passed")
        self.runs.mkdir(parents=True)
        (self.runs / "x1").mkdir()
        (self.runs / "x1" / "results.json").write_bytes(
            (self.lab / "runs" / contract_sha256(other) / "r1" / "results.json").read_bytes())
        self.refused_unchanged("run:unaccounted:x1")
        self.assertEqual(runner.reconcile(self.contract, self.lab, "reconciler", clock=clock)["recorded_runs"], ["x1"])
        self.assertEqual(self.run_once("r2")["engineering_acceptance"], "passed")

    def test_a_results_write_cut_just_before_its_final_newline_still_claims(self):
        real = runner._write_new

        def all_but_the_newline(target, chunks, where):
            if where != "results":
                return real(target, chunks, where)
            real(target, [b"".join(chunks)[:-1]], where)
            raise runner.PublicationUncertain(f"{where}:synthetic")

        self.standard_inputs()
        with mock.patch.object(runner, "_write_new", all_but_the_newline):
            self.assertEqual(self.run_once("r1")["status"], "failed")
        data = self.run_file("results.json").read_bytes()
        self.assertEqual(runner.canonical_json(json.loads(data)), data)          # canonical, only its newline cut
        self.refused_unchanged("contract:engineering_pass_claim_unbound:r1")
        self.run_file("results.json").write_bytes(data + b"\n\n")               # one newline too many: not canonical
        self.assertEqual(self.run_once("r2")["engineering_acceptance"], "passed")       # it claims nothing

    def test_a_verified_pass_stops_it_before_another_runs_files_are_read(self):
        self.standard_inputs()
        problem = replay.ReplayProblem("prefix:x:member_hash_mismatch")
        with mock.patch.object(runner, "check_prefix", side_effect=problem):
            self.assertEqual(self.run_once("r1")["status"], "failed")
        self.assertEqual(self.run_once("r2")["engineering_acceptance"], "passed")
        self.run_file("results.json").mkdir()                 # r1's would refuse the scan: not a regular file
        self.refused_unchanged("contract:engineering_passed:r2", run_id="r3")

    def test_a_dangling_or_uninspectable_results_file_refuses(self):
        self.tied_inputs()
        problem = replay.ReplayProblem("prefix:x:member_hash_mismatch")
        with mock.patch.object(runner, "check_prefix", side_effect=problem):
            self.assertEqual(self.run_once()["status"], "failed")
        target = self.run_file("results.json")
        target.symlink_to(self.tmp / "nowhere.json")         # present to lstat, absent to exists()
        self.refused_unchanged("results:r1:not_a_regular_file")
        target.unlink()
        target.write_bytes(b"{}\n")
        for name in ("lstat", "is_symlink", "is_file"):
            real = getattr(Path, name)

            def failing(item, *args, real=real, **kwargs):
                if item == target:
                    raise PermissionError(13, "synthetic: permission denied")
                return real(item, *args, **kwargs)
            before = self.lab_files()
            with self.subTest(method=name):
                with mock.patch.object(Path, name, failing):
                    result = self.run_once("r2")
                self.assertEqual(result, {"status": "refused", "problems": ["results:r1:uninspectable"]})
                self.assertEqual(self.lab_files(), before)
        self.assertEqual(self.run_once("r2")["status"], "completed")

    def test_the_stop_scan_reads_results_through_the_guard(self):
        real_guard = runner._guard

        def deny(target, where, prospective=False):
            if where == "results:r1":
                raise runner.RunRefused(f"{where}:denied")
            return real_guard(target, where, prospective)

        self.tied_inputs()
        problem = replay.ReplayProblem("prefix:x:member_hash_mismatch")
        with mock.patch.object(runner, "check_prefix", side_effect=problem):
            self.assertEqual(self.run_once()["status"], "failed")
        self.run_file("results.json").write_bytes(b"{}\n")
        with mock.patch.object(runner, "_guard", deny), \
                mock.patch.object(runner, "_read_capped", wraps=runner._read_capped) as read:
            self.refused_unchanged("results:r1:denied")
        self.assertNotIn("results:r1", [call.args[1] for call in read.call_args_list])     # denied before it is read
        self.assertEqual(self.run_once("r2")["status"], "completed")

    def test_a_results_file_nested_too_deep_to_parse_claims_nothing(self):
        self.tied_inputs()
        problem = replay.ReplayProblem("prefix:x:member_hash_mismatch")
        with mock.patch.object(runner, "check_prefix", side_effect=problem):
            self.assertEqual(self.run_once()["status"], "failed")
        depth = 10 ** 6
        data = b"[" * depth + b"]" * depth + b"\n"
        try:
            runner.loads_strict(data)
        except RecursionError:
            pass
        else:
            self.skipTest("this interpreter parses it")
        self.run_file("results.json").write_bytes(data)
        self.assertEqual(self.run_once("r2")["status"], "completed")

    def test_a_results_file_that_is_not_a_passing_results_object_claims_nothing(self):
        real = runner._write_new

        def results_as(change):
            def write(target, chunks, where):
                if where != "results":
                    return real(target, chunks, where)
                real(target, [runner.canonical_json(change(json.loads(b"".join(chunks)))) + b"\n"], where)
                raise runner.PublicationUncertain(f"{where}:synthetic")
            return write

        changes = {"another_kind": lambda value: dict(value, kind=runner.RECEIPT_KIND),
                   "an_array": lambda value: [value],
                   "a_string_acceptance": lambda value: dict(value, engineering_acceptance="passed")}
        for name, change in changes.items():
            with self.subTest(case=name):
                self.fresh_roots(name)
                self.standard_inputs()
                with mock.patch.object(runner, "_write_new", results_as(change)):
                    self.assertEqual(self.run_once("r1")["status"], "failed")
                self.assertEqual(self.run_once("r2")["engineering_acceptance"], "passed")

    def test_a_stop_names_every_passed_run_sorted(self):
        self.contract_value = fx.frozen_contract(max_trials=12)
        self.contract = self.write_contract(self.contract_value, "twelve.json")
        self.digest = contract_sha256(self.contract_value)
        self.passed("r2")
        with mock.patch.object(runner, "_stop_evidence", return_value=([], [])):        # a runner without the rule
            for run_id in ("r1.x", "r10", "a.b", "r1-y", "b"):
                self.assertEqual(self.run_once(run_id)["engineering_acceptance"], "passed")
        self.refused_unchanged("contract:engineering_passed:a.b,b,r1-y,r1.x,r10,r2", run_id="r3")

    def test_an_unbound_stop_names_every_claiming_run_sorted(self):
        self.contract_value = fx.frozen_contract(max_trials=12)
        self.contract = self.write_contract(self.contract_value, "twelve.json")
        self.digest = contract_sha256(self.contract_value)
        real = runner._write_new

        def results_then_fail(target, chunks, where):
            if where != "results":
                return real(target, chunks, where)
            real(target, [b"".join(chunks)], where)
            raise runner.PublicationUncertain(f"{where}:synthetic")

        self.standard_inputs()
        with mock.patch.object(runner, "_write_new", results_then_fail):
            self.assertEqual(self.run_once("r2")["status"], "failed")
        claim = json.loads(self.run_file("results.json", "r2").read_bytes())
        copies = ["r1.x", "r10", "a.b", "r1-y", "b"]
        for run_id in copies:                                 # its whole claim, as its own, recorded by reconcile
            self.run_file("results.json", run_id).parent.mkdir()
            self.run_file("results.json", run_id).write_bytes(runner.canonical_json(dict(claim, run_id=run_id)) + b"\n")
        self.assertEqual(runner.reconcile(self.contract, self.lab, "reconciler", clock=clock)["recorded_runs"],
                         sorted(copies))
        self.refused_unchanged("contract:engineering_pass_claim_unbound:a.b,b,r1-y,r1.x,r10,r2", run_id="r3")


class ExactIntentTests(RunnerCase):
    """A recovery intent names exactly the contract's variants, sorted (Astra's control E)."""

    def write_intent(self, run_id, variants):
        run = self.lab / "runs" / self.digest / run_id
        run.mkdir(parents=True)
        intent = {"kind": runner.RECOVERY_KIND, "schema_version": 1, "contract_sha256": self.digest,
                  "run_id": run_id, "variants": variants, "reason": "unaccounted_run", "recorded_by": "someone"}
        (run / runner.RECOVERY_FILE).write_bytes(runner.canonical_json(intent) + b"\n")
        return run

    def test_an_intent_must_name_exactly_the_contract_variants(self):
        cases = {"a subset": (["ltp_favourite"], "variants_not_the_contracts"),
                 "an unknown variant": (["ltp_favourite", "zzz"], "variants_not_the_contracts"),
                 "an extra variant": (["ltp_favourite", "seeded_random_runner", "zzz"], "variants_not_the_contracts"),
                 "unsorted": (["seeded_random_runner", "ltp_favourite"], "malformed"),
                 "repeated": (["ltp_favourite", "ltp_favourite", "seeded_random_runner"], "malformed")}
        for n, (name, (variants, reason)) in enumerate(cases.items()):
            with self.subTest(case=name):
                self.fresh_roots(f"intent{n}")
                self.standard_inputs()
                self.write_intent("x1", variants)
                before = self.lab_files()
                self.assertRefused(self.run_once(), f"recovery:x1:{reason}")
                self.assertEqual(runner.reconcile(self.contract, self.lab, "reconciler", clock=clock),
                                 {"status": "refused", "problems": [f"recovery:x1:{reason}"]})
                self.assertEqual(self.lab_files(), before)                     # nothing written by either

    def test_a_forged_receipt_cannot_ride_on_a_one_variant_intent(self):
        self.standard_inputs()
        run = self.write_intent("x1", ["ltp_favourite"])
        (run / "receipt.json").write_bytes(runner.canonical_json(runner._receipt(
            self.digest, "x1", "aborted", "reconciled", None,
            [("ltp_favourite", "x1.ltp_favourite", (0, "0" * 64), (1, "0" * 64), None)], (1, "0" * 64), 1,
            "2026-10-15T00:00:00Z", "forger")) + b"\n")                  # well formed: only the intent is wrong
        self.assertRefused(self.run_once(), "recovery:x1:variants_not_the_contracts")

    def test_an_exact_intent_is_finished_by_reconcile(self):
        self.standard_inputs()
        self.write_intent("x1", ["ltp_favourite", "seeded_random_runner"])
        self.assertRefused(self.run_once(), "run:recovery_incomplete:x1")
        result = runner.reconcile(self.contract, self.lab, "reconciler", clock=clock)
        self.assertEqual((result["status"], result["receipted"]), ("reconciled", ["x1"]))
        receipt = json.loads((self.lab / "runs" / self.digest / "x1" / "receipt.json").read_bytes())
        self.assertEqual((receipt["status"], receipt["engineering_acceptance"], receipt["issuance_ledger_bytes"]),
                         ("aborted", None, None))
        self.assertEqual(self.run_once()["status"], "completed")


class CliTests(RunnerCase):
    def test_run_backtest_and_reconcile_trials(self):
        self.standard_inputs()
        out = io.StringIO()
        with redirect_stdout(out), mock.patch.object(runner, "utc_now_text", return_value="2026-10-15T00:00:00Z"):
            code = main(["run-backtest", "--contract", str(self.contract), "--data-root", str(self.data),
                         "--lab-state-root", str(self.lab), "--run-id", "cli", "--code-identity", CODE])
        printed = json.loads(out.getvalue())
        self.assertEqual((code, printed["status"], printed["engineering_acceptance"]), (0, "completed", "passed"))
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["run-backtest", "--contract", str(self.contract), "--data-root", str(self.data),
                         "--lab-state-root", str(self.lab), "--run-id", "cli", "--code-identity", CODE])
        self.assertEqual((code, json.loads(out.getvalue())), (2, {"status": "refused",
                                                                 "problems": ["contract:engineering_passed:cli"]}))
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["reconcile-trials", "--contract", str(self.contract), "--lab-state-root", str(self.lab),
                         "--code-identity", CODE])
        self.assertEqual((code, json.loads(out.getvalue())),
                         (0, {"status": "reconciled", "aborted": [], "recorded_runs": [], "receipted": [],
                              "ledger_head": [3, self.ledger()[3]["entry_sha256"]]}))


    def test_run_backtest_exit_codes(self):
        argv = ["run-backtest", "--contract", str(self.contract), "--data-root", str(self.data),
                "--lab-state-root", str(self.lab), "--run-id", "cli", "--code-identity", CODE]
        for result, code in (({"status": "completed", "engineering_acceptance": "passed"}, 0),
                             ({"status": "completed", "engineering_acceptance": "failed"}, 4),
                             ({"status": "completed", "engineering_acceptance": "incomplete"}, 4),
                             ({"status": "failed", "engineering_acceptance": None}, 2),
                             ({"status": "uncertain"}, 2), ({"status": "refused"}, 2)):
            with self.subTest(result=result), redirect_stdout(io.StringIO()), \
                    mock.patch.object(runner, "run_backtest", return_value=result):
                self.assertEqual(main(argv), code)
        self.standard_inputs()                                  # a real failed acceptance: completed, exit 4
        real_fold = replay._fold
        out = io.StringIO()
        with redirect_stdout(out), \
                mock.patch.object(replay, "_fold", lambda raw, mid, t, lim: real_fold(raw, mid, t + 180000, lim)), \
                mock.patch.object(replay, "_upto", lambda timeline, t, real=replay._upto: real(timeline, t + 180000)):
            code = main(argv)
        printed = json.loads(out.getvalue())
        self.assertEqual((code, printed["status"], printed["engineering_acceptance"]), (4, "completed", "failed"))

if __name__ == "__main__":
    unittest.main()
