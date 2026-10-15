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
import tempfile
import unittest
from unittest import mock

from betangel_research.betfair.backtest import runner, trial_ledger
from betangel_research.betfair.backtest.__main__ import main
from betangel_research.betfair.backtest.archive import ingest_archive, safe_target
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
        self.assertEqual(results["engineering_acceptance"]["status"], "not_evaluated")
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
        self.standard_inputs()
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
        self.standard_inputs()
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
        self.standard_inputs()
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
        self.standard_inputs()
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
                                              "pending": ["crashed.ltp_favourite"]})
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
        self.standard_inputs()
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


class EmptyInputTests(RunnerCase):
    """Astra's design B (2026-10-15): a corpus with no record, or with no record eligible under the contract's own
    population rules, refuses before the lock or any write, and only after the whole first pass; one eligible record
    starts the trials, which are charged whatever their decisions."""

    def lab_entries(self):
        return sorted(str(item.relative_to(self.lab)) for item in self.lab.rglob("*"))

    def fresh_roots(self, name):
        """A new data root and lab-state root, so one case never sees another's archives or starts."""
        self.data, self.lab = self.tmp / f"data-{name}", self.tmp / f"lab-{name}"
        self.data.mkdir()
        self.lab.mkdir()

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
        self.standard_inputs()
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
        cases = {"receipt:r1:status_not_the_ledgers": dict(receipt, status="aborted", results_sha256=None),
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
        self.ingest([(path("1.100000001"), market("1.100000001"))])
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
        forged = runner._receipt(self.digest, "r1", "aborted", "reconciled", None, None,
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
        self.ingest([(path("1.100000001"), market("1.100000001"))])
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


class CliTests(RunnerCase):
    def test_run_backtest_and_reconcile_trials(self):
        self.standard_inputs()
        out = io.StringIO()
        with redirect_stdout(out), mock.patch.object(runner, "utc_now_text", return_value="2026-10-15T00:00:00Z"):
            code = main(["run-backtest", "--contract", str(self.contract), "--data-root", str(self.data),
                         "--lab-state-root", str(self.lab), "--run-id", "cli", "--code-identity", CODE])
        self.assertEqual((code, json.loads(out.getvalue())["status"]), (0, "completed"))
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["run-backtest", "--contract", str(self.contract), "--data-root", str(self.data),
                         "--lab-state-root", str(self.lab), "--run-id", "cli", "--code-identity", CODE])
        self.assertEqual((code, json.loads(out.getvalue())["status"]), (2, "refused"))
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["reconcile-trials", "--contract", str(self.contract), "--lab-state-root", str(self.lab),
                         "--code-identity", CODE])
        self.assertEqual((code, json.loads(out.getvalue())),
                         (0, {"status": "reconciled", "aborted": [], "recorded_runs": [], "receipted": [],
                              "ledger_head": [3, self.ledger()[3]["entry_sha256"]]}))


if __name__ == "__main__":
    unittest.main()
