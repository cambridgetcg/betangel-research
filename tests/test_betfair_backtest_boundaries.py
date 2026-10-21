"""Recursive, package-aware import boundaries for the offline tools and the root modules, by AST only.

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
relative import above the top package, a star import from an internal module, any file the importer could
load that the scan cannot read (a sourceless .pyc or an extension module outside __pycache__), and a module
file beside a package directory of the same name (X.py and X/__init__.py: the name is read from the package,
which the importer loads first, and the twin is still refused). Every unresolved import fails closed. The
rules then walk the transitive closure inside betangel_research and report each offending chain.

Shadowing (shadow_sites, shadowing_names): an external name is trusted only if no repository file or directory
can answer it first. The places searched are the directory beside the top package (sys.path[0] under -m from
the repository root) and the directory of each module in the closure (sys.path[0] when that file runs as a
script); every subdirectory and every file with an importable suffix (.py, a sourceless .pyc, an extension
module) there counts, compared ignoring case (with PYTHONCASEOK, macOS and Windows match a module file
regardless of case; elsewhere this is only conservative). Under both rules, before any allowlist is consulted,
two things are refused: an imported external name one of them carries (Astra's SHADOW_REPRODUCTION, a
fractions.py beside the package that imports the gateway, passed the allowlist alone), and any of them that
carries a standard-library name, imported by the closure or not, because the standard library imports itself
by absolute name (STDLIB_SHADOW_REPRODUCTION: pathlib imports fnmatch). The importer may load such a file or
directory first, not always: a directory without __init__ is a namespace portion that loses to a module later
on sys.path, and builtin and frozen modules come first. Refusing every one is the conservative reading.

Backtest rule: from every backtest module, the closure reaches no FORBIDDEN_BETFAIR module; every module in it
imports only standard-library modules on the BACKTEST_STDLIB allowlist that nothing in the repository shadows
(a FORBIDDEN_STDLIB module is reported with its reason, anything else as off the allowlist: third-party
packages, private modules such as _socket or _frozen_importlib, and stdlib wrappers that reach the network,
processes or import machinery, such as timeit, pickle, code, ctypes, wsgiref or concurrent.futures); no module
in it uses a dynamic-import spelling the resolver could not follow (importlib, runpy, pkgutil, zipimport,
builtins, _imp, __import__, any use of exec, eval or a bare compile, the import state __package__, __path__,
__spec__ or __loader__, and sys.modules, sys.path, sys.meta_path, sys.path_hooks or sys.path_importer_cache);
and a backtest module binds no internal package object outside the backtest package (attribute access on a
package reaches any submodule already loaded). Root rule: from every module outside betangel_research.betfair
(betangel_research/*.py and any other subpackage), the closure reaches nothing under betangel_research.betfair
and imports only the standard library that nothing in the repository shadows (a repo directory such as
scripts or tests is outside the scan, so importing one is refused).

What this shows: no import statement in those closures names a forbidden or unlisted module, none uses the
listed dynamic spellings, and no repository file or directory in the searched places carries an imported
external name or any standard-library name, so neither our imports nor the standard library's own can load a
repository file from those places in place of the library. What it cannot show: what the standard library
imports internally is not traced (pathlib imports os; that is the library's own import, not ours), a string
built at run time and passed to getattr is not seen, nor is attribute access through a module object to
modules it imported; a shadowing file elsewhere on sys.path (another working directory, PYTHONPATH, user or
site packages) is outside the repository and not seen, and neither is a repository directory that joins
sys.path only in another launch mode (scripts/ when one of its scripts runs; tests/, sys.path[0] under
unittest discover, is checked for standard-library names only, by the real-tree test); and a test is not a
sandbox. Python does not stop a process from opening a file by path, so "never reads live state" also rests on
code review; the backtest closure must not name the state root, the client certificate directory or the home
directory (LIVE_STATE_TEXT, HOME_LOOKUPS), with one reviewed exception: path_guard, which denies those two trees,
may name them only in its exact top-level deny literal (PATH_GUARD_DENIED; live_state_checked_text). The runtime
companion in tests/test_betfair_backtest_contract.py lists the modules the validator actually loads on this
interpreter.

Readiness uses the same transitive import rule from its separate betfair.readiness entry point, including
shadowing, dynamic imports and package-object bindings. Its closure gets the same live-state/home text checks;
it does not enlarge the certificate's backtest-only source descriptor or become a trading CLI subcommand.
The pure pilot module (betfair.pilot, P1-05) is held to the same rule (pilot_violations), and its closure gets the
same text checks; tests/test_betfair_pilot.py runs them and pins its exact imports.

Classification of every module directly in betangel_research/betfair is in FORBIDDEN_BETFAIR and ALLOWED_BETFAIR
with one reason each. The original modules were read in full or by their imports and docstrings at 8b0da84,
2026-10-14; readiness was added and reviewed in R1 at 4df3399, 2026-10-16. Allowed means "not itself an account,
network, order-path or live-state module"; it does not mean the module is
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
import io
from collections import deque
from importlib import machinery
from pathlib import Path
import subprocess
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
READINESS = f"{BETFAIR}.readiness"
PILOT = f"{BETFAIR}.pilot"

# Forbidden to the backtester: account, credential, session, network, order-path or live-state modules.
FORBIDDEN_BETFAIR = {
    "__main__": "the betfair client entry point: runs cli.main",
    "admission": ("recording admission: the profile's recordings/ admission files and locks, the login ledger (auth), "
                  "a read-only fold of the live journal and SIGTERM to a recorder process"),
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
    "pilot_gate": ("the RS-19 pilot gateway adapter: runs git, reads the contract, receipt and state root, writes raw "
                   "evidence; the gateway's first-byte predicate and latch table"),
    "pilot_run": ("the RS-19 pilot executor: typed lines, the live journal, the recording admission, Keychain reads "
                  "and capture reads through the read sessions and gateways the CLI hands it"),
    "presence": "operator presence on the controlling terminal before each live action",
    "reconcile": "reconciles live order state against the exchange through a ReadClient",
    "recorder": "live stream recorder: esa.StreamClient, the profile's recordings dir and recorder lock",
    "recordings": "offline index and verify-day over the profile's recordings dir and run ledger; imports recorder",
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
    "pilot": ("pure P1 predicates, decision record, measurement classes, NCLV, ledger fold with the pause, CR1 "
              "analysis; no clock, file or network; its closure obeys the offline rule"),
    "pnl": "own P&L and commission arithmetic in pence",
    "readiness": "read-only draft and index inventory over explicit paths; its closure obeys the offline rule",
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
    # archive.py reads Betfair Historical Data TARs as a header stream ("r|"); it never calls extract or creation.
    # Package-wide here; tests/test_betfair_backtest_archive.py pins that only archive.py imports it, "r|" only.
    "tarfile",
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
# The one reviewed exception to LIVE_STATE_TEXT (Astra's c2 design agreement, D12): path_guard may name the two
# client trees it denies, only as literals in this exact top-level assignment; everything else in it is checked as
# in every other backtest module.
PATH_GUARD = f"{BACKTEST}.path_guard"
PATH_GUARD_ASSIGNMENT = "DENIED_COMPONENT_SEQUENCES"
PATH_GUARD_DENIED = (("Library", "Application Support", "betangel-research", "betfair"),
                     (".config", "betangel-research", "betfair"))


def name_bindings(tree, name):
    """Every node of tree that binds or deletes name: a Name stored or deleted (assignment, augmented or annotated
    assignment, walrus, for or with target, comprehension target), a def or class, an import alias, an argument, an
    except-as, a match capture, or a global or nonlocal declaration of it."""
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == name and isinstance(node.ctx, (ast.Store, ast.Del)):
            found.append(node)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == name:
            found.append(node)
        elif isinstance(node, ast.alias) and (node.asname or node.name.split(".")[0]) == name:
            found.append(node)
        elif isinstance(node, ast.arg) and node.arg == name:
            found.append(node)
        elif isinstance(node, ast.ExceptHandler) and node.name == name:
            found.append(node)
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name == name:
            found.append(node)
        elif isinstance(node, ast.MatchMapping) and node.rest == name:
            found.append(node)
        elif isinstance(node, (ast.Global, ast.Nonlocal)) and name in node.names:
            found.append(node)
    return found


def live_state_checked_text(module, text, tree):
    """The text of a module that LIVE_STATE_TEXT is checked against. Every module but path_guard: its whole text. For
    path_guard, its tokens (tokenize, so lines are counted as Python counts them) with the string tokens inside its
    one top-level PATH_GUARD_ASSIGNMENT left out, after checking that the assignment is a plain assignment of exactly
    PATH_GUARD_DENIED written as a tuple of tuples of string literals, that it is never assigned again, and that its
    lines hold nothing else (no other statement, before or after it); comments, inside the literal included, are
    always kept. AssertionError when any check fails."""
    if module != PATH_GUARD:
        return text
    found = [node for node in tree.body if isinstance(node, ast.Assign)
             and [ast.unparse(target) for target in node.targets] == [PATH_GUARD_ASSIGNMENT]]
    if len(found) != 1:
        raise AssertionError("path_guard: exactly one top-level DENIED_COMPONENT_SEQUENCES assignment expected")
    node, value = found[0], found[0].value

    def text_literals(item):
        return isinstance(item, ast.Tuple) and all(isinstance(part, ast.Constant) and isinstance(part.value, str)
                                                   for part in item.elts)

    if not (isinstance(value, ast.Tuple) and all(text_literals(item) for item in value.elts)
            and ast.literal_eval(value) == PATH_GUARD_DENIED):
        raise AssertionError("path_guard: DENIED_COMPONENT_SEQUENCES is not exactly the reviewed literal")
    if [binding for binding in name_bindings(tree, PATH_GUARD_ASSIGNMENT) if binding is not node.targets[0]]:
        raise AssertionError("path_guard: DENIED_COMPONENT_SEQUENCES is bound more than once")
    first, last = (node.lineno, node.col_offset), (node.end_lineno, node.end_col_offset)
    layout = (tokenize.NEWLINE, tokenize.NL, tokenize.COMMENT, tokenize.INDENT, tokenize.DEDENT, tokenize.ENDMARKER)
    strings = tuple(getattr(tokenize, name) for name in ("STRING", "FSTRING_MIDDLE", "TSTRING_MIDDLE")
                    if hasattr(tokenize, name))
    kept = []
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        inside = first <= token.start and token.end <= last
        on_its_lines = first[0] <= token.start[0] <= last[0] or first[0] <= token.end[0] <= last[0]
        if on_its_lines and not inside and token.type not in layout:
            raise AssertionError("path_guard: the deny assignment's lines hold something else")
        if not (inside and token.type in strings):
            kept.append(token.string)
    return "\n".join(kept)

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


def path_guard_reuse_problems(tree):
    """What one module's AST does with path_guard beyond guard_path, PathDenied and denied_sequence: a forbidden
    import (the module whole, or another name) or a name of the deny list or its folded form."""
    allowed, private = {"PathDenied", "denied_sequence", "guard_path"}, {PATH_GUARD_ASSIGNMENT, "_DENIED", "_folded"}
    problems = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            from_guard = isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[-1] == "path_guard"
            for alias in node.names:
                if alias.name.split(".")[-1] == "path_guard" or (from_guard and alias.name not in allowed):
                    problems.append(f"import:{alias.name}")
        elif isinstance(node, ast.Name) and node.id in private:
            problems.append(f"name:{node.id}")
        elif isinstance(node, ast.Attribute) and node.attr in private:
            problems.append(f"name:{node.attr}")
    return sorted(problems)


class ImportGraph:
    """Static import graph of one top-level package below source_root; nothing is imported or executed.

    files: module name -> (path, is_package). For each module: internal targets (with the parent packages
    each import runs), external dotted names, dynamic-import sites, unresolved imports and the internal
    package objects it binds."""

    def __init__(self, source_root, top):
        self.source_root, self.top = Path(source_root), top
        self.files, twins = {}, []
        for path in sorted((self.source_root / top).rglob("*.py")):
            parts = path.relative_to(self.source_root).with_suffix("").parts
            is_package = parts[-1] == "__init__"
            name = ".".join(parts[:-1] if is_package else parts)
            if name in self.files:
                twins.append(name)
            # X/__init__.py and X.py share a name; the importer finds the package directory first.
            if is_package or name not in self.files:
                self.files[name] = (path, is_package)
        # A directory holding modules is a package even without an __init__ (a namespace package).
        self.known = {".".join(name.split(".")[:i]) for name in self.files for i in range(1, name.count(".") + 2)}
        self._bound = {}
        self.internal, self.external, self.dynamic, self.unresolved, self.packages = {}, {}, {}, {}, {}
        for name in self.files:
            self._scan(name)
        for name in twins:
            leaf = name.rpartition(".")[2]
            self.unresolved[name].append(f"{leaf}.py and {leaf}/__init__.py share the name; the importer loads the "
                                         "package")
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

    @staticmethod
    def importable_names(directory):
        """Every name a module search of directory can answer: each subdirectory (a package, or a namespace
        portion) and each file with an importable suffix (source, sourceless .pyc, extension module)."""
        suffixes = sorted(machinery.all_suffixes(), key=len, reverse=True)
        names = set()
        for path in Path(directory).iterdir():
            if path.is_dir():
                names.add(path.name)
                continue
            suffix = next((s for s in suffixes if path.name.endswith(s) and len(path.name) > len(s)), None)
            if suffix:
                names.add(path.name[:-len(suffix)])
        return names

    def top_level_names(self):
        """Modules, compiled modules and directories beside the top package (scripts, tests, ...): outside the
        scan, and first on sys.path when a module of the package runs with -m from the repository root."""
        return self.importable_names(self.source_root) - {self.top}


def under(module, package):
    return module == package or module.startswith(package + ".")


SHADOWED = "a repository file or directory of this name can be imported in its place (outside the scan)"
STDLIB_SHADOWED = ("a repository file or directory carries a standard-library name: the standard library imports "
                   "itself by absolute name, so any of its imports can load this instead")
# Compared ignoring case: with PYTHONCASEOK the importer on macOS and Windows matches a file regardless of case.
STDLIB_FOLDED = frozenset(name.casefold() for name in sys.stdlib_module_names)


def shadow_sites(graph, starts):
    """Sorted (place, name) for every name a repository file or directory can answer before the standard library or
    any installed package, for the closure of starts: everything beside the top package (place "./", sys.path[0]
    under -m from the repository root) and everything in the directory of each module in the closure (place
    "<relative directory>/", sys.path[0] when that file runs as a script)."""
    directories = {graph.source_root} | {graph.files[module][0].parent for module in graph.closure(starts)}
    sites = set()
    for directory in directories:
        place = directory.relative_to(graph.source_root).as_posix() + "/"
        names = graph.top_level_names() if directory == graph.source_root else graph.importable_names(directory)
        sites.update((place, name) for name in names)
    return sorted(sites)


def shadowing_names(graph, starts):
    """The names of shadow_sites. An external import whose first component is one of these, ignoring case, is
    refused before any allowlist accepts it."""
    return {name for _, name in shadow_sites(graph, starts)}


def stdlib_shadow_violations(graph, starts):
    """[(place, name, STDLIB_SHADOWED)] for every shadow site carrying a standard-library name, ignoring case,
    whether or not the closure imports it: the standard library's own imports (pathlib imports fnmatch) are not
    traced, so a repository file with such a name is refused wherever the importer could reach it first."""
    return [(place, name, STDLIB_SHADOWED) for place, name in shadow_sites(graph, starts)
            if name.casefold() in STDLIB_FOLDED]


def backtest_rules(betfair, forbidden, shadows):
    """(internal_rule, external_rule) of the backtest rule: forbidden {leaf name: reason}, then shadows (names a
    repository file answers, compared ignoring case and refused first), then BACKTEST_STDLIB."""
    folded = {name.casefold() for name in shadows}

    def internal_rule(module):
        if under(module, betfair) and not under(module, f"{betfair}.backtest"):
            leaf = module[len(betfair) + 1:].split(".")[0] if module != betfair else "__init__"
            if leaf in forbidden:
                return f"forbidden betfair module: {forbidden[leaf]}"
        return None

    def external_rule(dotted):
        first = dotted.split(".")[0]
        if first.casefold() in folded:
            return SHADOWED
        if first in FORBIDDEN_STDLIB:
            return f"forbidden stdlib module ({FORBIDDEN_STDLIB[first]})"
        return None if first in BACKTEST_STDLIB else "not on the backtest stdlib allowlist"

    return internal_rule, external_rule


def backtest_violations(graph, *, betfair, forbidden):
    """The backtest rule from every module of graph's <betfair>.backtest package, then every standard-library name
    a repository file or directory carries in the searched places."""
    backtest = f"{betfair}.backtest"
    starts = [m for m in graph.files if under(m, backtest)]
    found = graph.violations(starts, *backtest_rules(betfair, forbidden, shadowing_names(graph, starts)))
    for module in sorted(starts):
        found += [(module, site, "binds an internal package object outside the backtest package")
                  for site in graph.packages[module] if not under(site.partition(": ")[2], backtest)]
    return found + stdlib_shadow_violations(graph, starts)


def readiness_violations(graph, *, betfair, forbidden):
    """Apply the unchanged backtest import rules to readiness and every module it transitively reaches."""
    starts = [f"{betfair}.readiness"]
    backtest = f"{betfair}.backtest"
    found = graph.violations(starts, *backtest_rules(betfair, forbidden, shadowing_names(graph, starts)))
    for module in sorted(graph.closure(starts)):
        found += [(module, site, "binds an internal package object outside the backtest package")
                  for site in graph.packages[module] if not under(site.partition(": ")[2], backtest)]
    return found + stdlib_shadow_violations(graph, starts)


def pilot_violations(graph, *, betfair, forbidden):
    """Apply the unchanged backtest import rules to the pure pilot module and every module it transitively reaches."""
    starts = [f"{betfair}.pilot"]
    backtest = f"{betfair}.backtest"
    found = graph.violations(starts, *backtest_rules(betfair, forbidden, shadowing_names(graph, starts)))
    for module in sorted(graph.closure(starts)):
        found += [(module, site, "binds an internal package object outside the backtest package")
                  for site in graph.packages[module] if not under(site.partition(": ")[2], backtest)]
    return found + stdlib_shadow_violations(graph, starts)


def root_violations(graph, *, betfair):
    """The root rule: nothing outside <betfair> reaches <betfair>, directly or transitively, or imports a module
    the scan cannot follow (a repository file or directory that answers the name, beside the package or beside a
    module in the closure, or anything outside the standard library); and no repository file or directory in
    those places carries a standard-library name."""
    starts = [m for m in graph.files if not under(m, betfair)]
    folded = {name.casefold() for name in shadowing_names(graph, starts)}

    def external_rule(dotted):
        first = dotted.split(".")[0]
        if first.casefold() in folded:
            return SHADOWED
        if first not in sys.stdlib_module_names and first not in ROOT_THIRD_PARTY:
            return "not standard library: the root rule cannot trace it"
        return None

    found = graph.violations(starts, lambda m: "betfair module" if under(m, betfair) else None, external_rule)
    return found + stdlib_shadow_violations(graph, starts)


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
# Astra's R4 reproduction (2026-10-14), synthetic only: a fractions.py beside the package that imports the gateway.
# The stdlib-allowlist check alone accepted "import fractions", while the importer loads the sibling first.
SHADOW_REPRODUCTION = {
    "pkg/__init__.py": "",
    "pkg/betfair/__init__.py": "",
    "pkg/betfair/gateway.py": 'MARKER = "synthetic-only"\n',
    "pkg/betfair/backtest/__init__.py": "",
    "pkg/betfair/backtest/run.py": "import fractions\n",
    "fractions.py": "from pkg.betfair import gateway\n",
}
SHADOW_PROBE = ("import sys\nsys.path.insert(0, sys.argv[1])\nimport pkg.betfair.backtest.run\n"
                "sys.stdout.write(str('pkg.betfair.gateway' in sys.modules))\n")
# The same shape through the standard library's own import (internal review of R4, 2026-10-14), synthetic only:
# run.py imports only the allowlisted pathlib, pathlib imports fnmatch (3.10 to 3.14), and the importer loads a
# fnmatch.py beside the package first. Nothing in the closure imports fnmatch, so the name check alone missed it.
STDLIB_SHADOW_REPRODUCTION = {
    **{path: text for path, text in SHADOW_REPRODUCTION.items() if path != "fractions.py"},
    "pkg/betfair/backtest/run.py": "from pathlib import Path\n",
    "fnmatch.py": "from pkg.betfair import gateway\n",
}


# One standard-library module that pathlib imports internally on the running interpreter and that a source file can
# shadow: loaded by SourceFileLoader (frozen, built-in and extension modules cannot be shadowed by a .py), preferring
# leaf modules (3.10 to 3.14 give fnmatch; 3.15 under -I -S gives only operator).
STDLIB_SHADOW_PICK = ("import sys\nbefore = set(sys.modules)\nimport pathlib\n"
                      "names = sorted(n for n in set(sys.modules) - before if '.' not in n and n != 'pathlib'\n"
                      "               and type(getattr(sys.modules[n].__spec__, 'loader', None)).__name__\n"
                      "               == 'SourceFileLoader')\n"
                      "preferred = [n for n in ('fnmatch', 'glob', 'reprlib', 'keyword', 'operator') if n in names]\n"
                      "sys.stdout.write((preferred or names or [''])[0])\n")
# Like SHADOW_PROBE, but a shadow that breaks pathlib still counts: the question is only whether it was loaded.
STDLIB_SHADOW_PROBE = ("import sys\nsys.path.insert(0, sys.argv[1])\n"
                       "try:\n    import pkg.betfair.backtest.run\nexcept Exception:\n    pass\n"
                       "sys.stdout.write(str('pkg.betfair.gateway' in sys.modules))\n")


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

    def test_clean_synthetic_readiness_passes_the_offline_rule(self):
        graph = self.graph({"pkg/betfair/readiness.py": "from .backtest import run\nfrom pathlib import Path\n"})
        self.assertEqual(readiness_violations(graph, betfair="pkg.betfair", forbidden=SYNTHETIC_FORBIDDEN), [])

    def test_readiness_mutations_fail_directly_and_transitively(self):
        cases = {
            "os in readiness": ({"pkg/betfair/readiness.py": "import os\n"}, "os"),
            "api in readiness": ({"pkg/betfair/readiness.py": "from . import api\n"}, "pkg.betfair.api"),
            "forbidden hop": ({"pkg/betfair/readiness.py": "from . import helper\n",
                                "pkg/betfair/helper.py": "from . import api\n"}, "pkg.betfair.api"),
            "os in a hop": ({"pkg/betfair/readiness.py": "from . import helper\n",
                              "pkg/betfair/helper.py": "import os\n"}, "os"),
            "dynamic in a hop": ({"pkg/betfair/readiness.py": "from . import helper\n",
                                   "pkg/betfair/helper.py": "import importlib\n"}, "imports importlib"),
            "unresolved in a hop": ({"pkg/betfair/readiness.py": "from . import helper\n",
                                      "pkg/betfair/helper.py": "from .missing import x\n"},
                                     "line 1: pkg.betfair.missing is not a module"),
            "parent package binding": ({"pkg/betfair/readiness.py": "from .. import betfair\n"},
                                       "line 1: pkg.betfair"),
            "shadowed stdlib": ({"pkg/betfair/readiness.py": "import fractions\n", "fractions.py": ""},
                                 "fractions"),
        }
        for label, (files, endpoint) in cases.items():
            with self.subTest(case=label):
                graph = self.graph(files)
                found = readiness_violations(graph, betfair="pkg.betfair", forbidden=SYNTHETIC_FORBIDDEN)
                self.assert_endpoint(found, endpoint)
                self.assertTrue(any(chain.startswith("pkg.betfair.readiness") for chain, _, _ in found))
                if label in {"os in readiness", "api in readiness"}:
                    self.assertEqual(backtest_violations(graph, betfair="pkg.betfair",
                                                        forbidden=SYNTHETIC_FORBIDDEN), [])

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
        a sourceless .pyc beside the sources, a namespace subpackage, a coding cookie that hides an import, and a
        module file beside a same-named package (the importer loads the package, so the scan reads it too)."""
        twin = {"pkg/betfair/helper/__init__.py": "from .. import gateway\n",
                "pkg/betfair/backtest/run.py": "from .. import helper\n"}
        cases = {
            "module and package twin": (twin, "helper.py and helper/__init__.py share the name; the importer loads "
                                              "the package"),
            "the twin's package is what is read": (twin, "pkg.betfair.gateway"),
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

    def test_repository_names_cannot_shadow_external_modules(self):
        """R4: a repository file or directory with an external module's name is refused before any allowlist
        accepts that name. Astra's reproduction first, exactly: statically it is now a violation, and a fresh
        isolated interpreter shows the importer really loads the sibling (and with it the synthetic gateway)."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        write_tree(tmp.name, SHADOW_REPRODUCTION)
        graph = ImportGraph(tmp.name, "pkg")
        self.assertEqual(backtest_violations(graph, betfair="pkg.betfair", forbidden=SYNTHETIC_FORBIDDEN),
                         [("pkg.betfair.backtest.run", "fractions", SHADOWED), ("./", "fractions", STDLIB_SHADOWED)])
        self.assertEqual([m for m, sites in (*graph.unresolved.items(), *graph.dynamic.items()) if sites], [])
        result = subprocess.run([sys.executable, "-I", "-S", "-B", "-c", SHADOW_PROBE, tmp.name], cwd=tmp.name,
                                env={"PYTHONDONTWRITEBYTECODE": "1"}, capture_output=True, text=True, timeout=60)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "True", ""))
        # Every importable shape of the name, beside the package or beside a module in the closure.
        extension = machinery.EXTENSION_SUFFIXES[0]
        shapes = {
            "sourceless pyc beside": ({"fractions.pyc": b"\x00"}, "fractions"),
            "extension module beside": ({f"fractions{extension}": b"\x00"}, "fractions"),
            "package beside": ({"fractions/__init__.py": "from pkg.betfair import gateway\n"}, "fractions"),
            "namespace directory beside": ({"fractions/notes.txt": "x\n"}, "fractions"),
            "module beside the backtest module": ({"pkg/betfair/backtest/fractions.py": "x = 1\n"}, "fractions"),
            "module beside a hop": ({"pkg/betfair/backtest/run.py": "from .. import helper\n",
                                     "pkg/betfair/helper.py": "import math\n", "pkg/betfair/math.py": "x = 1\n"},
                                    "math"),
            "another case beside": ({"Fractions.py": "from pkg.betfair import gateway\n"}, "fractions"),
        }
        for label, (files, endpoint) in shapes.items():
            graph = self.graph({"pkg/betfair/backtest/run.py": "import fractions\n", **files})
            found = backtest_violations(graph, betfair="pkg.betfair", forbidden=SYNTHETIC_FORBIDDEN)
            with self.subTest(case=label):
                self.assertIn(endpoint, [e for _, e, reason in found if reason == SHADOWED], describe(found))
        # Clean twins: names that no import can resolve to the stdlib name.
        for label, files in {"other module beside": {"notes.py": "x = 1\n"},
                             "not importable suffix": {"fractions.txt": "x\n", "fractions.py.orig": "x\n"},
                             "nested outside every closure directory": {"docs/fractions.py": "x = 1\n"}}.items():
            graph = self.graph({"pkg/betfair/backtest/run.py": "import fractions\n", **files})
            with self.subTest(clean=label):
                self.assertEqual(backtest_violations(graph, betfair="pkg.betfair", forbidden=SYNTHETIC_FORBIDDEN), [])
        # The root rule had the same acceptance path for standard-library names; it refuses shadows the same way.
        for label, files in {"pyc beside": {"json.pyc": b"\x00"}, "module in the top package": {"pkg/json.py": ""},
                             "extension beside": {f"json{extension}": b"\x00"}}.items():
            found = root_violations(self.graph(files), betfair="pkg.betfair")
            with self.subTest(root=label):
                self.assertIn(("pkg.other", "json", SHADOWED), found, describe(found))

    def test_stdlib_internal_import_really_loads_a_shadow(self):
        """Runtime half of the R4 internal review, per interpreter: which pure-Python standard-library modules the
        allowlisted pathlib imports internally differs by version (fnmatch on 3.10 to 3.14; on 3.15 under -I -S only
        operator is source-loaded), so a hard-coded fnmatch failed on 3.15 (lab MacBook Pro, 2026-10-14). A fresh
        isolated interpreter names one such module; a file of that name beside the package is refused by the static
        rule and, in a second fresh interpreter, really is loaded (and with it the synthetic gateway), even if pathlib
        then breaks."""
        pick = subprocess.run([sys.executable, "-I", "-S", "-B", "-c", STDLIB_SHADOW_PICK],
                              env={"PYTHONDONTWRITEBYTECODE": "1"}, capture_output=True, text=True, timeout=60)
        self.assertEqual((pick.returncode, pick.stderr), (0, ""))
        name = pick.stdout.strip()
        if not name:
            self.skipTest("pathlib imports no shadowable pure-Python standard-library module on this interpreter")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tree = {**{p: text for p, text in STDLIB_SHADOW_REPRODUCTION.items() if p != "fnmatch.py"},
                f"{name}.py": "from pkg.betfair import gateway\n"}
        write_tree(tmp.name, tree)
        graph = ImportGraph(tmp.name, "pkg")
        self.assertEqual(backtest_violations(graph, betfair="pkg.betfair", forbidden=SYNTHETIC_FORBIDDEN),
                         [("./", name, STDLIB_SHADOWED)])
        result = subprocess.run([sys.executable, "-I", "-S", "-B", "-c", STDLIB_SHADOW_PROBE, tmp.name], cwd=tmp.name,
                                env={"PYTHONDONTWRITEBYTECODE": "1"}, capture_output=True, text=True, timeout=60)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "True", ""))

    def test_standard_library_names_are_refused_even_when_not_imported(self):
        """Internal review of R4: a repository file named after a module the standard library imports internally is
        loaded through the library's own import, which the scan does not trace (STDLIB_SHADOW_REPRODUCTION:
        fnmatch.py beside the package, reached through the allowlisted pathlib). Any standard-library name in a
        searched place is refused, imported or not and ignoring case. The runtime proof is in
        test_stdlib_internal_import_really_loads_a_shadow, which picks its module on the running interpreter."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        write_tree(tmp.name, STDLIB_SHADOW_REPRODUCTION)
        graph = ImportGraph(tmp.name, "pkg")
        self.assertEqual(backtest_violations(graph, betfair="pkg.betfair", forbidden=SYNTHETIC_FORBIDDEN),
                         [("./", "fnmatch", STDLIB_SHADOWED)])
        self.assertEqual(root_violations(graph, betfair="pkg.betfair"), [("./", "fnmatch", STDLIB_SHADOWED)])
        # Every place and shape, under both rules; the standard-library name need not be imported anywhere.
        shapes = {
            "module beside the package": ({"fnmatch.py": "x = 1\n"}, ("./", "fnmatch")),
            "another case beside the package": ({"FnMatch.py": "x = 1\n"}, ("./", "FnMatch")),
            "package beside the package": ({"inspect/__init__.py": ""}, ("./", "inspect")),
            "sourceless pyc beside the package": ({"random.pyc": b"\x00"}, ("./", "random")),
            "module beside the backtest module": ({"pkg/betfair/backtest/fnmatch.py": ""},
                                                  ("pkg/betfair/backtest/", "fnmatch")),
            "module in an ancestor package": ({"pkg/betfair/random.py": ""}, ("pkg/betfair/", "random")),
        }
        for label, (files, site) in shapes.items():
            graph = self.graph({"pkg/betfair/backtest/run.py": "from pathlib import Path\n", **files})
            found = backtest_violations(graph, betfair="pkg.betfair", forbidden=SYNTHETIC_FORBIDDEN)
            with self.subTest(case=label):
                self.assertIn((*site, STDLIB_SHADOWED), found, describe(found))
        found = root_violations(self.graph({"pkg/lab/__init__.py": "", "pkg/lab/fnmatch.py": "",
                                            "pkg/other.py": "from . import lab\n"}), betfair="pkg.betfair")
        self.assertEqual(found, [("pkg/lab/", "fnmatch", STDLIB_SHADOWED)])
        for label, files in {"not importable suffix": {"fnmatch.txt": "x\n", "fnmatch.py.orig": "x\n"},
                             "nested outside every searched directory": {"docs/fnmatch.py": "x = 1\n"}}.items():
            graph = self.graph({"pkg/betfair/backtest/run.py": "from pathlib import Path\n", **files})
            with self.subTest(clean=label):
                self.assertEqual(backtest_violations(graph, betfair="pkg.betfair", forbidden=SYNTHETIC_FORBIDDEN), [])
        # PYTHONCASEOK (read only without -E, so not under -I): where the platform and the file system both ignore
        # case, a Fractions.py beside the package answers "import fractions".
        tree = {**{p: t for p, t in SHADOW_REPRODUCTION.items() if p != "fractions.py"},
                "Fractions.py": "from pkg.betfair import gateway\n"}
        case_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(case_tmp.cleanup)
        write_tree(case_tmp.name, tree)
        self.assertEqual(backtest_violations(ImportGraph(case_tmp.name, "pkg"), betfair="pkg.betfair",
                                             forbidden=SYNTHETIC_FORBIDDEN),
                         [("pkg.betfair.backtest.run", "fractions", SHADOWED), ("./", "Fractions", STDLIB_SHADOWED)])
        if sys.platform.startswith(("darwin", "win", "cygwin")) and (Path(case_tmp.name) / "fractions.py").exists():
            result = subprocess.run([sys.executable, "-s", "-S", "-B", "-c", SHADOW_PROBE, case_tmp.name],
                                    cwd=case_tmp.name, env={"PYTHONCASEOK": "1", "PYTHONDONTWRITEBYTECODE": "1"},
                                    capture_output=True, text=True, timeout=60)
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "True", ""))

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
        """The offline closures (backtest, readiness, pilot); certificate coverage remains the separate backtest
        closure below."""
        return sorted(REAL.closure([m for m in REAL.files if under(m, BACKTEST)] + [READINESS, PILOT]))

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

    def test_readiness_closure_avoids_forbidden_modules(self):
        self.assertIn(READINESS, REAL.files)
        found = readiness_violations(REAL, betfair=BETFAIR, forbidden=FORBIDDEN_BETFAIR)
        self.assertEqual(found, [], describe(found))
        for entry in (f"{BETFAIR}.cli", f"{BETFAIR}.__main__", f"{BACKTEST}.__main__"):
            with self.subTest(entry=entry):
                self.assertNotIn(READINESS, REAL.closure([entry]))

    def test_backtest_imports_are_stdlib_or_relative(self):
        """Every absolute import names a standard-library module that no repository file or directory shadows."""
        starts = [m for m in REAL.files if under(m, BACKTEST)] + [READINESS]
        shadows = {name.casefold() for name in shadowing_names(REAL, starts)}
        stdlib = {name for name in sys.stdlib_module_names if name.casefold() not in shadows}
        for module in sorted(starts):
            for node in ast.walk(parse(REAL.files[module][0])):
                with self.subTest(module=module, line=getattr(node, "lineno", 0)):
                    if isinstance(node, ast.Import):
                        self.assertTrue(all(a.name.split(".")[0] in stdlib for a in node.names))
                    elif isinstance(node, ast.ImportFrom) and not node.level:
                        self.assertIn(node.module.split(".")[0], stdlib)

    def test_no_repository_name_shadows_an_allowed_or_standard_module(self):
        """Today nothing beside the package, nothing beside any module of the backtest or root closures, and
        nothing in tests/ (sys.path[0] under unittest discover, and outside both rules) carries the name of a
        standard-library module, ignoring case (BACKTEST_STDLIB included)."""
        for label, starts in (("backtest", [m for m in REAL.files if under(m, BACKTEST)]),
                              ("readiness", [READINESS]),
                              ("root", [m for m in REAL.files if not under(m, BETFAIR)])):
            with self.subTest(closure=label):
                self.assertEqual({n for n in shadowing_names(REAL, starts) if n.casefold() in STDLIB_FOLDED}, set())
                self.assertEqual(stdlib_shadow_violations(REAL, starts), [])
        self.assertEqual({n for n in ImportGraph.importable_names(REPO / "tests") if n.casefold() in STDLIB_FOLDED},
                         set())
        self.assertIn("tests", REAL.top_level_names())
        self.assertNotIn(TOP, REAL.top_level_names())

    def test_offline_closures_name_no_live_state_or_home_and_start_no_process(self):
        """Over the backtest and readiness closures: no state-root, certificate-directory or Keychain text, no
        home-directory lookup, and no os/posix/nt process call under any alias (os is off the allowlist as well)."""
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
                checked = live_state_checked_text(module, text, tree)
                self.assertEqual([s for s in LIVE_STATE_TEXT if s in checked], [])
                self.assertEqual(spawned, [])
                self.assertEqual(home, [])

    def test_the_path_guard_exception_is_exactly_one_reviewed_literal(self):
        """Only the exact top-level deny assignment is exempt: any other protected literal, a changed or extended
        assignment, a computed one or a second assignment still fails (negative cases on synthetic sources)."""
        real = REAL.files[PATH_GUARD][0].read_text(encoding="utf-8")
        checked = live_state_checked_text(PATH_GUARD, real, ast.parse(real))
        self.assertEqual([s for s in LIVE_STATE_TEXT if s in checked], [])
        self.assertIn("Application Support", real)
        self.assertEqual(live_state_checked_text(f"{BACKTEST}.replay", real, ast.parse(real)), real)
        good = ('DENIED_COMPONENT_SEQUENCES = (("Library", "Application Support", "betangel-research", "betfair"),\n'
                '                              (".config", "betangel-research", "betfair"))\n')
        cases = {
            "another literal": good + 'STATE = "Application Support"\n',
            "a docstring": '"""Reads ~/.config/betangel-research/betfair."""\n' + good,
            "a comment": good + "# Library/Application Support/betangel-research/betfair\n",
            "a comment inside the literal": good.replace("),\n", "),  # .config/betangel-research/x\n", 1),
            "a line-separator comment above": '# note\u2028\nLIVE = "journal-live"\n' + good,
            "a form-feed comment above": '# note\x0c\nLIVE = "journal-live"\n' + good,
            "an f-string elsewhere": good + 'LIVE = f"{1}journal-live"\n',
            "bytes elsewhere": good + 'LIVE = b"Application Support"\n',
        }
        for name, source in cases.items():
            with self.subTest(case=name):
                text = live_state_checked_text(PATH_GUARD, source, ast.parse(source))
                self.assertNotEqual([s for s in LIVE_STATE_TEXT if s in text], [])
        refused = {
            "extended": good.replace('"betfair"))', '"betfair"), ("Library", "Keychains"))'),
            "changed": good.replace('"betfair"),\n', '"betfair", "journal-live"),\n', 1),
            "computed": 'DENIED_COMPONENT_SEQUENCES = tuple([("Library",)])\n',
            "missing": 'DENIED = ()\n',
            "assigned twice": good + good,
            "reassigned later": good + 'DENIED_COMPONENT_SEQUENCES += (("x",),)\n',
            "annotated later": good + 'DENIED_COMPONENT_SEQUENCES: tuple = ()\n',
            "a walrus later": good + '(DENIED_COMPONENT_SEQUENCES := ())\n',
            "a for target": good + 'for DENIED_COMPONENT_SEQUENCES in [()]:\n    pass\n',
            "a with target": good + 'with open("x") as DENIED_COMPONENT_SEQUENCES:\n    pass\n',
            "a def": good + 'def DENIED_COMPONENT_SEQUENCES():\n    pass\n',
            "a class": good + 'class DENIED_COMPONENT_SEQUENCES:\n    pass\n',
            "an import alias": good + 'from pathlib import Path as DENIED_COMPONENT_SEQUENCES\n',
            "a global rebinding": good + 'def f():\n    global DENIED_COMPONENT_SEQUENCES\n    pass\n',
            "a deletion": good + 'del DENIED_COMPONENT_SEQUENCES\n',
            "a comprehension target": good + 'X = [0 for DENIED_COMPONENT_SEQUENCES in ()]\n',
            "an except name": good + 'try:\n    pass\nexcept Exception as DENIED_COMPONENT_SEQUENCES:\n    pass\n',
            "a statement after it on its line": good.rstrip("\n") + '; LIVE = "journal-live"\n',
            "a statement before it on its line": 'X = "journal-live"; ' + good,
        }
        for name, source in refused.items():
            with self.subTest(case=name), self.assertRaises(AssertionError):
                live_state_checked_text(PATH_GUARD, source, ast.parse(source))

    def test_only_path_guard_reads_its_deny_list(self):
        """Another backtest module may import only guard_path, PathDenied and denied_sequence from path_guard, and may
        not name the deny list or its folded form, so the exception cannot be borrowed to reach the trees."""
        for module in self.closure_modules():
            if module != PATH_GUARD:
                with self.subTest(module=module):
                    self.assertEqual(path_guard_reuse_problems(parse(REAL.files[module][0])), [])
        borrowed = ast.parse("from .path_guard import DENIED_COMPONENT_SEQUENCES as _TREES\n"
                             "from . import path_guard\nTREES = path_guard._DENIED\n"
                             "MORE = _folded(DENIED_COMPONENT_SEQUENCES)\n")
        self.assertEqual(path_guard_reuse_problems(borrowed),
                         ["import:DENIED_COMPONENT_SEQUENCES", "import:path_guard", "name:DENIED_COMPONENT_SEQUENCES",
                          "name:_DENIED", "name:_folded"])
        tree = parse(REAL.files[PATH_GUARD][0])
        imports = {alias.name for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))
                   for alias in node.names}
        imports |= {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        self.assertEqual(imports, {"Path", "pathlib"})

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
        # The archive subcommands import their three backtest modules inside a function (task (b)), the runner
        # subcommands import runner inside another (task c2b2) and certify-engine imports certify (task c2b3b2b).
        self.assertEqual(REAL.internal[f"{BACKTEST}.__main__"],
                         {TOP, BETFAIR, BACKTEST, f"{BACKTEST}.contract", f"{BACKTEST}.archive",
                          f"{BACKTEST}.coverage_audit", f"{BACKTEST}.market_index", f"{BACKTEST}.runner",
                          f"{BACKTEST}.certify"})

    def test_the_certificate_descriptor_is_the_import_closure(self):
        """certificate.source_descriptor hashes exactly the files the backtest package can load: its own modules, the
        parent package initialisers and what they reach (temporal.py), so a new internal import cannot go unhashed
        (task c2b3b2b; Astra's control B)."""
        from betangel_research.betfair.backtest import certificate
        descriptor, _ = certificate.source_descriptor(REPO)
        closure = REAL.closure([m for m in REAL.files if under(m, BACKTEST)])
        self.assertEqual({item["path"] for item in descriptor["files"]},
                         {REAL.files[m][0].relative_to(REPO).as_posix() for m in closure})
        self.assertNotIn(READINESS, closure)
        self.assertEqual(len(descriptor["files"]), 21)

    def test_offline_syntax_floor_is_python_3_10(self):
        for module in sorted([m for m in REAL.files if under(m, BACKTEST)] + [READINESS]):
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
            found = REAL.violations([module], *backtest_rules(BETFAIR, FORBIDDEN_BETFAIR,
                                                              shadowing_names(REAL, [module])))
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
