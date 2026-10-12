"""Keeps docs/betfair-facts.md consistent with research/betfair-facts.json; documentation checks, not API checks.

Nothing generates the doc: these tests re-derive every table row from the JSON, so an edit to one file
without the other fails here. They also hold the U6 docs to the evidence vocabulary of spec section 15, to
the code they describe (lifecycle diagram, risk rules, acknowledgements, PAPER_ASSUMPTIONS), and, once the
setup report leaves draft, to the modules and tests their "What is implemented" tables name.
"""

import ast
from collections import Counter
import json
import re
import unittest

from betangel_research.betfair import constants

import test_betfair_support as support

ROOT = support.REPO_ROOT
FACTS_PATH = ROOT / "research" / "betfair-facts.json"
FACTS_DOC = ROOT / "docs" / "betfair-facts.md"
INTEGRATION_DOC = ROOT / "docs" / "betfair-integration.md"
ORDER_PATH_DOC = ROOT / "docs" / "betfair-order-path.md"
REPORT_JSON = ROOT / "reports" / "betfair-setup-2026-10-10.json"
REPORT_MD = ROOT / "reports" / "betfair-setup-2026-10-10.md"
U6_DOCS = (FACTS_DOC, INTEGRATION_DOC, ORDER_PATH_DOC)
BETFAIR_PACKAGE = ROOT / "betangel_research" / "betfair"
PAPER_MODULE = BETFAIR_PACKAGE / "paper.py"
LIFECYCLE_MODULE = BETFAIR_PACKAGE / "lifecycle.py"
RISK_MODULE = BETFAIR_PACKAGE / "risk.py"
ARMING_MODULE = BETFAIR_PACKAGE / "arming.py"
DRAFT_STATUS = "draft_pending_integration_test_run"

FIELDS = ["id", "area", "value", "used_by", "evidence_type", "secondary_evidence_type", "retrieval", "sources",
          "published_or_updated", "accessed", "verification_status", "code_handling"]
# Spec section 15, verbatim. vendor_* means Bet Angel only; first_person_observation is our own live-test receipt.
SPEC_EVIDENCE_TYPES = ["official_documentation", "official_current_policy", "official_data_specification",
                       "vendor_documentation", "vendor_current_pricing", "forum_participant_opinion",
                       "self_report_unverified", "primary_research_simulation", "inferred", "unverified",
                       "first_person_observation"]
SPEC_RETRIEVALS = ["official_rest_api_storage_read", "official_page_read", "official_four_page_pdf_read",
                   "zendesk_api_read", "reader_proxy_read", "wayback_snapshot_<ts>", "man_page_read",
                   "local_python_check"]
UNSETTLED = frozenset({"conflicting", "unverified", "unknown", "inferred", "partially_unverified"})
# T11 (live-probe --replace-step) was dropped with live replace in v1, and T12 earlier: neither is a first-live test.
LIVE_TEST_IDS = frozenset(f"T{n}" for n in range(11))
WAYBACK_RETRIEVAL_RE = re.compile(r"wayback_snapshot_(\d{14})")
ROW_RE = re.compile(r"^\| (F\d{2}) \|")
FACT_ID_RE = re.compile(r"\bF\d{2}\b")
FACT_RANGE_RE = re.compile(r"\bF(\d{2})[–-]F(\d{2})\b")
AREA_HEADING_RE = re.compile(r"^### `([a-z_]+)` · ")
CELL_SPLIT_RE = re.compile(r"(?<!\\)\|")
LINK_RE = re.compile(r"\[[^\]]+\]\(([^)\s]+)\)")
CODE_RE = re.compile(r"`([^`]+)`")
COLUMNS = ["id", "value used", "evidence_type · status", "retrieval", "source", "published_or_updated", "accessed",
           "code handling"]
# Spec section 15: a paraphrase is at most one sentence. A second sentence starts after ". " and a capital.
MULTI_SENTENCE_RE = re.compile(r"(?<![A-Za-z]\.[a-z])(?<=[A-Za-z0-9)\]\"'`])[.!?](?=\s+[A-Z])")
# Values U1's JSON still writes as two sentences on 2026-10-10. New entries are refused; the list only shrinks.
KNOWN_MULTI_SENTENCE = frozenset({"F03", "F10", "F18", "F25", "F37", "F63"})
MULTI_SENTENCE_DOC_LEAD = "- **仲係兩句、等 JSON 擁有者收窄嘅 value：**"
# "Independently verified" is never claimed (spec section 15). The one approved sentence says so in the negative.
APPROVED_NEGATIONS = ("冇人獨立重做過任何結果，所以呢度冇任何事實標成「已獨立核實」。",)
INDEPENDENT_CLAIM_RE = re.compile(r"(?<![冇未唔])獨立(?:核實|驗證|覆核|確認)")
# A first_person_observation label must cite a live-test receipt: live-test:T<n>:sha256:<receipt sha256>.
LIVE_TEST_SOURCE_RE = re.compile(r"live-test:(T\d{1,2}):sha256:([0-9a-f]{64})")
ARROW_RE = re.compile(r"^\s*(\[\*\]|[A-Z_]+)\s*-->\s*(\[\*\]|[A-Z_]+)\s*(?::.*)?$")
TERMINAL_NOT_DRAWN_LEAD = "- **`TERMINAL_STATES` 但唔係 `[*]`：**"
NAMED_MODULE_RE = re.compile(r"`([a-z_]+\.py)`")
NAMED_TEST_RE = re.compile(r"`(test_betfair_[a-z0-9_]+)`")
IMPLEMENTED_SECTIONS = ((INTEGRATION_DOC, "## 11. What is implemented"), (ORDER_PATH_DOC, "## 16. What is implemented"))


def read_text(path):
    return path.read_text(encoding="utf-8")


def load_facts():
    return json.loads(read_text(FACTS_PATH))


def report_status():
    return json.loads(read_text(REPORT_JSON))["status"]


def cell_text(text):
    """How a free-text JSON value is written in a table cell: pipes escaped, angle brackets as entities."""
    return text.replace("|", "\\|").replace("<", "&lt;").replace(">", "&gt;")


def retrieval_labels(fact):
    """The retrieval labels of a fact: one string, or a list of two or more in the order of the value's parts."""
    retrieval = fact["retrieval"]
    return list(retrieval) if isinstance(retrieval, list) else [retrieval]


def retrieval_cell(fact):
    return " + ".join(f"`{label}`" for label in retrieval_labels(fact))


def evidence_cell(fact):
    text = f"`{fact['evidence_type']}`"
    if fact["secondary_evidence_type"] is not None:
        text += f" + secondary `{fact['secondary_evidence_type']}`"
    return text + f" · {fact['verification_status']}"


def split_row(line):
    cells = CELL_SPLIT_RE.split(line)
    return [cell.strip() for cell in cells[1:-1]]


def doc_rows():
    """{fact id: (area heading in force, [cells])} plus a Counter of every row id seen."""
    rows, seen, area = {}, Counter(), None
    for line in read_text(FACTS_DOC).splitlines():
        heading = AREA_HEADING_RE.match(line)
        if heading:
            area = heading.group(1)
            continue
        if line.startswith("## "):
            area = None
        match = ROW_RE.match(line)
        if match:
            seen[match.group(1)] += 1
            rows[match.group(1)] = (area, split_row(line))
    return rows, seen


def section(text, heading):
    """Body of a level-2 section, up to the next level-2 heading."""
    start = text.index(heading)
    end = text.find("\n## ", start + len(heading))
    return text[start:] if end == -1 else text[start:end]


def lead_line(text, lead):
    """The single line starting with lead, or None."""
    lines = [line for line in text.splitlines() if line.startswith(lead)]
    if len(lines) > 1:
        raise ValueError(f"more than one line starts with {lead!r}")
    return lines[0] if lines else None


def multi_sentence_facts(facts):
    return {fact["id"] for fact in facts if MULTI_SENTENCE_RE.search(fact["value"])}


def fact_ids_in(text):
    """Fact ids named in text, with ranges such as F01–F04 expanded."""
    ids = set(FACT_ID_RE.findall(text))
    for low, high in FACT_RANGE_RE.findall(text):
        ids.update(f"F{n:02d}" for n in range(int(low), int(high) + 1))
    return ids


def checklist_facts():
    """{test id: fact ids its "解決邊條" cell names} from the first-live-test checklist table."""
    out = {}
    for line in section(read_text(FACTS_DOC), "## First-live-test checklist").splitlines():
        match = re.match(r"^\| (T\d{1,2}) \|", line)
        if match:
            out[match.group(1)] = fact_ids_in(split_row(line)[-1])
    return out


def live_test_receipts(root=ROOT):
    """{(test id, receipt sha256)} listed in any committed reports/betfair-*.json first_live_tests_run."""
    receipts = set()
    for path in sorted((root / "reports").glob("betfair-*.json")):
        for entry in json.loads(read_text(path)).get("first_live_tests_run", []):
            if isinstance(entry, dict):
                receipts.add((entry.get("test_id"), entry.get("receipt_sha256")))
    return receipts


def first_person_problems(fact, checklist, receipts):
    """Why fact may not carry first_person_observation; empty when a matching live-test receipt backs it."""
    if "first_person_observation" not in (fact["evidence_type"], fact["secondary_evidence_type"]):
        return []
    cited = [m for m in (LIVE_TEST_SOURCE_RE.fullmatch(s) for s in fact["sources"]) if m]
    if not cited:
        return ["first_person_observation needs a live-test:T<n>:sha256:<receipt> source"]
    problems = []
    for match in cited:
        test_id, digest = match.groups()
        if test_id not in LIVE_TEST_IDS:
            problems.append(f"{test_id} is not a first-live test (T0-T10)")
        if fact["id"] not in checklist.get(test_id, set()):
            problems.append(f"the checklist row for {test_id} does not list {fact['id']}")
        if (test_id, digest) not in receipts:
            problems.append(f"no reports/betfair-*.json lists {test_id} with that receipt sha256")
    return problems


def mermaid_block(text, header):
    start = text.index(f"```mermaid\n{header}\n")
    return text[start + len("```mermaid\n"):text.index("\n```", start + 3)]


def diagram_arrows(block):
    """[(src, dst)] for every arrow; ValueError on any other non-blank line, labelled arrows included."""
    arrows = []
    for line in block.splitlines()[1:]:
        if not line.strip():
            continue
        match = ARROW_RE.match(line)
        if not match:
            raise ValueError(f"unparsed stateDiagram line: {line.strip()!r}")
        arrows.append(match.groups())
    return arrows


def string_literals(path):
    return {node.value for node in ast.walk(ast.parse(read_text(path)))
            if isinstance(node, ast.Constant) and isinstance(node.value, str)}


def table_rows(text, row_re):
    """{key: [cells]} for table rows matching row_re, plus a Counter of every key seen."""
    rows, seen = {}, Counter()
    for line in text.splitlines():
        match = re.match(row_re, line)
        if match:
            seen[match.group(1)] += 1
            rows[match.group(1)] = split_row(line)
    return rows, seen


def named_files():
    """[(where, name, path)] for every module and test named where the docs or the report claim delivery."""
    named = []
    for doc, heading in IMPLEMENTED_SECTIONS:
        body = section(read_text(doc), heading)
        for name in sorted(set(NAMED_MODULE_RE.findall(body))):
            named.append((f"{doc.name} {heading[3:]}", name, BETFAIR_PACKAGE / name))
        for name in sorted(set(NAMED_TEST_RE.findall(body))):
            named.append((f"{doc.name} {heading[3:]}", name, ROOT / "tests" / f"{name}.py"))
    for name in sorted(set(NAMED_TEST_RE.findall(read_text(REPORT_MD)))):
        named.append((REPORT_MD.name, name, ROOT / "tests" / f"{name}.py"))
    return named


def missing_named_files():
    """Named modules and tests that do not exist; the brief's fill script refuses a final status while any remain."""
    return [(where, name) for where, name, path in named_files() if not path.is_file()]


class FactsDocTests(unittest.TestCase):
    def setUp(self):
        support.block_network(self)

    def test_every_fact_id_in_json_appears_once_in_doc(self):
        facts = load_facts()["facts"]
        ids = [fact["id"] for fact in facts]
        self.assertEqual(len(ids), len(set(ids)), "duplicate fact id in the JSON")
        rows, seen = doc_rows()
        for fact_id in ids:
            with self.subTest(fact=fact_id):
                self.assertEqual(seen[fact_id], 1, "each fact needs exactly one table row in docs/betfair-facts.md")
        self.assertEqual(set(seen), set(ids), "the doc has a row for a fact the JSON does not define")
        # Prose anywhere in the U6 docs may cite a fact only if it exists.
        known = set(ids)
        for path in U6_DOCS:
            for cited in set(FACT_ID_RE.findall(read_text(path))):
                with self.subTest(doc=path.name, cited=cited):
                    self.assertIn(cited, known)
        # Every public constant's provenance points at a fact the doc shows.
        for name, fact_ids in constants.PROVENANCE.items():
            for fact_id in fact_ids:
                with self.subTest(constant=name, fact=fact_id):
                    self.assertIn(fact_id, rows)

    def test_fact_fields_exact(self):
        data = load_facts()
        self.assertEqual(data["fields"], FIELDS)
        rows, _ = doc_rows()
        header = f"| {' | '.join(COLUMNS)} |"
        self.assertEqual(read_text(FACTS_DOC).count(header), len({fact["area"] for fact in data["facts"]}),
                         "one table, with the exact header, per area")
        for fact in data["facts"]:
            with self.subTest(fact=fact["id"]):
                self.assertEqual(list(fact), FIELDS)
                self.assertIsInstance(fact["value"], str)
                self.assertIsInstance(fact["code_handling"], str)
                self.assertTrue(fact["sources"])
                self.assertTrue(fact["used_by"])
                self.assertEqual(fact["accessed"], "2026-10-10")
                area, cells = rows[fact["id"]]
                self.assertEqual(area, fact["area"], "row sits under the wrong area heading")
                self.assertEqual(len(cells), len(COLUMNS))
                self.assertEqual(cells[0], fact["id"])
                self.assertEqual(cells[1], cell_text(fact["value"]))
                self.assertEqual(cells[2], evidence_cell(fact))
                self.assertEqual(cells[3], retrieval_cell(fact))
                cited = LINK_RE.findall(cells[4]) + [c for c in CODE_RE.findall(cells[4]) if ":" in c]
                self.assertEqual(sorted(cited), sorted(fact["sources"]))
                self.assertEqual(len(cited), len(fact["sources"]))
                self.assertEqual(cells[5], fact["published_or_updated"] or "not stated")
                self.assertEqual(cells[6], fact["accessed"])
                self.assertEqual(cells[7], cell_text(fact["code_handling"]))
        # One sentence per paraphrase: known two-sentence values are listed in the doc, and no new ones may appear.
        multi = multi_sentence_facts(data["facts"])
        self.assertLessEqual(multi, KNOWN_MULTI_SENTENCE, "a fact value has more than one sentence (spec section 15)")
        line = lead_line(read_text(FACTS_DOC), MULTI_SENTENCE_DOC_LEAD)
        listed = set(FACT_ID_RE.findall(line)) if line else set()
        self.assertEqual(listed, multi, "docs/betfair-facts.md must list exactly the two-sentence values")

    def test_multi_sentence_detector(self):
        for text in ("customerRef is at most 32 characters from letters, digits and : - . _ + * ; ~ and de-duplicates.",
                     "Prices use the CLASSIC ladder, e.g. 1.01 to 1000; sizes have 2 dp.",
                     "Minimum stake is GBP 1.00 (F38). "):
            with self.subTest(text=text[:30]):
                self.assertIsNone(MULTI_SENTENCE_RE.search(text))
        for text in ("Logins are limited. Whether failures count is unclear.", "It returned 403. The archive was read."):
            with self.subTest(text=text[:30]):
                self.assertIsNotNone(MULTI_SENTENCE_RE.search(text))

    def test_evidence_type_vocabulary(self):
        data = load_facts()
        self.assertEqual(data["vocabulary"]["evidence_type"], SPEC_EVIDENCE_TYPES)
        self.assertEqual(data["vocabulary"]["retrieval"], SPEC_RETRIEVALS)
        checklist, receipts = checklist_facts(), live_test_receipts()
        for fact in data["facts"]:
            with self.subTest(fact=fact["id"]):
                self.assertIn(fact["evidence_type"], SPEC_EVIDENCE_TYPES)
                if fact["secondary_evidence_type"] is not None:
                    self.assertIn(fact["secondary_evidence_type"], SPEC_EVIDENCE_TYPES)
                    self.assertNotEqual(fact["secondary_evidence_type"], fact["evidence_type"])
                labels = retrieval_labels(fact)
                if isinstance(fact["retrieval"], list):
                    # A list only when the parts were obtained differently: two or more distinct labels.
                    self.assertGreaterEqual(len(labels), 2)
                    self.assertEqual(len(set(labels)), len(labels))
                for retrieval in labels:
                    self.assertIsInstance(retrieval, str)
                    wayback = WAYBACK_RETRIEVAL_RE.fullmatch(retrieval)
                    if wayback:
                        prefix = f"https://web.archive.org/web/{wayback.group(1)}/"
                        self.assertTrue(any(s.startswith(prefix) for s in fact["sources"]),
                                        "a wayback retrieval label must cite that snapshot")
                    else:
                        self.assertIn(retrieval, SPEC_RETRIEVALS)
                        self.assertNotEqual(retrieval, "wayback_snapshot_<ts>")
                # Regression R3#11(3) (review): a local check is never hidden behind a documentation label. A fact
                # citing a local: source lists local_python_check, and when its primary label is not already the
                # local check's (inferred · local_check, as F69) it carries a secondary label for that part.
                if any(source.startswith("local:") for source in fact["sources"]):
                    self.assertIn("local_python_check", labels)
                    if fact["verification_status"] != "local_check":
                        self.assertIsNotNone(fact["secondary_evidence_type"])
                # Our own observation only with a live-test receipt that a committed report lists.
                self.assertEqual(first_person_problems(fact, checklist, receipts), [])
                # "Vendor" means Bet Angel only (spec section 15); Betfair pages are official_*.
                if fact["evidence_type"].startswith("vendor_"):
                    self.assertTrue(any("betangel" in s.lower() for s in fact["sources"]))
                if fact["evidence_type"] in ("unverified", "inferred"):
                    self.assertIn(fact["verification_status"], UNSETTLED | {"local_check"})
        labels = section(read_text(FACTS_DOC), "## Evidence labels")
        for value in SPEC_EVIDENCE_TYPES + SPEC_RETRIEVALS + data["vocabulary"]["verification_status"]:
            with self.subTest(label=value):
                self.assertIn(f"`{value}`", labels)
        for path in U6_DOCS:
            with self.subTest(doc=path.name):
                text = read_text(path)
                # Never claimed unless someone independent reproduces a result (spec section 15), in either language.
                self.assertNotIn("independently verified", text.lower())
                for sentence in APPROVED_NEGATIONS:
                    text = text.replace(sentence, "")
                self.assertIsNone(INDEPENDENT_CLAIM_RE.search(text), "an independent-verification claim")
                self.assertIn("Prepared **2026-10-10**.", text.splitlines()[2])

    def test_first_person_observation_needs_listed_receipt(self):
        digest = "a" * 64
        fact = {"id": "F67", "evidence_type": "first_person_observation",
                "secondary_evidence_type": "inferred", "sources": ["https://example.invalid/page"]}
        checklist = checklist_facts()
        self.assertIn("F67", checklist["T3"], "the checklist maps T3 to F67")
        self.assertTrue(first_person_problems(fact, checklist, set()), "no receipt cited")
        fact["sources"] = fact["sources"] + [f"live-test:T3:sha256:{digest}"]
        self.assertTrue(first_person_problems(fact, checklist, set()), "receipt not listed in any report")
        self.assertTrue(first_person_problems(fact, checklist, {("T4", digest)}), "listed under another test")
        self.assertEqual(first_person_problems(fact, checklist, {("T3", digest)}), [])
        fact["id"] = "F01"
        self.assertTrue(first_person_problems(fact, checklist, {("T3", digest)}), "T3 does not resolve F01")

    def test_open_questions_cover_every_unsettled_fact(self):
        open_section = section(read_text(FACTS_DOC), "## Conflicts and open questions")
        listed = Counter(re.findall(r"^- \*\*(F\d{2})\*\* ", open_section, re.MULTILINE))
        expected = {f["id"] for f in load_facts()["facts"] if f["verification_status"] in UNSETTLED}
        self.assertEqual(set(listed), expected)
        self.assertTrue(all(count == 1 for count in listed.values()))
        checklist = section(read_text(FACTS_DOC), "## First-live-test checklist")
        for test_id in sorted(LIVE_TEST_IDS):
            with self.subTest(test=test_id):
                self.assertRegex(checklist, rf"(?m)^\| {test_id} \|")
        self.assertRegex(checklist, r"(?m)^\| T11 \| 已剔走")          # dropped with live replace: a row, not a test
        self.assertIn("T12", checklist)
        # Round 4: nothing names T11 as a first-live test to run any more.
        for path in (FACTS_DOC, ORDER_PATH_DOC, REPORT_MD, ROOT / ".team-work/betfair-exchange-20261010/brief.md"):
            with self.subTest(path=path.name):
                self.assertNotRegex(read_text(path), r"T0[–-]T11")
        self.assertNotRegex(load_facts()["scope"], r"T0[–-]T11")

    def test_order_path_state_diagram_matches_lifecycle(self):
        """The stateDiagram-v2 draws exactly lifecycle.TRANSITIONS; [*] differences from TERMINAL_STATES are stated."""
        text = read_text(ORDER_PATH_DOC)
        arrows = diagram_arrows(mermaid_block(text, "stateDiagram-v2"))
        drawn = [(src, dst) for src, dst in arrows if "[*]" not in (src, dst)]
        starts = {dst for src, dst in arrows if src == "[*]"}
        ends = {src for src, dst in arrows if dst == "[*]"}
        self.assertEqual(len(drawn), len(set(drawn)), "an arrow is drawn twice")
        if not LIFECYCLE_MODULE.exists():
            self.skipTest("lifecycle.py (U4) not present yet; diagram checked at integration")
        from betangel_research.betfair import lifecycle
        expected = {(src, dst) for src, dsts in lifecycle.TRANSITIONS.items() for dst in dsts}
        self.assertEqual(set(drawn), expected)
        targets = {dst for dsts in lifecycle.TRANSITIONS.values() for dst in dsts}
        self.assertEqual(starts, set(lifecycle.TRANSITIONS) - targets, "[*] --> marks the states nothing enters")
        self.assertEqual(ends, {state for state, dsts in lifecycle.TRANSITIONS.items() if not dsts})
        self.assertLessEqual(ends, set(lifecycle.TERMINAL_STATES))
        lifecycle_section = section(text, "## 5. Lifecycle state machine")
        line = lead_line(lifecycle_section, TERMINAL_NOT_DRAWN_LEAD)
        stated = set(CODE_RE.findall(line[len(TERMINAL_NOT_DRAWN_LEAD):].split("。")[0])) if line else set()
        self.assertEqual(stated, set(lifecycle.TERMINAL_STATES) - ends,
                         "every TERMINAL_STATES member without a [*] arrow is named in the section 5 bullet")
        for state in lifecycle.UNRESOLVED_STATES:
            with self.subTest(unresolved=state):
                self.assertIn(f"`{state}`", lifecycle_section)

    def test_diagram_parser_refuses_unparsed_arrows(self):
        self.assertEqual(diagram_arrows("stateDiagram-v2\n    UNKNOWN --> SUBMITTING : auto-resend\n"),
                         [("UNKNOWN", "SUBMITTING")])
        self.assertEqual(diagram_arrows("stateDiagram-v2\nA --> [*]\n"), [("A", "[*]")])
        with self.assertRaises(ValueError):
            diagram_arrows("stateDiagram-v2\n    UNKNOWN -> SUBMITTING\n")
        with self.assertRaises(ValueError):
            diagram_arrows("stateDiagram-v2\n    unknown --> submitting\n")

    def test_order_path_doc_matches_code_constants(self):
        """Tables and lists in the order-path doc equal the code they describe, row for row."""
        text = read_text(ORDER_PATH_DOC)
        arming_section = section(text, "## 2. Live arming A1–A19")
        a_rows, a_seen = table_rows(arming_section, r"^\| (A\d{1,2}) \|")
        self.assertEqual(set(a_seen), {f"A{n}" for n in range(1, 20)})
        self.assertTrue(all(count == 1 for count in a_seen.values()), "each A-code has exactly one table row")
        ack_text = arming_section[arming_section.index("**Acknowledgements。**"):arming_section.index("**Confirm token。**")]
        acks = [m.group(1) for m in re.finditer(r"^\| `([a-z_]+)` \|", ack_text, re.MULTILINE)]
        self.assertEqual(tuple(acks), constants.LIVE_ACK_IDS)
        risk_section = section(text, "## 6. Risk rules R1–R12")
        r_rows, r_seen = table_rows(risk_section, r"^\| (R\d{1,2}) \|")
        self.assertEqual(set(r_seen), {f"R{n}" for n in range(1, 13)})
        self.assertTrue(all(count == 1 for count in r_seen.values()), "each R-code has exactly one table row")
        documented_rules = [name.split(":")[0] for n in range(1, 13) for name in CODE_RE.findall(r_rows[f"R{n}"][1])]
        if RISK_MODULE.exists():
            from betangel_research.betfair import risk
            self.assertEqual(tuple(documented_rules), tuple(risk.RULE_ORDER))
        if ARMING_MODULE.exists():
            from betangel_research.betfair import arming
            self.assertEqual(tuple(acks), tuple(getattr(arming, "ACK_IDS", ())), "arming.ACK_IDS (spec 2.17)")
            markers = [c for c in CODE_RE.findall(a_rows["A10"][1]) if c.isupper()]
            self.assertEqual(tuple(markers), tuple(getattr(arming, "CLOUD_ENV_MARKERS", ())),
                             "arming.CLOUD_ENV_MARKERS (spec 2.17)")
            literals = set()
            for module in sorted(BETFAIR_PACKAGE.glob("*.py")):
                literals |= string_literals(module)
            for code in sorted(a_rows):
                for failure in CODE_RE.findall(a_rows[code][2]):
                    with self.subTest(arming=code, failure=failure):
                        self.assertIn(failure, literals, "no betfair module has this failure code")
        if not PAPER_MODULE.exists():
            self.skipTest("paper.py (U4) not present yet; PAPER_ASSUMPTIONS checked at integration")
        from betangel_research.betfair import paper
        assumptions = getattr(paper, "PAPER_ASSUMPTIONS", None)
        self.assertIsNotNone(assumptions, "paper.py defines no PAPER_ASSUMPTIONS")
        paper_section = section(text, "## 11. Paper model assumptions")
        quoted = paper_section[paper_section.index("**`PAPER_ASSUMPTIONS`"):]
        bullets = [line[2:] for line in quoted.splitlines() if line.startswith("- ")]
        self.assertEqual(tuple(bullets), tuple(assumptions),
                         "docs/betfair-order-path.md must quote PAPER_ASSUMPTIONS verbatim, in order, and nothing else")

    def test_v1_live_narrowing_is_documented(self):
        """Amendment A3 section 7: F77 is registered as an inference with no new source, the diagram draws the
        operator's EXECUTABLE -> SETTLED, the order-path doc links the verbatim amendment, and every settle-market
        refusal code the gateway raises is documented in section 8."""
        facts = {fact["id"]: fact for fact in load_facts()["facts"]}
        f77 = facts["F77"]
        self.assertEqual((f77["area"], f77["evidence_type"], f77["verification_status"]),
                         ("reconcile", "inferred", "inferred"))
        known = {source for fact_id, fact in facts.items() if fact_id != "F77" for source in fact["sources"]}
        self.assertLessEqual(set(f77["sources"]), known, "F77 claims no new source")
        text = read_text(ORDER_PATH_DOC)
        self.assertIn(("EXECUTABLE", "SETTLED"), diagram_arrows(mermaid_block(text, "stateDiagram-v2")))
        amendment = ROOT / ".team-work" / "betfair-exchange-20261010" / "amendment-v1-live-scope.md"
        self.assertTrue(amendment.is_file())
        self.assertIn("](../.team-work/betfair-exchange-20261010/amendment-v1-live-scope.md)", text)
        reconcile_section = section(text, "## 8. Reconciliation timeline")
        codes = {literal for literal in string_literals(BETFAIR_PACKAGE / "gateway.py")
                 if literal.startswith("settle_market_") and literal != "settle_market_"}
        self.assertIn("settle_market_bets_still_current", codes)
        for code in sorted(codes):
            with self.subTest(code=code):
                self.assertIn(f"`{code}`", reconcile_section)

    def test_docs_never_build_the_state_root_from_home(self):
        """Regression (audit): the emergency step said touch "$HOME/Library/.../KILL", but the state root is the
        passwd home, not $HOME; with an overridden HOME that file is never checked and nothing is halted."""
        for path in sorted(ROOT.glob("docs/betfair-*.md")):
            with self.subTest(doc=path.name):
                self.assertNotIn('"$HOME/Library/Application Support/betangel-research', read_text(path))
        kill_section = section(read_text(ORDER_PATH_DOC), "## 9. Kill switch")
        self.assertIn("python3 -m betangel_research.betfair kill", kill_section)
        self.assertIn("pw_dir", kill_section)

    def test_named_modules_and_tests_exist_once_final(self):
        """Every module and test the "What is implemented" tables and the setup report name must exist."""
        named = named_files()
        self.assertTrue(any(name == "test_betfair_facts_doc" for _, name, _ in named))
        missing = missing_named_files()
        if not missing:
            return
        if report_status() == DRAFT_STATUS:
            self.skipTest(f"{len(missing)} named files not delivered yet; required before the report leaves draft")
        self.fail("named but missing: " + ", ".join(f"{name} ({where})" for where, name in missing))


if __name__ == "__main__":
    unittest.main()
