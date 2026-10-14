"""python3 -m betangel_research.betfair.backtest: offline backtest tools.

validate-contract --contract PATH [--require-frozen] reads one local experiment contract and prints one JSON
line {"status", "contract_sha256", "problems"}: status is valid_draft or valid_frozen (exit 0) or invalid
(exit 2; argparse usage errors also exit 2). With --require-frozen a valid_draft exits 3, so a shell gate
before a run cannot mistake a draft for a frozen contract. An invalid contract's sha256 only names the file;
ledger writers must check status == valid_frozen.

archive-ingest --tar PATH --data-root DIR ingests one Betfair Historical Data TAR (archive.py); archive-index
--data-root DIR [--archive SHA256] indexes one or every ingested archive (market_index.py); archive-audit
--data-root DIR prints the capability and coverage report (coverage_audit.py). Each prints one JSON line and
exits 0, or 2 when its status is refused (every problem listed, file-system failures included as refusals with
their place) or the arguments are invalid. The data root is
always explicit and must lie outside every Git worktree and this repository. Those three modules are imported
inside _archive_command, so validate-contract still loads contract.py alone
(tests/test_betfair_backtest_contract.py lists what it loads).

run-backtest --contract PATH --data-root DIR --lab-state-root DIR --run-id ID --code-identity TEXT runs one frozen
contract over every indexed archive under the data root (runner.py) and prints one JSON line: status completed (exit
0), or refused, failed or uncertain (exit 2). reconcile-trials --contract PATH --lab-state-root DIR --code-identity
TEXT closes, as aborted, the contract's trials that started and never closed (exit 0 when reconciled, else 2).
runner is imported inside _runner_command, as the archive modules are.

Everything here imports only the standard library and this package: no Betfair client CLI, gateway, account,
credential, profile or network module (tests/test_betfair_backtest_boundaries.py). Nothing here places an order,
logs in or reads an account.
"""

import argparse
import json
import sys

from .contract import check_contract_file

EXIT_INVALID = 2
EXIT_NOT_FROZEN = 3


def build_parser():
    parser = argparse.ArgumentParser(prog="python3 -m betangel_research.betfair.backtest",
                                     description="Offline backtest tools; nothing here places an order.")
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate-contract", help="validate an experiment contract and print its sha256")
    validate.add_argument("--contract", required=True, help="path to a local experiment contract JSON file")
    validate.add_argument("--require-frozen", action="store_true", help="exit 3 when the contract is a valid draft")
    ingest = commands.add_parser("archive-ingest", help="ingest one Betfair Historical Data TAR into a data root")
    ingest.add_argument("--tar", required=True, help="path to one local TAR as downloaded from the portal")
    ingest.add_argument("--data-root", required=True, help="existing directory outside every Git worktree")
    index = commands.add_parser("archive-index", help="index the markets of ingested archives")
    index.add_argument("--data-root", required=True, help="existing directory outside every Git worktree")
    index.add_argument("--archive", help="sha256 of one ingested archive (default: every archive)")
    audit = commands.add_parser("archive-audit", help="print the capability and coverage report")
    audit.add_argument("--data-root", required=True, help="existing directory outside every Git worktree")
    run = commands.add_parser("run-backtest", help="run one frozen contract over the indexed archives")
    run.add_argument("--contract", required=True, help="path to a local frozen experiment contract JSON file")
    run.add_argument("--data-root", required=True, help="existing directory outside every Git worktree")
    run.add_argument("--lab-state-root", required=True, help="existing directory for ledgers, runs and receipts")
    run.add_argument("--run-id", required=True, help="one path component naming this run")
    run.add_argument("--code-identity", required=True, help="printable text naming the code, for instance a commit")
    reconcile = commands.add_parser("reconcile-trials", help="abort a contract's unfinished trials, record "
                                    "unaccounted runs as spent and publish missing receipts")
    reconcile.add_argument("--contract", required=True, help="path to a local frozen experiment contract JSON file")
    reconcile.add_argument("--lab-state-root", required=True, help="existing directory for ledgers, runs and receipts")
    reconcile.add_argument("--code-identity", required=True, help="printable text naming who reconciles")
    return parser


def _archive_command(args):
    """The result of one archive subcommand; imported here so validate-contract never loads these modules."""
    from . import archive, coverage_audit, market_index
    if args.command == "archive-ingest":
        return archive.ingest_archive(args.tar, args.data_root)
    if args.command == "archive-index":
        return market_index.index_archives(args.data_root, args.archive)
    return coverage_audit.audit(args.data_root)


def _runner_command(args):
    """(result, exit code) of one runner subcommand; imported here so validate-contract never loads the runner."""
    from . import runner
    if args.command == "run-backtest":
        result = runner.run_backtest(args.contract, args.data_root, args.lab_state_root, args.run_id,
                                     args.code_identity)
        return result, 0 if result["status"] == "completed" else EXIT_INVALID
    result = runner.reconcile(args.contract, args.lab_state_root, args.code_identity)
    return result, 0 if result["status"] == "reconciled" else EXIT_INVALID


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.command in ("run-backtest", "reconcile-trials"):
        result, code = _runner_command(args)
        sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
        return code
    if args.command != "validate-contract":
        result = _archive_command(args)
        sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
        return EXIT_INVALID if result["status"] == "refused" else 0
    result = check_contract_file(args.contract)
    sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
    if result["status"] == "invalid":
        return EXIT_INVALID
    return EXIT_NOT_FROZEN if args.require_frozen and result["status"] != "valid_frozen" else 0


if __name__ == "__main__":
    raise SystemExit(main())
