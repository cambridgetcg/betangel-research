"""Python 3.10 floor for every betangel_research/betfair module, checked by AST (no module is imported).

Execution happens on a newer Python; this only proves 3.10 syntax and the absence of names that
3.10 lacks. It is a syntax check, not a 3.10 test run. ast.parse(feature_version=(3, 10)) on a
3.12+ interpreter still accepts PEP 701 f-strings (a nested string reusing the enclosing quote, a
backslash or comment inside a replacement field, a multi-line single-quoted f-string), so those
are found with tokenize; a real 3.10 or 3.11 interpreter, when installed, also compiles every file.
"""

import ast
import io
from pathlib import Path
import shutil
import subprocess
import tokenize
import unittest

import test_betfair_support as support

PACKAGE = support.REPO_ROOT / "betangel_research" / "betfair"
TEMPORAL_ALLOWED = {"book.py", "paper.py"}


def sources():
    files = sorted(PACKAGE.glob("*.py"))
    if not files:
        raise AssertionError("no betfair modules found")
    return [(path, path.read_text(encoding="utf-8")) for path in files]


def annotations(tree):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.returns is not None:
                yield node.returns
            arguments = node.args
            for arg in arguments.posonlyargs + arguments.args + arguments.kwonlyargs:
                if arg.annotation is not None:
                    yield arg.annotation
            for arg in (arguments.vararg, arguments.kwarg):
                if arg is not None and arg.annotation is not None:
                    yield arg.annotation
        elif isinstance(node, ast.AnnAssign):
            yield node.annotation


def _quote(token_text):
    body = token_text.lstrip("rRbBfFuUtT")
    return body[:3] if body[:3] in ('"""', "'''") else body[:1]


def _clashes(inner, enclosing):
    # Before 3.12 an inner string ends the enclosing f-string if it reuses its delimiter: any
    # same-character quote inside a single-quoted f-string, or the same triple quote.
    return any(inner[0] == outer[0] and (len(outer) == 1 or len(inner) == 3) for outer in enclosing)


def pep701_violations(text):
    """(line, reason) for f-string syntax Python 3.12 accepts but 3.10 and 3.11 reject.

    On 3.12+ tokenize splits an f-string into FSTRING_START/MIDDLE/END with replacement-field
    expressions as ordinary tokens. Older interpreters tokenize the whole f-string as one STRING
    and refuse these constructs at ast.parse already, so there is nothing more to find there.
    """
    start_type = getattr(tokenize, "FSTRING_START", None)
    if start_type is None:
        return []
    end_type, middle_type = tokenize.FSTRING_END, tokenize.FSTRING_MIDDLE
    template_start = getattr(tokenize, "TSTRING_START", None)
    found = []
    stack = []
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        line = token.start[0]
        if template_start is not None and token.type == template_start:
            found.append((line, "template string (3.14+)"))
        if token.type == start_type:
            quote = _quote(token.string)
            if stack and _clashes(quote, [q for q, _ in stack]):
                found.append((line, "nested f-string reuses an enclosing quote"))
            stack.append((quote, line))
        elif token.type == end_type:
            quote, started = stack.pop()
            if len(quote) == 1 and line != started:
                found.append((line, "single-quoted f-string spans lines"))
        elif stack and token.type != middle_type:
            if token.type == tokenize.COMMENT:
                found.append((line, "comment inside an f-string replacement field"))
            elif "\\" in token.string:
                found.append((line, "backslash inside an f-string replacement field"))
            elif token.type == tokenize.STRING and _clashes(_quote(token.string), [q for q, _ in stack]):
                found.append((line, "string inside an f-string reuses an enclosing quote"))
    return found


def older_interpreters():
    """Installed Python 3.10/3.11 executables (the floor's own grammar), if any."""
    found = []
    for name in ("python3.10", "python3.11"):
        path = shutil.which(name)
        if path:
            found.append(path)
    return found


class SyntaxFloorTests(unittest.TestCase):
    def test_ast_parse_feature_version_3_10_all_betfair_files(self):
        for path, text in sources():
            with self.subTest(module=path.name):
                ast.parse(text, filename=str(path), feature_version=(3, 10))
                self.assertEqual(pep701_violations(text), [])

    def test_pep701_checker_catches_what_feature_version_misses(self):
        if getattr(tokenize, "FSTRING_START", None) is None:
            self.skipTest("this interpreter tokenizes f-strings whole; ast.parse already refuses PEP 701 forms")
        refused = [
            r'''x = f"{d["k"]}"''' + "\n",                  # same quote nested
            r'''x = f"{'\n'.join(items)}"''' + "\n",        # backslash in the expression
            'x = f"{1 + 1  # note\n}"\n',                     # comment in the expression
            'x = f"{\n1\n}"\n',                               # single-quoted f-string over lines
            r'''x = f"""{f"""{1}"""}"""''' + "\n",          # same triple quote nested
            r'''x = f"{f"{1}"}"''' + "\n",                  # nested f-string, same quote
        ]
        accepted = [
            r'''x = f"{d['k']}"''' + "\n",
            r'''x = f"""{"a"}"""''' + "\n",
            r'''x = f"{f'{1}'}"''' + "\n",
            r'''x = f"{value!r:>10}" + "plain \n"''' + "\n",
            'x = f"""\n{1}\n"""\n',
        ]
        for snippet in refused:
            with self.subTest(refused=snippet):
                ast.parse(snippet)                                    # this interpreter accepts it
                self.assertTrue(pep701_violations(snippet))
        for snippet in accepted:
            with self.subTest(accepted=snippet):
                ast.parse(snippet, feature_version=(3, 10))
                self.assertEqual(pep701_violations(snippet), [])

    def test_compile_catches_what_parse_does_not(self):
        """Regression (audit): the older interpreter only ran ast.parse, which accepts a nonlocal with no
        binding; compile() refuses it, which is what the docstring and the report promise."""
        snippet = "def f():\n    nonlocal x\n"
        ast.parse(snippet)
        with self.assertRaises(SyntaxError):
            compile(snippet, "<snippet>", "exec")

    def test_compiles_under_installed_3_10_or_3_11(self):
        interpreters = older_interpreters()
        if not interpreters:
            self.skipTest("no python3.10 or python3.11 installed")
        files = [str(path) for path, _ in sources()]
        for interpreter in interpreters:
            with self.subTest(interpreter=interpreter):
                # compile(), not just ast.parse(): symbol-table errors (a nonlocal with no binding, say)
                # are raised only when compiling. compile() writes no .pyc.
                result = subprocess.run([interpreter, "-I", "-c",
                                         "import sys\n"
                                         "for name in sys.argv[1:]:\n"
                                         "    with open(name, encoding='utf-8') as handle:\n"
                                         "        compile(handle.read(), name, 'exec')\n",
                                         *files], capture_output=True, text=True, timeout=120)
                self.assertEqual(result.returncode, 0, result.stderr[-2000:])

    def test_banned_names_absent(self):
        for path, text in sources():
            tree = ast.parse(text, filename=str(path))
            found = []
            for node in ast.walk(tree):
                if type(node).__name__ == "TryStar":
                    found.append("except*")
                if isinstance(node, ast.Import):
                    found += [alias.name for alias in node.names if alias.name.split(".")[0] == "tomllib"]
                if isinstance(node, ast.ImportFrom):
                    if node.module == "tomllib":
                        found.append("tomllib")
                    if node.module == "datetime" and any(alias.name == "UTC" for alias in node.names):
                        found.append("datetime.UTC")
                    if node.module in ("typing", "typing_extensions") and any(alias.name == "Self" for alias in node.names):
                        found.append("typing.Self")
                    if node.module == "hashlib" and any(alias.name == "file_digest" for alias in node.names):
                        found.append("file_digest")
                if isinstance(node, ast.Attribute):
                    if node.attr == "UTC" and isinstance(node.value, ast.Name) and node.value.id == "datetime":
                        found.append("datetime.UTC")
                    if node.attr == "Self" and isinstance(node.value, ast.Name) and node.value.id == "typing":
                        found.append("typing.Self")
                    if node.attr == "file_digest":
                        found.append("file_digest")
            with self.subTest(module=path.name):
                self.assertEqual(found, [])

    def test_no_lowercase_iterator_annotation(self):
        for path, text in sources():
            tree = ast.parse(text, filename=str(path))
            bad = []
            for annotation in annotations(tree):
                for node in ast.walk(annotation):
                    if isinstance(node, ast.Name) and node.id == "iterator":
                        bad.append(node.lineno)
                    if isinstance(node, ast.Constant) and isinstance(node.value, str) and "iterator" in node.value:
                        bad.append(node.lineno)
            with self.subTest(module=path.name):
                self.assertEqual(bad, [])

    def test_temporal_imported_only_in_book_and_paper(self):
        for path, text in sources():
            tree = ast.parse(text, filename=str(path))
            imports = False
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    names = {a.name for a in node.names}
                    if ((node.level == 2 and module == "temporal") or module == "betangel_research.temporal"
                            or (node.level == 2 and module == "" and "temporal" in names)
                            or (node.level == 0 and module == "betangel_research" and "temporal" in names)):
                        imports = True
                if isinstance(node, ast.Import) and any(a.name == "betangel_research.temporal" for a in node.names):
                    imports = True
            with self.subTest(module=path.name):
                if path.name not in TEMPORAL_ALLOWED:
                    self.assertFalse(imports, "temporal.timestamp_ms is only for marketTime in book.py and paper.py")


if __name__ == "__main__":
    unittest.main()
