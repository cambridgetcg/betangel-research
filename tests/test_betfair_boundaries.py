"""Static boundaries of the Betfair package, checked by AST and text scans only (nothing is imported).

These show where order method names, capabilities, secrets, transport calls and imports may appear;
they cannot show that the code behind those places is correct. Tests themselves are exempt (they build
forbidden names by concatenation where they need them).
"""

import ast
from pathlib import Path
import re
import sys
import unittest

REPO = Path(__file__).resolve().parent.parent
PACKAGE = REPO / "betangel_research"
BETFAIR = PACKAGE / "betfair"
ORDER_SUFFIX = "Orders"
ORDER_LITERALS = ("place" + ORDER_SUFFIX, "cancel" + ORDER_SUFFIX)
NEVER_ANYWHERE = ("update" + ORDER_SUFFIX, "Heartbeat" + "APING")
# Live replace was removed in v1 (amendment A3, D2): its method name is nowhere in the package.
REMOVED_ORDER_LITERALS = ("replace" + ORDER_SUFFIX,)
INTERNAL_ONLY = ("api", "transport", "auth", "esa", "capability", "risk")
PAPER_PATH = ("paper", "journal", "lifecycle", "risk", "validate", "ticks", "pnl", "schemas", "capability", "presence")
NETWORK_MODULES = ("socket", "ssl", "http", "urllib", "transport", "api", "auth", "esa")


def betfair_files():
    return sorted(BETFAIR.glob("*.py"))


def code_files():
    """Every non-test code file in the repository's package and scripts."""
    return sorted(PACKAGE.rglob("*.py")) + sorted((REPO / "scripts").glob("*.py"))


def tree(path):
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def calls(path, predicate):
    return [node for node in ast.walk(tree(path)) if isinstance(node, ast.Call) and predicate(node)]


def called_name(node):
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def imported_modules(path):
    """(module text, level, names) for every import in a file; level > 0 is relative."""
    found = []
    for node in ast.walk(tree(path)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.append((alias.name, 0, [alias.name]))
        elif isinstance(node, ast.ImportFrom):
            found.append((node.module or "", node.level, [alias.name for alias in node.names]))
    return found


def imports_betfair_module(path, name):
    """True if the file imports betfair.<name> (relative or absolute)."""
    in_betfair = path.parent == BETFAIR
    for module, level, names in imported_modules(path):
        if level == 0:
            if module == f"betangel_research.betfair.{name}" or (module == "betangel_research.betfair" and name in names):
                return True
        elif in_betfair and level == 1 and (module == name or (module == "" and name in names)):
            return True
        elif level == 1 and path.parent == PACKAGE and (module == f"betfair.{name}"
                                                        or (module == "betfair" and name in names)):
            return True
    return False


def strings(path):
    return [node.value for node in ast.walk(tree(path)) if isinstance(node, ast.Constant) and isinstance(node.value, str)]


class BoundaryTests(unittest.TestCase):
    def test_order_method_literals_only_in_gateway(self):
        for path in code_files():
            text = path.read_text(encoding="utf-8")
            for literal in ORDER_LITERALS:
                with self.subTest(file=path.name, literal=literal):
                    if path == BETFAIR / "gateway.py":       # whole names only ("replaceOrders" holds "placeOrders")
                        named = [s for s in strings(path) if re.search(r"(?<![A-Za-z])" + literal, s)]
                        self.assertEqual(len(named), 1)
                    else:
                        self.assertNotIn(literal, text)
        constants = [s for s in strings(BETFAIR / "gateway.py") if s.startswith("SportsAPING/v1.0/")]
        self.assertEqual(sorted(constants), sorted("SportsAPING/v1.0/" + name for name in ORDER_LITERALS))

    def test_live_replace_method_and_verb_removed(self):
        """Amendment A3 (D2): replaceOrders appears nowhere in the package (not even in a comment), and the
        risk guard's ACTION_VERBS has no replace verb, so no capability can be minted for a replace body."""
        for path in code_files():
            text = path.read_text(encoding="utf-8")
            for literal in REMOVED_ORDER_LITERALS:
                with self.subTest(file=path.name, literal=literal):
                    self.assertNotIn(literal, text)
                    self.assertNotIn(literal.lower(), text.lower())
        verbs = None
        for node in ast.walk(tree(BETFAIR / "risk.py")):
            if (isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "ACTION_VERBS"
                                                     for t in node.targets)):
                verbs = ast.literal_eval(node.value)
        self.assertEqual(verbs, {"place": "place", "cancel": "cancel", "cancel_all": "cancel"})
        for name in ("PROBE_REPLACE_PRICE_H", "REPLACE_METHOD", "normalise_replace_report", "_replace_live",
                     "_refresh_parents", "replace_echo_line", "refresh_open_orders", "close_from_cleared",
                     "settle_from_cleared", "cleared_move", "_child_candidate", "pnl_basis_mismatch",
                     "_pnl_mismatch", "inferred_child_match"):
            with self.subTest(name=name):
                for path in betfair_files():
                    self.assertNotIn(name, path.read_text(encoding="utf-8"), path.name)

    def test_send_bounds_passed_only_from_the_place_path(self):
        """Amendment A3, C2: a send-by and a first-byte gate are passed by the gateway's place path alone (one
        call, in _place_core); cancels, sweep cancels and cancel_all pass neither. Inside the package only the
        transport's own plumbing names them as keywords (the API client forwards them unnamed)."""
        bound_keywords = {"send_gate", "send_by_monotonic_ns"}
        for path in code_files():
            passed = calls(path, lambda node: any(k.arg in bound_keywords for k in node.keywords))
            with self.subTest(file=str(path.relative_to(REPO))):
                if path == BETFAIR / "gateway.py":
                    owners = [node.name for node in ast.walk(tree(path)) if isinstance(node, ast.FunctionDef)
                              and any(isinstance(call, ast.Call) and any(k.arg in bound_keywords for k in call.keywords)
                                      for call in ast.walk(node))]
                    self.assertEqual(len(passed), 1)
                    self.assertEqual(owners, ["_place_core"])
                elif path != BETFAIR / "transport.py":
                    self.assertEqual(passed, [])

    def test_anchor_write_called_only_by_publish_anchor(self):
        """Re-review R1: every Keychain anchor write goes through credentials.publish_anchor, which serializes
        publications under paths.anchor_lock and reads the journal head inside it. Nothing else in the package
        or scripts calls or imports anchor_write (gateway open and close, the kill, resume all publish)."""
        for path in code_files():
            found = calls(path, lambda node: called_name(node) == "anchor_write")
            imported = [names for _, _, names in imported_modules(path) if "anchor_write" in names]
            with self.subTest(file=str(path.relative_to(REPO))):
                if path == BETFAIR / "credentials.py":
                    owners = [node.name for node in ast.walk(tree(path)) if isinstance(node, ast.FunctionDef)
                              and any(isinstance(call, ast.Call) and called_name(call) == "anchor_write"
                                      for call in ast.walk(node))]
                    self.assertEqual((len(found), owners), (1, ["publish_anchor"]))
                else:
                    self.assertEqual((found, imported), ([], []))

    def test_hedge_credit_true_only_at_the_listed_call_sites(self):
        """Amendment A3, C1: exposure gives a matched portion no hedge credit unless a call site opts in. Every
        hedge_credit= keyword in the package is listed here with its exact value: the literal True only for R12's
        explained exposure in evaluate_place, the paper-only condition only in status and the journal summary,
        and plain pass-through inside risk.py. The defaults are False (module functions) and None (the guard's
        own setting, paper only)."""
        sites = set()
        for path in code_files():
            for function in ast.walk(tree(path)):
                if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                inner = {id(n) for child in ast.walk(function) if child is not function
                         and isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
                         for n in ast.walk(child)}
                for node in ast.walk(function):
                    if isinstance(node, ast.Call) and id(node) not in inner:
                        for keyword in node.keywords:
                            if keyword.arg == "hedge_credit":
                                sites.add((path.name, function.name, ast.unparse(keyword.value)))
        self.assertEqual(sites, {
            ("risk.py", "exposure_by_market", "hedge_credit"),
            ("risk.py", "_exposure", "credit"),
            ("risk.py", "evaluate_place", "True"),
            ("gateway.py", "status", "self.mode == 'paper'"),
            ("cli.py", "_journal_summary", "mode == 'paper'"),
        })
        risk_tree = tree(BETFAIR / "risk.py")
        defaults = {}
        for function in ast.walk(risk_tree):
            if isinstance(function, ast.FunctionDef):
                for arg, default in zip(function.args.kwonlyargs, function.args.kw_defaults):
                    if arg.arg == "hedge_credit":
                        defaults[function.name] = ast.unparse(default)
        self.assertEqual(defaults, {"worst_case_market_loss": "False", "exposure_by_market": "False",
                                    "_exposure": "None"})
        exact = [ast.unparse(node) for node in ast.walk(risk_tree) if isinstance(node, ast.Call)
                 and called_name(node) == "_exact_cp"]
        self.assertEqual(exact, ["_exact_cp(order, outcome, hedge_credit)"])
        assigned = [ast.unparse(node) for node in ast.walk(risk_tree) if isinstance(node, ast.Assign)
                    and any(isinstance(t, ast.Attribute) and t.attr == "hedge_credit" for t in node.targets)]
        self.assertEqual(assigned, ["self.hedge_credit = mode == 'paper'"])

    def test_kill_lock_wait_and_give_way_machinery_removed(self):
        """Amendment A3, D4: the kill takes no lock, so the lock wait, the holder's give-way checks, the
        holder's own cancel after a kill (spec step 16), the sourceIdKey kill leg, the wall-clock re-sweep
        trigger, kill_file_only and the unreported-cancel-all scan are gone, not left dormant."""
        for name in ("kill_lock_wait_s", "KILL_LOCK_MARGIN_S", "_kill_arrived", "_kill_watch",
                     "reconcile_abandoned_for_kill", "reconcile_deferred_for_kill", "stopped_for_kill",
                     "RECENT_SUBMIT_S", "_cancel_after_kill", "account_wide_cancel_already_unreported",
                     "_clocks_disagree_since", "book_clock_disagreement_before_send", "self._journal_unfoldable",
                     "self._chain_broken", "journal_unfoldable_sweep_uses_exchange_legs"):
            with self.subTest(name=name):
                for path in betfair_files():
                    self.assertNotIn(name, path.read_text(encoding="utf-8"), path.name)
        for path in betfair_files():
            with self.subTest(file=path.name):
                self.assertNotIn("kill_file_only", strings(path))
        for path in (BETFAIR / "killswitch.py", BETFAIR / "gateway.py"):
            text = path.read_text(encoding="utf-8")
            with self.subTest(file=path.name):
                self.assertNotIn("last_submit_wall_ns", text)
                self.assertNotIn("source_id_key", text)
                self.assertNotIn("time_ns", ast.unparse(next(
                    node for node in ast.walk(tree(BETFAIR / "killswitch.py"))
                    if isinstance(node, ast.FunctionDef) and node.name == "sweep")))

    def test_no_update_orders_or_heartbeat_anywhere(self):
        targets = code_files() + [REPO / "research" / "betfair-config.example.json", REPO / "Dockerfile"]
        for path in targets:
            text = path.read_text(encoding="utf-8")
            for literal in NEVER_ANYWHERE:
                with self.subTest(file=path.name, literal=literal):
                    self.assertNotIn(literal, text)
                    self.assertNotIn(literal.lower(), text.lower().replace("heartbeat_api_enabled", ""))

    def test_capability_mint_only_called_in_risk(self):
        for path in betfair_files():
            minted = calls(path, lambda node: called_name(node) == "_mint")
            imported = [names for module, level, names in imported_modules(path) if "_mint" in names]
            with self.subTest(file=path.name):
                if path.name == "risk.py":
                    self.assertEqual(len(minted), 1)
                    self.assertEqual(len(imported), 1)
                elif path.name == "capability.py":
                    self.assertEqual(minted, [])
                else:
                    self.assertEqual(minted, [])
                    self.assertEqual(imported, [])
                    self.assertNotIn("_MINT_TOKEN", path.read_text(encoding="utf-8"))

    def test_order_capability_constructed_only_in_capability(self):
        for path in betfair_files():
            built = calls(path, lambda node: called_name(node) == "OrderCapability")
            with self.subTest(file=path.name):
                self.assertEqual(len(built), 1 if path.name == "capability.py" else 0)

    def test_reveal_only_in_transport_auth_esa_credentials_killswitch_gateway(self):
        allowed = {"transport.py", "auth.py", "esa.py", "credentials.py", "killswitch.py", "gateway.py"}
        for path in code_files():
            revealed = calls(path, lambda node: called_name(node) == "reveal")
            with self.subTest(file=str(path.relative_to(REPO))):
                if path.parent != BETFAIR or path.name not in allowed:
                    self.assertEqual(revealed, [])

    def test_transport_request_called_only_in_auth_api(self):
        for path in betfair_files():
            requested = calls(path, lambda node: isinstance(node.func, ast.Attribute) and node.func.attr == "request")
            with self.subTest(file=path.name):
                if path.name not in ("auth.py", "api.py"):
                    self.assertEqual(requested, [])

    def test_order_digest_kw_only_in_api(self):
        for path in code_files():
            passed = calls(path, lambda node: any(k.arg == "order_digest" for k in node.keywords))
            with self.subTest(file=str(path.relative_to(REPO))):
                self.assertEqual(len(passed), 1 if path == BETFAIR / "api.py" else 0)

    def test_send_and_issue_only_in_gateway(self):
        for path in code_files():
            for name in ("send_order", "prepare_order", "issue"):
                used = calls(path, lambda node, name=name: isinstance(node.func, ast.Attribute)
                             and node.func.attr == name)
                with self.subTest(file=path.name, call=name):
                    if path != BETFAIR / "gateway.py":
                        self.assertEqual(used, [])

    def test_gateway_imported_only_by_cli(self):
        for path in code_files():
            with self.subTest(file=str(path.relative_to(REPO))):
                if path in (BETFAIR / "cli.py", BETFAIR / "gateway.py"):
                    continue
                self.assertFalse(imports_betfair_module(path, "gateway"))
        self.assertTrue(imports_betfair_module(BETFAIR / "cli.py", "gateway"))

    def test_api_transport_auth_esa_capability_risk_imported_only_within_package(self):
        outside = [p for p in code_files() if p.parent != BETFAIR]
        for path in outside:
            for name in INTERNAL_ONLY:
                with self.subTest(file=str(path.relative_to(REPO)), module=name):
                    self.assertFalse(imports_betfair_module(path, name))
            for module, level, names in imported_modules(path):
                self.assertNotIn("betfair", module)

    def test_stdlib_only_imports(self):
        stdlib = set(sys.stdlib_module_names)
        for path in betfair_files():
            for module, level, names in imported_modules(path):
                with self.subTest(file=path.name, module=module):
                    if level:
                        self.assertLessEqual(level, 2)
                        if level == 2:                  # only betangel_research root modules
                            self.assertIn(module or names[0], ("evaluation", "temporal", "stream"))
                        continue
                    self.assertIn(module.split(".")[0], stdlib)

    def test_no_logging_module(self):
        for path in code_files():
            for module, level, names in imported_modules(path):
                with self.subTest(file=str(path.relative_to(REPO)), module=module):
                    self.assertNotEqual(module.split(".")[0], "logging")
                    self.assertNotIn("logging", names if level == 0 else [])

    def test_root_modules_do_not_import_betfair(self):
        for path in sorted(PACKAGE.glob("*.py")):
            with self.subTest(file=path.name):
                for module, level, names in imported_modules(path):
                    self.assertNotIn("betfair", module)
                    self.assertNotIn("betfair", names)

    def test_betfair_init_has_no_imports(self):
        init = tree(BETFAIR / "__init__.py")
        self.assertEqual([n for n in ast.walk(init) if isinstance(n, (ast.Import, ast.ImportFrom))], [])
        self.assertEqual(len(init.body), 1)                 # the docstring only
        self.assertIsInstance(init.body[0].value, ast.Constant)

    def test_main_guarded(self):
        main = tree(BETFAIR / "__main__.py")
        body = [node for node in main.body if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant))]
        self.assertEqual(len(body), 2)
        self.assertIsInstance(body[0], ast.ImportFrom)
        self.assertEqual((body[0].module, body[0].level, [a.name for a in body[0].names]), ("cli", 1, ["main"]))
        guard = body[1]
        self.assertIsInstance(guard, ast.If)
        self.assertEqual(ast.unparse(guard.test), "__name__ == '__main__'")
        self.assertEqual(ast.unparse(guard.body[0]), "raise SystemExit(main())")

    def test_readiness_is_a_separate_guarded_entry_point(self):
        """Readiness runs only through its own module; the trading CLI has no readiness import or subcommand."""
        readiness = tree(BETFAIR / "readiness.py")
        guard = readiness.body[-1]
        self.assertIsInstance(guard, ast.If)
        self.assertEqual(ast.unparse(guard.test), "__name__ == '__main__'")
        self.assertEqual([ast.unparse(node) for node in guard.body], ["raise SystemExit(main())"])
        self.assertEqual(guard.orelse, [])
        self.assertIn("main", [node.name for node in readiness.body if isinstance(node, ast.FunctionDef)])
        for name in ("cli.py", "__main__.py"):
            path = BETFAIR / name
            with self.subTest(file=name):
                self.assertFalse(imports_betfair_module(path, "readiness"))
                self.assertFalse(any("readiness" in value for value in strings(path)))

    def test_readiness_imports_and_model_uses_are_only_the_reviewed_preflight_surface(self):
        """Pin the read-only helpers; importing the runner never grants readiness its run, ledger or lock methods.
        The models are used only through pure from_contract construction, never to evaluate or draw."""
        path = BETFAIR / "readiness.py"
        imports = {(module, level, tuple(names)) for module, level, names in imported_modules(path)}
        self.assertEqual(imports, {
            ("argparse", 0, ("argparse",)), ("dataclasses", 0, ("asdict",)), ("hashlib", 0, ("hashlib",)),
            ("pathlib", 0, ("Path",)), ("re", 0, ("re",)),
            ("backtest", 1, ("certificate", "runner")), ("backtest.accounting", 1, ("AccountingRules",)),
            ("backtest.archive", 1, ("DEFAULT_LIMITS", "MARKET_ID_RE", "ArchiveProblem", "check_data_root",
                                    "gate_snapshot", "month_gate", "safe_path")),
            ("backtest.cohort", 1, ("ANCHOR_NOT_KNOWN", "ELIGIBLE", "NO_VIEW", "CohortRules", "classify")),
            ("backtest.contract", 1, ("ContractFileError", "canonical_json", "load_contract", "result_status",
                                     "validate_contract")),
            ("backtest.engineering", 1, ("PREFIX_LIMIT_BYTES",)),
            ("backtest.market_index", 1, ("HEAD_FILE", "MARKETS_FILE", "IndexProblem", "archive_shas")),
            ("backtest.metrics", 1, ("BootstrapPlan",)), ("backtest.replay", 1, ("ReplayProblem", "_member")),
        })
        source = tree(path)
        self.assertEqual([node.asname for node in ast.walk(source) if isinstance(node, ast.alias) and node.asname], [])
        expected = {
            "certificate": {"CertificateProblem", "P0_PROFILE", "_source_file", "current_key", "profile_of",
                            "source_root"},
            "runner": {"CODE_IDENTITY_RE", "RunRefused", "SHA256_RE", "_guard", "_inputs", "_record_months",
                       "_records", "_regular_file", "_source_refusals"},
            "AccountingRules": {"from_contract"}, "BootstrapPlan": {"from_contract"},
            "CohortRules": {"from_contract"},
        }
        parents = {child: node for node in ast.walk(source) for child in ast.iter_child_nodes(node)}
        for name, attributes in expected.items():
            used = [node for node in ast.walk(source) if isinstance(node, ast.Name) and node.id == name]
            with self.subTest(object=name):
                self.assertTrue(all(isinstance(parents[node], ast.Attribute) and parents[node].value is node
                                    and isinstance(parents[node].ctx, ast.Load) for node in used))
                self.assertEqual({parents[node].attr for node in used}, attributes)
        forbidden = {"open", "write", "write_text", "write_bytes", "mkdir", "touch", "unlink", "rmdir", "rename",
                     "replace", "chmod", "symlink_to", "link_to", "acquire", "release", "run_backtest", "reconcile",
                     "replay_view", "decide_market", "decide_records", "settle", "block_bootstrap", "check_prefix",
                     "known_accounting_answers", "metrics_by_variant", "variant_metrics"}
        self.assertEqual([ast.unparse(node) for node in ast.walk(source)
                          if isinstance(node, ast.Call) and called_name(node) in forbidden], [])

    def test_no_frozen_experiment_strings(self):
        targets = code_files() + [REPO / "research" / "betfair-config.example.json"]
        for path in targets:
            text = path.read_text(encoding="utf-8")
            with self.subTest(file=str(path.relative_to(REPO))):
                self.assertNotIn("frozen_experiment", text)
                self.assertNotIn("allowed_experiments", text)
                self.assertIsNone(re.search(r"[\"']bx-", text))

    def test_paper_path_imports_no_network_module(self):
        for name in PAPER_PATH:
            path = BETFAIR / f"{name}.py"
            for module, level, names in imported_modules(path):
                with self.subTest(file=path.name, module=module):
                    first = module.split(".")[0]
                    self.assertNotIn(first, NETWORK_MODULES)
                    if level == 1 and not module:
                        self.assertFalse(set(names) & set(NETWORK_MODULES))
        # The gateway reaches the network only through the client it is handed (paper is handed none).
        for module, level, names in imported_modules(BETFAIR / "gateway.py"):
            with self.subTest(file="gateway.py", module=module):
                self.assertNotIn(module.split(".")[0], NETWORK_MODULES)

    def test_temporal_timestamp_ms_only_in_book_and_paper(self):
        for path in betfair_files():
            used = [n for n in ast.walk(tree(path)) if (isinstance(n, ast.Name) and n.id == "timestamp_ms")
                    or (isinstance(n, ast.Attribute) and n.attr == "timestamp_ms")]
            imports = [m for m, level, names in imported_modules(path) if "temporal" in m or "temporal" in names]
            with self.subTest(file=path.name):
                if path.name not in ("book.py", "paper.py"):
                    self.assertEqual(used, [])
                    self.assertEqual(imports, [])

    def test_cli_mode_defaults_to_paper(self):
        cli = tree(BETFAIR / "cli.py")
        modes = [node for node in ast.walk(cli) if isinstance(node, ast.Call) and called_name(node) == "add_argument"
                 and node.args and isinstance(node.args[0], ast.Constant) and node.args[0].value == "--mode"]
        self.assertGreaterEqual(len(modes), 3)
        for node in modes:
            keywords = {k.arg: ast.literal_eval(k.value) for k in node.keywords if k.arg in ("default", "choices")}
            self.assertEqual(keywords, {"default": "paper", "choices": ["paper", "live"]})
        # live-probe has no --mode at all: its subparser gets the order flags with live_only=True, and the
        # shared helper adds --mode only under `if not live_only`.
        functions = {n.name: n for n in ast.walk(cli) if isinstance(n, ast.FunctionDef)}
        helper = functions["_add_order_flags"]
        guarded = [n for n in ast.walk(helper) if isinstance(n, ast.If) and ast.unparse(n.test) == "not live_only"]
        self.assertEqual(len(guarded), 1)
        inside = [c for c in ast.walk(guarded[0]) if isinstance(c, ast.Call) and called_name(c) == "add_argument"]
        self.assertEqual([c.args[0].value for c in inside], ["--mode"])
        parser = functions["build_parser"]
        probe_vars = [n.targets[0].id for n in ast.walk(parser) if isinstance(n, ast.Assign)
                      and isinstance(n.value, ast.Call) and called_name(n.value) == "add" and n.value.args
                      and isinstance(n.value.args[0], ast.Constant) and n.value.args[0].value == "live-probe"]
        self.assertEqual(len(probe_vars), 1)
        flagged = [c for c in ast.walk(parser) if isinstance(c, ast.Call) and called_name(c) == "_add_order_flags"
                   and isinstance(c.args[0], ast.Name) and c.args[0].id == probe_vars[0]]
        self.assertEqual(len(flagged), 1)
        self.assertEqual({k.arg: ast.literal_eval(k.value) for k in flagged[0].keywords}, {"live_only": True})
        # And cmd_live_probe always opens its gateway in live mode.
        opened = [c for c in ast.walk(functions["cmd_live_probe"]) if isinstance(c, ast.Call)
                  and called_name(c) == "_gateway"]
        self.assertEqual(len(opened), 1)
        self.assertEqual([ast.literal_eval(k.value) for k in opened[0].keywords if k.arg == "mode"], ["live"])


if __name__ == "__main__":
    unittest.main()
