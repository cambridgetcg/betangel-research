"""python3 -m betangel_research.betfair.backtest: offline backtest tools (one subcommand so far).

validate-contract --contract PATH [--require-frozen] reads one local experiment contract and prints one JSON
line {"status", "contract_sha256", "problems"}: status is valid_draft or valid_frozen (exit 0) or invalid
(exit 2; argparse usage errors also exit 2). With --require-frozen a valid_draft exits 3, so a shell gate
before a run cannot mistake a draft for a frozen contract. An invalid contract's sha256 only names the file;
ledger writers must check status == valid_frozen. It imports only the standard library and contract.py: no
Betfair client CLI, gateway, account, credential, profile or network module
(tests/test_betfair_backtest_boundaries.py).
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
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    result = check_contract_file(args.contract)
    sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
    if result["status"] == "invalid":
        return EXIT_INVALID
    return EXIT_NOT_FROZEN if args.require_frozen and result["status"] != "valid_frozen" else 0


if __name__ == "__main__":
    raise SystemExit(main())
