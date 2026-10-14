"""Recursive, package-aware import boundaries for the offline backtester and the root modules, by AST only.

AGENTS.md: the backtester (betangel_research.betfair.backtest) must not import, directly or transitively, the
gateway, account/session/credential clients, network transports or trading CLI, or access account profiles or
live order state; root analysis modules must not import betfair. ImportGraph parses every .py file under a
package tree from its bytes, so a PEP 263 coding cookie is honoured exactly as the importer honours it, and
nothing is imported or executed. It collects every Import and ImportFrom anywhere in each AST, function-local
and conditional ones included. Relative imports resolve from the module's own package and level (a package
__init__ is its own package; level 2 is its parent); every module also runs its own ancestor packages, and
every import runs the parent packages of what it names. "from X import Y" imports X.Y when that module or
package exists; otherwise Y must be a name X's own file binds (any assignment, def, class, import alias or
except name), else the import is unresolved. Unresolved also covers an internal X that does not exist, a
relative import above the top package, a star import from an internal module, and any file the importer
could load that the scan cannot read (a sourceless .pyc or an extension module outside __pycache__). Every
unresolved import fails closed. The rules then walk the transitive closure inside betangel_research and
report each offending chain.

Backtest rule: from every backtest module, the closure reaches no FORBIDDEN_BETFAIR module; every module in
it imports only standard-library modules on the BACKTEST_STDLIB allowlist (a FORBIDDEN_STDLIB module is
reported with its reason, anything else as off the allowlist: third-party packages, private modules such as
_socket or _frozen_importlib, and stdlib wrappers that reach the network, processes or import machinery, such
as timeit, pickle, code, ctypes, wsgiref or concurrent.futures); no module in it uses a dynamic-import
spelling the resolver could not follow (importlib, runpy, pkgutil, zipimport, builtins, _imp, __import__, any
use of exec, eval or a bare compile, the import state __package__, __path__, __spec__ or __loader__, and
sys.modules, sys.path, sys.meta_path, sys.path_hooks or sys.path_importer_cache); and a backtest module
binds no internal package object outside the backtest package (attribute access on a package reaches any
submodule already loaded). Root rule: from every module outside betangel_research.betfair (betangel_research/
*.py and any other subpackage), the closure reaches nothing under betangel_research.betfair and imports only
the standard library (a repo directory such as scripts or tests is outside the scan, so importing one is
refused).

What this shows: no import statement in those closures names a forbidden or unlisted module, and none uses
the listed dynamic spellings. What it cannot show: imports made inside the standard library itself are not
traced (pathlib imports os; that is the library's own import, not ours), a string built at run time and
passed to getattr is not seen, nor is attribute access through a module object to modules it imported, and
a test is not a sandbox. Python does not stop a process from opening a file by path, so "never reads live
state" also rests on code review; the backtest closure must not name the state root, the client certificate
directory or the home directory (LIVE_STATE_TEXT, HOME_LOOKUPS). The runtime companion in
tests/test_betfair_backtest_contract.py lists the modules the validator actually loads on this interpreter.

Classification of every module directly in betangel_research/betfair (read in full or by its imports and
docstring at 8b0da84, 2026-10-14) is in FORBIDDEN_BETFAIR and ALLOWED_BETFAIR with one reason each. Allowed
means "not itself an account, network, order-path or live-state module"; it does not mean the module is
usable from the backtester, because its own closure may still reach a forbidden module. That is what
REUSE_FINDINGS records.

Reuse feasibility (REUSE_FINDINGS, asserted below as the current truth; nothing was modified to change it):
- Clean today: constants, ticks, pnl (with root evaluation), errors, secret, and the root stream and temporal
  modules that book uses.
- Forbidden: journal is the order journal the gateway folds at open; OrderJournal(path, mode="live") appends
  live order state (intents, resume, kill, halt), so the backtester may not import it, and paper reaches it.
- Not clean: receipts imports subprocess at module level (git_state runs git), so everything that imports
  receipts is out: schemas, filters, lifecycle, book, paper and validate. validate also imports config (reads
  the profile config.json under the state root), which imports paths (resolves the per-Mac state root, the
  live journal and the KILL file, and runs git); paper reaches config and paths through validate, and journal
  directly. Each also imports standard-library modules off the allowlist (REUSE_FINDINGS lists them).
  A function-local import does not help: the rule counts it. Task (c) therefore needs a pure split (for
  example canonical JSON and timestamp parsing in a module without git_state, and money_pence moved out of
  config) reviewed on its own, or its own pure code built on constants, ticks and pnl only; a paper journal
  would need a reviewed paper-only facade that cannot take mode="live".
"""

import ast
from collections import deque
from importlib import machinery
from pathlib import Path
import sys
import tempfile
import textwrap
import tokenize
import unittest

from test_betfair_syntax_floor import pep701_violations

REPO = Path(__file__).resolve().parent.parent
TOP = "betangel_research"
BETFAIR = f"{TOP}.betfair"
BACKTEST = f"{BETFAIR}.backtest"

# Forbidden to the backtester: account, credential, session, network, order-path or live-state modules.
FORBIDDEN_BETFAIR = {
    "__main__": "the betfair client entry point: runs cli.main",
    "api": "exchange JSON-RPC client: Betting/Accounts reads, order preparation and the single order send",
    "arming": "live arming: Keychain presence, the journal anchor, the KILL file and the account registry",
    "auth": "SSO login, keepAlive, logout and the persistent login ledger under the profile",
    "capability": "single-use order capabilities: what a live order body needs to pass the transport's gate",
    "cli": "the trading command line: opens gateways, sessions, credentials, the recorder and the kill switch",
    "config": "reads and validates the profile's STATE_ROOT/profiles/<profile>/config.json (credentials, hosts)",
    "credentials": "macOS Keychain via /usr/bin/security, certificate checks and the journal anchor",
    "esa": "Exchange Stream API client over a TLS socket",
    "gateway": "the order gateway: the single choke point between an order decision and Betfair",
    "journal": "order journal writer the gateway folds at open; mode='live' appends live order state",
    "killswitch": "engages STATE_ROOT/KILL and cancels our unmatched bets through the exchange",
    "paths": "resolves the per-Mac state root (profiles, live journal, KILL) from the password database; runs git",
    "presence": "operator presence on the controlling terminal before each live action",
    "reconcile": "reconciles live order state against the exchange through a ReadClient",
    "recorder": "live stream recorder: esa.StreamClient, the profile's recordings dir and recorder lock",
    "risk": "RiskGuard: mints order capabilities (capability._mint) and reads the KILL file (paths.kill_engaged)",
    "transport": "HTTPS and TLS-socket transports for SSO, JSON-RPC and the stream",
}
# Not themselves forbidden; each is still subject to the closure rule (see REUSE_FINDINGS).
ALLOWED_BETFAIR = {
    "__init__": "docstring only (test_betfair_boundaries.test_betfair_init_has_no_imports)",
    "book": "market book cache and recording reader over paths its caller names; no state root, no network",
    "constants": "fact constants; names hosts and RPC paths as data and imports nothing",
    "errors": "exception taxonomy; imports secret inside safe_code",
    "filters": "market filter validation and REST/stream mapping",
    "lifecycle": "order state machine and report classification",
    "paper": "paper fill model and offline replay over paths its caller names; holds no transport",
    "pnl": "own P&L and commission arithmetic in pence",
    "receipts": "clocks, timestamp parsing, canonical hashing, durable local writes; git_state runs git",
    "schemas": "exact-key record validators",
    "secret": "in-memory secret wrapper and write-time leak checks; reads no credential",
    "ticks": "CLASSIC price ladder and exact money/price parsing",
    "validate": "pre-trade rule checks and order references; imports config.money_pence",
}
# Named reasons for the worst standard-library modules; the allowlist below refuses everything else too.
FORBIDDEN_STDLIB = {
    "asyncio": "network streams and subprocesses", "ftplib": "network", "http": "network", "imaplib": "network",
    "multiprocessing": "processes and socket connections", "nntplib": "network", "poplib": "network",
    "pty": "processes", "smtplib": "network", "socket": "network", "socketserver": "network", "ssl": "network",
    "subprocess": "processes", "telnetlib": "network", "urllib": "network", "webbrowser": "processes",
    "xmlrpc": "network",
}
# The only modules (first name component) the backtest closure may import. Adding one is a reviewed change;
# os, posix, nt, pwd, ctypes, sqlite3, tempfile and urllib (urllib.parse included) are deliberately absent.
BACKTEST_STDLIB = frozenset({
    "argparse", "bisect", "bz2", "collections", "copy", "csv", "dataclasses", "datetime", "decimal", "fractions",
    "functools", "gzip", "hashlib", "heapq", "itertools", "json", "math", "numbers", "operator", "pathlib", "re",
    "statistics", "sys", "typing",
})
# Non-stdlib packages root modules may import (none today): the root rule cannot trace into them.
ROOT_THIRD_PARTY = frozenset()
DYNAMIC_MODULES = ("_frozen_importlib", "_frozen_importlib_external", "_imp", "builtins", "importlib", "pkgutil",
                   "runpy", "zipimport")
# Flagged as a bare name or as an attribute.
DYNAMIC_NAMES = ("__builtins__", "__import__", "__loader__", "__package__", "__path__", "__spec__", "exec_module",
                 "find_loader", "import_module", "load_module", "module_from_spec", "resolve_name", "run_module",
                 "run_path", "spec_from_file_location", "spec_from_loader")
DYNAMIC_BUILTINS = ("compile", "eval", "exec")            # any use of the bare name: re.compile is fine
SYS_IMPORT_STATE = ("meta_path", "modules", "path", "path_hooks", "path_importer_cache")
OS_MODULES = ("nt", "os", "posix")
OS_PROCESS_CALLS = ("execl", "execle", "execlp", "execlpe", "execv", "execve", "execvp", "execvpe", "fork",
                    "forkpty", "popen", "posix_spawn", "posix_spawnp", "spawnl", "spawnle", "spawnlp", "spawnlpe",
                    "spawnv", "spawnve", "spawnvp", "spawnvpe", "startfile", "system")
LIVE_STATE_TEXT = ("Application Support", "StatePaths", "account_home", "journal-live", "/usr/bin/security",
                   ".config/betangel-research", "betangel-research/betfair")
# The runner works only from explicit paths: no home-directory lookups (Path.home, expanduser, pwd, $HOME).
HOME_LOOKUPS = ("expanduser", "getpwnam", "getpwuid", "home")

REUSE = ("paper", "book", "validate", "pnl", "ticks", "schemas", "constants")
# Off-allowlist stdlib modules every receipts importer pulls in (receipts itself imports them).
RECEIPTS_STDLIB = ("fcntl", "os", "secrets", "subprocess", "time")
# module -> (forbidden betfair modules its closure reaches, stdlib modules its closure imports off the allowlist).
REUSE_FINDINGS = {
    BETFAIR: ((), ()),
    f"{BETFAIR}.book": ((), RECEIPTS_STDLIB),
    f"{BETFAIR}.constants": ((), ()),
    f"{BETFAIR}.errors": ((), ()),
    f"{BETFAIR}.filters": ((), RECEIPTS_STDLIB),
    f"{BETFAIR}.lifecycle": ((), RECEIPTS_STDLIB),
    f"{BETFAIR}.paper": (("config", "journal", "paths"), tuple(sorted(RECEIPTS_STDLIB + (
        "contextlib", "platform", "pwd", "shutil", "sqlite3")))),
    f"{BETFAIR}.pnl": ((), ()),
    f"{BETFAIR}.receipts": ((), RECEIPTS_STDLIB),
    f"{BETFAIR}.schemas": ((), RECEIPTS_STDLIB),
    f"{BETFAIR}.secret": ((), ()),
    f"{BETFAIR}.ticks": ((), ()),
    f"{BETFAIR}.validate": (("config", "paths"), tuple(sorted(RECEIPTS_STDLIB + ("contextlib", "pwd")))),
    TOP: ((), ()),
    f"{TOP}.evaluation": ((), ()),
    f"{TOP}.stream": ((), ()),
    f"{TOP}.temporal": ((), ()),
}


def parse(path):
    """The AST of one source file, decoded from its bytes as the importer decodes it (PEP 263 cookie)."""
    return ast.parse(Path(path).read_bytes(), filename=str(path))


class ImportGraph:
    """Static import graph of one top-level package below source_root; nothing is imported or executed.

    files: module name -> (path, is_package). For each module: internal targets (with the parent packages
    each import runs), external dotted names, dynamic-import sites, unresolved imports and the internal
    package objects it binds."""

    def __init__(self, source_root, top):
        self.source_root, self.top = Path(source_root), top
        self.files = {}
        for path in sorted((self.source_root / top).rglob("*.py")):
            parts = path.relative_to(self.source_root).with_suffix("").parts
            is_package = parts[-1] == "__init__"
            self.files[".".join(parts[:-1] if is_package else parts)] = (path, is_package)
        # A directory holding modules is a package even without an __init__ (a namespace package).
        self.known = {".".join(name.split(".")[:i]) for name in self.files for i in range(1, name.count(".") + 2)}
        self._bound = {}
        self.internal, self.external, self.dynamic, self.unresolved, self.packages = {}, {}, {}, {}, {}
        for name in self.files:
            self._scan(name)
        # The importer also loads sourceless .pyc files and extension modules next to the sources.
        suffixes = tuple(suffix for suffix in machinery.all_suffixes() if suffix != ".py")
        for path in sorted((self.source_root / top).rglob("*")):
            if path.name.endswith(suffixes) and "__pycache__" not in path.parts:
                package = ".".join(path.parent.relative_to(self.source_root).parts)
                self.unresolved.setdefault(package, []).append(f"{path.name}: importable file the scan cannot read")

    def _parents(self, dotted):
        parts = dotted.split(".")
        return [".".join(parts[:i]) for i in range(1, len(parts) + 1)]

    def is_package(self, dotted):
        return dotted in self.known and (dotted not in self.files or self.files[dotted][1])

    def binds(self, name):
        """Every name module name's file binds anywhere (over-approximate, so only a name it never binds fails)."""
        if name not in self._bound:
            bound = set()
            for node in ast.walk(parse(self.files[name][0])):
                if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                    bound.add(node.id)
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    bound.add(node.name)
                elif isinstance(node, (ast.Import, ast.ImportFrom)):
                    bound.update((alias.asname or alias.name).split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ExceptHandler) and node.name:
                    bound.add(node.name)
            self._bound[name] = bound
        return self._bound[name]

    def _name(self, dotted, internal, external, unresolved, line):
        if dotted.split(".")[0] != self.top:
            external.add(dotted)
        elif dotted not in self.known:
            unresolved.append(f"line {line}: {dotted} is not a module")
        else:
            internal.update(p for p in self._parents(dotted) if p in self.files)

    def _scan(self, name):
        path, is_package = self.files[name]
        tree = parse(path)
        package = name if is_package else name.rpartition(".")[0]
        internal = {p for p in self._parents(package) if p in self.files} if package else set()
        external, dynamic, unresolved, packages = set(), [], [], []
        sys_names = {alias.asname or alias.name for node in ast.walk(tree) if isinstance(node, ast.Import)
                     for alias in node.names if alias.name == "sys"}
        for node in ast.walk(tree):
            line = getattr(node, "lineno", 0)
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self._name(alias.name, internal, external, unresolved, line)
                    bound = alias.name if alias.asname else alias.name.split(".")[0]
                    if bound.split(".")[0] == self.top and self.is_package(bound):
                        packages.append(f"line {line}: {bound}")
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    parts = package.split(".") if package else []
                    keep = len(parts) - (node.level - 1)
                    if keep < 1:
                        unresolved.append(f"line {line}: relative import above {self.top}")
                        continue
                    base = ".".join(parts[:keep] + ([node.module] if node.module else []))
                else:
                    base = node.module
                self._name(base, internal, external, unresolved, line)
                is_internal = base.split(".")[0] == self.top
                for alias in node.names:
                    child = f"{base}.{alias.name}"
                    if alias.name == "*":
                        if is_internal:
                            unresolved.append(f"line {line}: star import from internal {base}")
                        elif base == "sys":
                            dynamic.append(f"line {line}: from sys import *")
                    elif child in self.known:
                        self._name(child, internal, external, unresolved, line)
                        if is_internal and self.is_package(child):
                            packages.append(f"line {line}: {child}")
                    elif is_internal and base in self.known and (base not in self.files
                                                                 or alias.name not in self.binds(base)):
                        unresolved.append(f"line {line}: {base} binds no {alias.name}")
                    elif base == "sys" and alias.name in SYS_IMPORT_STATE:
                        dynamic.append(f"line {line}: from sys import {alias.name}")
            elif isinstance(node, ast.Name) and node.id in DYNAMIC_NAMES + DYNAMIC_BUILTINS:
                dynamic.append(f"line {line}: {node.id}")
            elif isinstance(node, ast.Attribute) and node.attr in DYNAMIC_NAMES:
                dynamic.append(f"line {line}: .{node.attr}")
            elif (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in sys_names
                  and node.attr in SYS_IMPORT_STATE):
                dynamic.append(f"line {line}: sys.{node.attr}")
        dynamic += [f"imports {d}" for d in sorted(external) if d.split(".")[0] in DYNAMIC_MODULES]
        internal.discard(name)
        self.internal[name], self.external[name], self.packages[name] = internal, external, packages
        self.dynamic[name], self.unresolved[name] = dynamic, unresolved

    def closure(self, starts):
        """{module: the module it was first reached from (None for a start)}, breadth first."""
        reached = {start: None for start in starts}
        queue = deque(sorted(starts))
        while queue:
            module = queue.popleft()
            for target in sorted(self.internal[module]):
                if target not in reached:
                    reached[target] = module
                    queue.append(target)
        return reached

    @staticmethod
    def chain(reached, module):
        links = [module]
        while reached[links[-1]] is not None:
            links.append(reached[links[-1]])
        return " -> ".join(reversed(links))

    def violations(self, starts, internal_rule, external_rule):
        """[(chain, endpoint, reason)] over the closure of starts, sorted; [] when the rule holds."""
        reached = self.closure(starts)
        found = []
        for module in sorted(reached):
            chain = self.chain(reached, module)
            reason = internal_rule(module)
            if reason:
                found.append((chain, module, reason))
            for dotted in sorted(self.external[module]):
                reason = external_rule(dotted)
                if reason:
                    found.append((chain, dotted, reason))
            found += [(chain, site, "dynamic import the resolver cannot follow") for site in self.dynamic[module]]
            found += [(chain, site, "unresolved import") for site in self.unresolved[module]]
        return found

    def top_level_names(self):
        """Modules and directories beside the top package (scripts, tests, ...): outside the scan."""
        return {path.stem for path in self.source_root.iterdir()
                if path.name != self.top and (path.is_dir() or path.suffix == ".py")}


def under(module, package):
    return module == package or module.startswith(package + ".")


def backtest_rules(betfair, forbidden):
    """(internal_rule, external_rule) of the backtest rule: forbidden {leaf name: reason} plus BACKTEST_STDLIB."""

    def internal_rule(module):
        if under(module, betfair) and not under(module, f"{betfair}.backtest"):
            leaf = module[len(betfair) + 1:].split(".")[0] if module != betfair else "__init__"
            if leaf in forbidden:
                return f"forbidden betfair module: {forbidden[leaf]}"
        return None

    def external_rule(dotted):
        first = dotted.split(".")[0]
        if first in FORBIDDEN_STDLIB:
            return f"forbidden stdlib module ({FORBIDDEN_STDLIB[first]})"
        return None if first in BACKTEST_STDLIB else "not on the backtest stdlib allowlist"

    return internal_rule, external_rule


def backtest_violations(graph, *, betfair, forbidden):
    """The backtest rule from every module of graph's <betfair>.backtest package."""
    backtest = f"{betfair}.backtest"
    starts = [m for m in graph.files if under(m, backtest)]
    found = graph.violations(starts, *backtest_rules(betfair, forbidden))
    for module in sorted(starts):
        found += [(module, site, "binds an internal package object outside the backtest package")
                  for site in graph.packages[module] if not under(site.partition(": ")[2], backtest)]
    return found


def root_violations(graph, *, betfair):
    """The root rule: nothing outside <betfair> reaches <betfair>, directly or transitively, or imports a module
    the scan cannot follow (a repo directory beside the package, or anything outside the standard library)."""
    beside = graph.top_level_names()

    def external_rule(dotted):
        first = dotted.split(".")[0]
        if first in beside:
            return "a repo directory or module outside the scanned package"
        if first not in sys.stdlib_module_names and first not in ROOT_THIRD_PARTY:
            return "not standard library: the root rule cannot trace it"
        return None

    starts = [m for m in graph.files if not under(m, betfair)]
    return graph.violations(starts, lambda m: "betfair module" if under(m, betfair) else None, external_rule)


def describe(found):
    return "\n".join(f"{chain} => {endpoint}: {reason}" for chain, endpoint, reason in found)


REAL = ImportGraph(REPO, TOP)


def write_tree(root, files):
    for relative, text in files.items():
        path = Path(root) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(text, bytes):
            path.write_bytes(text)
        else:
            path.write_text(textwrap.dedent(text), encoding="utf-8")


SYNTHETIC_BASE = {
    "pkg/__init__.py": '"""Synthetic top package."""\n',
    "pkg/other.py": "import json\n",
    "pkg/betfair/__init__.py": '"""Synthetic betfair package."""\n',
    "pkg/betfair/gateway.py": "import socket\n",
    "pkg/betfair/api.py": "from . import gateway\n",
    "pkg/betfair/helper.py": "import math\nx = 1\n",
    "pkg/betfair/hop1.py": "from . import hop2\n",
    "pkg/betfair/hop2.py": "from ..betfair import helper\n",
    "pkg/betfair/backtest/__init__.py": '"""Synthetic backtest package."""\n',
    "pkg/betfair/backtest/util.py": "import json\n",
    "pkg/betfair/backtest/run.py": "from . import util\n",
}
SYNTHETIC_FORBIDDEN = {"gateway": "synthetic gateway", "api": "synthetic api"}


class ResolverSelfTests(unittest.TestCase):
    """The checker on synthetic packages: each forbidden shape must fail, each clean twin must pass."""

    def graph(self, overrides):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        write_tree(tmp.name, {**SYNTHETIC_BASE, **overrides})
        return ImportGraph(tmp.name, "pkg")

    def backtest(self, run_text, **extra):
        files = {"pkg/betfair/backtest/run.py": run_text}
        files.update({f"pkg/{name.replace('__', '/')}.py": text for name, text in extra.items()})
        return backtest_violations(self.graph(files), betfair="pkg.betfair", forbidden=SYNTHETIC_FORBIDDEN)

    def assert_endpoint(self, found, endpoint):
        self.assertIn(endpoint, [e for _, e, _ in found], describe(found))

    def test_clean_synthetic_backtest_passes(self):
        clean = {
            "relative sibling": "from . import util\n",
            "parent allowed module": "from .. import helper\n",
            "level three": "from ...betfair import helper\n",
            "function-local allowed": "def f():\n    from ..helper import x\n    return x\n",
            "allowed hop chain": "from .. import hop1\n",
            "stdlib": "import json, math\nfrom collections import deque\n",
            "attribute compile": "import re\nPATTERN = re.compile('x')\n",
            "root module": "from ... import other\n",
            "sys argv and os.path-like attributes": "import sys\nARGS = sys.argv\nP = ARGS.path\n",
            "allowlisted pathlib": "from pathlib import Path\nP = Path('runs') / 'x'\n",
        }
        for label, text in clean.items():
            with self.subTest(case=label):
                self.assertEqual(self.backtest(text), [])

    def test_forbidden_shapes_fail(self):
        cases = {
            "function-local import": ("def load():\n    from ..gateway import send\n    return send\n",
                                      "pkg.betfair.gateway"),
            "conditional import": ("import sys\nif sys.argv:\n    import pkg.betfair.api\n", "pkg.betfair.api"),
            "from . import gateway, one hop": ("from .. import bridge\n", "pkg.betfair.gateway"),
            "from ...betfair import api": ("from ...betfair import api\n", "pkg.betfair.api"),
            "absolute import": ("import pkg.betfair.gateway\n", "pkg.betfair.gateway"),
            "absolute from import": ("from pkg.betfair import api\n", "pkg.betfair.api"),
            "transitive hop through allowed modules": ("from .. import hop1\n", "pkg.betfair.api"),
            "transitive stdlib hop": ("from .. import helper\n", "subprocess"),
            "direct stdlib network": ("import http.client\n", "http.client"),
            "third party in a hop": ("from ... import other\n", "boto3"),
            "private stdlib": ("import _socket\n", "_socket"),
            "process pool": ("from concurrent.futures import ProcessPoolExecutor\n", "concurrent.futures"),
            "timeit runs source": ("import timeit\n", "timeit"),
            "pickle imports by name": ("import pickle\n", "pickle"),
            "ctypes": ("import ctypes\n", "ctypes"),
            "os": ("import os\n", "os"),
            "urllib.parse": ("from urllib.parse import urlsplit\n", "urllib.parse"),
            "importlib.import_module": ("import importlib\nm = importlib.import_module('pkg.betfair.gateway')\n",
                                        "line 2: .import_module"),
            "from importlib import": ("from importlib import import_module\n", "imports importlib"),
            "importlib.util": ("import importlib.util\n", "imports importlib.util"),
            "frozen importlib": ("import _frozen_importlib as b\n", "imports _frozen_importlib"),
            "runpy": ("import runpy\nrunpy.run_module('pkg.betfair.gateway')\n", "imports runpy"),
            "__import__": ("m = __import__('pkg.betfair.gateway')\n", "line 1: __import__"),
            "exec": ("exec('import socket')\n", "line 1: exec"),
            "exec alias": ("run = exec\nrun('import socket')\n", "line 1: exec"),
            "__package__": ("__package__ = 'pkg.betfair'\nfrom . import gateway\n", "line 1: __package__"),
            "__path__": ("import json\n__path__ = ['x']\n", "line 2: __path__"),
            "sys.modules": ("import sys\nB = sys.modules['builtins']\n", "line 2: sys.modules"),
            "sys alias path": ("import sys as s\ns.path.insert(0, 'x')\n", "line 2: sys.path"),
            "from sys import modules": ("from sys import modules\n", "line 1: from sys import modules"),
            "relative import above top": ("from .... import x\n", "line 1: relative import above pkg"),
            "unresolved internal": ("import pkg.betfair.missing\n", "line 1: pkg.betfair.missing is not a module"),
            "name the package does not bind": ("from . import gateway\n",
                                               "line 1: pkg.betfair.backtest binds no gateway"),
            "star import from internal": ("from .. import *\n", "line 1: star import from internal pkg.betfair"),
            "binds the betfair package object": ("from ... import betfair\n", "line 1: pkg.betfair"),
            "binds the top package object": ("from pkg.betfair import helper\nimport pkg.other\n", "line 2: pkg"),
        }
        extras = {
            "from . import gateway, one hop": {"betfair__bridge": "from . import gateway\n"},
            "transitive hop through allowed modules": {"betfair__hop2": "from ..betfair import api\n"},
            "transitive stdlib hop": {"betfair__helper": "import subprocess\n"},
            "third party in a hop": {"other": "def upload():\n    import boto3\n"},
        }
        for label, (text, endpoint) in cases.items():
            found = self.backtest(text, **extras.get(label, {}))
            with self.subTest(case=label):
                self.assert_endpoint(found, endpoint)

    def test_hop_ancestor_and_unreadable_shapes_fail(self):
        """A forbidden reach through an ancestor package __init__, dynamic or unresolved imports in a hop module,
        a sourceless .pyc beside the sources, a namespace subpackage, and a coding cookie that hides an import."""
        cases = {
            "ancestor __init__": ({"pkg/betfair/__init__.py": "from . import gateway\n",
                                   "pkg/betfair/backtest/run.py": "import json\n"}, "pkg.betfair.gateway"),
            "dynamic in hop": ({"pkg/betfair/backtest/run.py": "from .. import helper\n",
                                "pkg/betfair/helper.py": "import importlib\n"}, "imports importlib"),
            "unresolved in hop": ({"pkg/betfair/backtest/run.py": "from .. import helper\n",
                                   "pkg/betfair/helper.py": "import pkg.missing\n"},
                                  "line 1: pkg.missing is not a module"),
            "sourceless pyc": ({"pkg/betfair/backtest/evil.pyc": b"\x00",
                                "pkg/betfair/backtest/run.py": "import json\n"},
                               "evil.pyc: importable file the scan cannot read"),
            "namespace subpackage binds nothing": ({"pkg/betfair/live/orders.py": "x = 1\n",
                                                    "pkg/betfair/backtest/run.py": "from ..live import thing\n"},
                                                   "line 1: pkg.betfair.live binds no thing"),
            "utf-7 cookie": ({"pkg/betfair/backtest/run.py": "# coding: utf-7\n# note +AAo-from .. import gateway\n"},
                             "pkg.betfair.gateway"),
        }
        for label, (files, endpoint) in cases.items():
            found = backtest_violations(self.graph(files), betfair="pkg.betfair", forbidden=SYNTHETIC_FORBIDDEN)
            with self.subTest(case=label):
                self.assert_endpoint(found, endpoint)

    def test_relative_resolution_in_modules_and_package_inits(self):
        graph = self.graph({
            "pkg/betfair/bridge.py": "from . import gateway\n",
            "pkg/betfair/backtest/__init__.py": "from . import util\n",
            "pkg/lab/__init__.py": "from ..betfair import api\n",
            "pkg/lab/report.py": "from . import table\nfrom .. import other\n",
            "pkg/lab/table.py": "",
        })
        self.assertIn("pkg.betfair.gateway", graph.internal["pkg.betfair.bridge"])
        self.assertIn("pkg.betfair.backtest.util", graph.internal["pkg.betfair.backtest"])
        self.assertIn("pkg.betfair.api", graph.internal["pkg.lab"])
        self.assertTrue({"pkg.lab", "pkg.lab.table", "pkg.other"} <= graph.internal["pkg.lab.report"])
        # Every module also runs its parent packages.
        self.assertTrue({"pkg", "pkg.betfair", "pkg.betfair.backtest"} <= graph.internal["pkg.betfair.backtest.run"])
        self.assertEqual(graph.unresolved["pkg.lab.report"], [])

    def test_root_rule_on_synthetic_packages(self):
        cases = {
            "root module, relative": {"pkg/other.py": "from .betfair import helper\n"},
            "root subpackage, from ..betfair import api": {"pkg/lab/__init__.py": "",
                                                           "pkg/lab/report.py": "from ..betfair import api\n"},
            "root module, absolute, function-local": {"pkg/other.py": "def f():\n    import pkg.betfair\n"},
            "root module, transitive": {"pkg/other.py": "from .lab import report\n", "pkg/lab/__init__.py": "",
                                        "pkg/lab/report.py": "from pkg.betfair import constants\n",
                                        "pkg/betfair/constants.py": ""},
            "root module, dynamic": {"pkg/other.py": "import importlib\n"},
            "root module, repo directory": {"pkg/other.py": "import scripts.hop\n", "scripts/hop.py": "x = 1\n"},
            "root module, third party": {"pkg/other.py": "def f():\n    import boto3\n"},
            "root module, star import with __all__": {"pkg/__init__.py": "__all__ = ['betfair']\n",
                                                      "pkg/other.py": "from . import *\n"},
        }
        for label, files in cases.items():
            with self.subTest(case=label):
                self.assertNotEqual(root_violations(self.graph(files), betfair="pkg.betfair"), [])
        clean = {"pkg/other.py": "from . import lab\nimport json\n", "pkg/lab/__init__.py": "import socket\n"}
        self.assertEqual(root_violations(self.graph(clean), betfair="pkg.betfair"), [])


class BacktestBoundaryTests(unittest.TestCase):
    def closure_modules(self):
        return sorted(REAL.closure([m for m in REAL.files if under(m, BACKTEST)]))

    def test_every_betfair_module_is_classified(self):
        direct = {path.stem for path in (REPO / TOP / "betfair").glob("*.py")}
        subpackages = {m.split(".")[2] for m in REAL.known if under(m, BETFAIR) and m.count(".") >= 2}
        self.assertEqual(subpackages - direct, {"backtest"}, "a new betfair subpackage needs its own classification")
        self.assertEqual(set(FORBIDDEN_BETFAIR) & set(ALLOWED_BETFAIR), set())
        self.assertEqual(set(FORBIDDEN_BETFAIR) | set(ALLOWED_BETFAIR), direct)

    def test_forbidden_list_covers_the_agents_rule(self):
        """AGENTS.md names the gateway, account/session/credential clients, network transports and the trading CLI,
        and live order state; the brief adds kill switch, arming, presence, reconcile and profile/live-state
        readers."""
        for name in ("gateway", "api", "auth", "credentials", "transport", "esa", "cli", "__main__", "killswitch",
                     "arming", "presence", "reconcile", "paths", "config", "recorder", "capability", "risk",
                     "journal"):
            self.assertIn(name, FORBIDDEN_BETFAIR)
        self.assertEqual(BACKTEST_STDLIB & set(FORBIDDEN_STDLIB), frozenset())
        self.assertEqual(BACKTEST_STDLIB & {*OS_MODULES, *DYNAMIC_MODULES, "pwd", "ctypes", "sqlite3", "tempfile"},
                         frozenset())
        self.assertTrue(BACKTEST_STDLIB <= set(sys.stdlib_module_names))

    def test_backtest_closure_avoids_forbidden_modules(self):
        starts = [m for m in REAL.files if under(m, BACKTEST)]
        self.assertTrue({f"{BACKTEST}", f"{BACKTEST}.contract", f"{BACKTEST}.__main__"} <= set(starts))
        found = backtest_violations(REAL, betfair=BETFAIR, forbidden=FORBIDDEN_BETFAIR)
        self.assertEqual(found, [], describe(found))

    def test_backtest_imports_are_stdlib_or_relative(self):
        stdlib = set(sys.stdlib_module_names)
        for module in sorted(m for m in REAL.files if under(m, BACKTEST)):
            for node in ast.walk(parse(REAL.files[module][0])):
                with self.subTest(module=module, line=getattr(node, "lineno", 0)):
                    if isinstance(node, ast.Import):
                        self.assertTrue(all(a.name.split(".")[0] in stdlib for a in node.names))
                    elif isinstance(node, ast.ImportFrom) and not node.level:
                        self.assertIn(node.module.split(".")[0], stdlib)

    def test_backtest_closure_names_no_live_state_or_home_and_starts_no_process(self):
        """Over the whole backtest closure: no state-root, certificate-directory or Keychain text, no home-directory
        lookup, and no os/posix/nt process call under any alias (os is off the allowlist as well)."""
        for module in self.closure_modules():
            path = REAL.files[module][0]
            text = path.read_text(encoding="utf-8")
            tree = parse(path)
            aliases = {a.asname or a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names
                       if a.name in OS_MODULES}
            spawned = [ast.unparse(n) for n in ast.walk(tree) if isinstance(n, ast.Attribute)
                       and n.attr in OS_PROCESS_CALLS and isinstance(n.value, ast.Name) and n.value.id in aliases]
            spawned += [a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module in OS_MODULES
                        for a in n.names if a.name in OS_PROCESS_CALLS or a.name == "*"]
            home = [ast.unparse(n) for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr in HOME_LOOKUPS]
            home += [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and n.value == "HOME"]
            with self.subTest(module=module):
                self.assertEqual([s for s in LIVE_STATE_TEXT if s in text], [])
                self.assertEqual(spawned, [])
                self.assertEqual(home, [])

    def test_backtest_init_docstring_only_and_main_guarded(self):
        init = parse(REAL.files[BACKTEST][0])
        self.assertEqual([n for n in ast.walk(init) if isinstance(n, (ast.Import, ast.ImportFrom))], [])
        self.assertEqual(len(init.body), 1)
        self.assertIsInstance(init.body[0].value, ast.Constant)
        main = parse(REAL.files[f"{BACKTEST}.__main__"][0])
        guard = main.body[-1]
        self.assertIsInstance(guard, ast.If)
        self.assertEqual(ast.unparse(guard.test), "__name__ == '__main__'")
        self.assertEqual([ast.unparse(n) for n in guard.body], ["raise SystemExit(main())"])
        top_level_calls = [ast.unparse(n) for n in main.body[:-1] if isinstance(n, ast.Expr)
                           and not isinstance(n.value, ast.Constant)]
        self.assertEqual(top_level_calls, [])
        self.assertEqual(REAL.internal[f"{BACKTEST}.__main__"],
                         {TOP, BETFAIR, BACKTEST, f"{BACKTEST}.contract"})

    def test_backtest_syntax_floor_is_python_3_10(self):
        for module in sorted(m for m in REAL.files if under(m, BACKTEST)):
            text = REAL.files[module][0].read_text(encoding="utf-8")
            with self.subTest(module=module):
                ast.parse(text, feature_version=(3, 10))
                self.assertEqual(pep701_violations(text), [])

    def test_every_package_source_is_utf8_by_its_own_cookie(self):
        """The text scans here and in test_betfair_boundaries read sources as UTF-8; the importer decodes by the
        PEP 263 cookie (or BOM). Both agree only if every file under the package decodes as plain UTF-8."""
        paths = sorted((REPO / TOP).rglob("*.py"))
        self.assertGreater(len(paths), 40)
        for path in paths:
            with open(path, "rb") as handle, self.subTest(path=str(path.relative_to(REPO))):
                self.assertEqual(tokenize.detect_encoding(handle.readline)[0], "utf-8")

    def test_root_modules_do_not_reach_betfair_transitively(self):
        starts = sorted(m for m in REAL.files if not under(m, BETFAIR))
        self.assertTrue({TOP, f"{TOP}.__main__", f"{TOP}.motifs", f"{TOP}.stream"} <= set(starts))
        found = root_violations(REAL, betfair=BETFAIR)
        self.assertEqual(found, [], describe(found))

    def test_no_unresolved_or_dynamic_imports_anywhere_in_the_package(self):
        """Fail closed for the whole tree: every import the resolver meets names a real module or a name its
        module binds, nothing uses the dynamic spellings, and no unreadable importable file sits beside the
        sources, so the closures above are complete as far as static imports go."""
        self.assertEqual({m: s for m, s in REAL.unresolved.items() if s}, {})
        self.assertEqual({m: s for m, s in REAL.dynamic.items() if s}, {})

    def test_reuse_feasibility_is_recorded(self):
        """Which modules later backtest tasks intend to reuse, and everything they pull in, pass the backtest rule
        today. Asserts the current truth recorded in REUSE_FINDINGS (see the module docstring)."""
        reached = REAL.closure([f"{BETFAIR}.{name}" for name in REUSE])
        pulled_in = {m for m in reached if not under(m, BETFAIR) or m[len(BETFAIR) + 1:] not in FORBIDDEN_BETFAIR}
        self.assertEqual(pulled_in, set(REUSE_FINDINGS))
        for module in sorted(pulled_in):
            found = REAL.violations([module], *backtest_rules(BETFAIR, FORBIDDEN_BETFAIR))
            betfair = tuple(sorted({e[len(BETFAIR) + 1:] for _, e, _ in found if under(e, BETFAIR)}))
            stdlib = tuple(sorted({e.split(".")[0] for _, e, reason in found
                                   if reason.startswith(("forbidden stdlib", "not on the backtest stdlib"))}))
            other = [f for f in found if not under(f[1], BETFAIR)
                     and not f[2].startswith(("forbidden stdlib", "not on the backtest stdlib"))]
            with self.subTest(module=module):
                self.assertEqual((betfair, stdlib), REUSE_FINDINGS[module], describe(found))
                self.assertEqual(other, [])
        clean = sorted(m for m, findings in REUSE_FINDINGS.items() if findings == ((), ()))
        self.assertEqual(clean, [TOP, BETFAIR, f"{BETFAIR}.constants", f"{BETFAIR}.errors", f"{BETFAIR}.pnl",
                                 f"{BETFAIR}.secret", f"{BETFAIR}.ticks", f"{TOP}.evaluation", f"{TOP}.stream",
                                 f"{TOP}.temporal"])

    def test_receipts_subprocess_is_the_module_level_cause(self):
        """The single stdlib cause behind most unclean reuse modules: receipts imports subprocess at module level."""
        receipts = REAL.files[f"{BETFAIR}.receipts"][0]
        top_level = [a.name for n in parse(receipts).body if isinstance(n, ast.Import) for a in n.names]
        self.assertIn("subprocess", top_level)
        self.assertIn("subprocess", REAL.external[f"{BETFAIR}.receipts"])

    def test_journal_writes_live_order_state(self):
        """Why journal is forbidden: it takes mode='live', and the trading CLI opens the live journal with it."""
        journal = REAL.files[f"{BETFAIR}.journal"][0].read_text(encoding="utf-8")
        cli = REAL.files[f"{BETFAIR}.cli"][0].read_text(encoding="utf-8")
        self.assertIn("mode IN ('paper','live')", journal)
        self.assertIn('OrderJournal(paths.journal_path("live"), mode="live"', cli)
        self.assertIn(f"{BETFAIR}.journal", REAL.internal[f"{BETFAIR}.paper"])


if __name__ == "__main__":
    unittest.main()
