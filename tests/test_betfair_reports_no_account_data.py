"""Committed Betfair reports and the team handoff carry codes and hashes only, never account data.

Offline file checks. The patterns are deliberately coarse: an 11+ digit run could be a betId (or a long support
article id or Wayback timestamp, which belong in docs/betfair-facts.md, not in reports).

The handoff hashes are a point-in-time record, like .team-work/collection-infra-20261010/handoff.json. The
default suite checks that they are present, well formed and consistent with each other, and that the S0 state
the handoff claims holds; it never requires a shared file to stay unchanged, so a later edit to collector.py or
AGENTS.md cannot turn the repository suite red. Comparing the tree with the handoff (shared-file drift, S0
all-or-nothing, artifact hashes, final report) is the pre-merge gate: it runs only with
BETANGEL_BETFAIR_MERGE_GATE=1, as merge_gate.pre_merge_checks says.
"""

import copy
import glob
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import unittest

from betangel_research.betfair import secret
from betangel_research.betfair.receipts import code_hashes

import test_betfair_support as support

ROOT = support.REPO_ROOT
REPORT_JSON = ROOT / "reports" / "betfair-setup-2026-10-10.json"
REPORT_MD = ROOT / "reports" / "betfair-setup-2026-10-10.md"
TEAM_DIR = ROOT / ".team-work" / "betfair-exchange-20261010"
HANDOFF = TEAM_DIR / "handoff.json"
BRIEF = TEAM_DIR / "brief.md"
# The binding v1 live-scope amendment, a verbatim copy of a spec text like final.txt: its prose uses "exposure"
# as a risk term (never an account value), so only that one field-name word is allowed there.
AMENDMENT = TEAM_DIR / "amendment-v1-live-scope.md"
SPEC_PROSE_WORDS = {AMENDMENT: frozenset({"exposure"})}

MERGE_GATE_ENV = "BETANGEL_BETFAIR_MERGE_GATE"
FORBID_ASSIGNMENT = "BETANGEL_BETFAIR_FORBID_NETWORK=1"
PROPERTY_ASSIGNMENTS = ("BETANGEL_BETFAIR_PROPERTY_SEEDS=300", "BETANGEL_BETFAIR_PROPERTY_STEPS=200")
PLACEHOLDER = "PENDING_INTEGRATION"
DRAFT_STATUS = "draft_pending_integration_test_run"
FINAL_STATUS = "tests_passed_no_credentials_no_network_calls"
FAILED_STATUS = "tests_failed_no_credentials_no_network_calls"
STATUSES = (DRAFT_STATUS, FINAL_STATUS, FAILED_STATUS)
STATUS_LINE_RE = re.compile(r"^Status：`([a-z_]+)`$", re.MULTILINE)
# T11 was dropped with live replace in v1 (T12 earlier): a receipt for either is not a first-live test.
LIVE_TEST_IDS = frozenset(f"T{n}" for n in range(11))
S0_NOT_APPLIED_LIMITATION = ("S0 (AGENTS.md, betangel_research/__init__.py, .gitignore, .dockerignore) is not "
                             "applied; merging to main is blocked until it is.")

BET_ID_RE = re.compile(r"\b\d{11,}\b")
CUSTOMER_REF_RE = re.compile(r"\bbr[qo]\d{12}-")
ACCOUNT_KEYS = ("availableToBetBalance", "exposure", "pointsBalance", "currencyCode", "connectionId")
ACCOUNT_KEY_RE = re.compile(r"\b(" + "|".join(ACCOUNT_KEYS) + r")\b")
# Credential shapes: a Betfair app key is 16 characters (F-sheet: "16 characters in total"); a session token is
# base64 with "=" padding (certlogin sample). Both need letters and digits, so prose and hex digests do not match.
APP_KEY_SHAPE_RE = re.compile(r"(?<![A-Za-z0-9_+/=.-])(?=[A-Za-z]*[0-9])(?=[0-9]*[A-Za-z])[A-Za-z0-9]{16}"
                              r"(?![A-Za-z0-9_+/=-]|\.[A-Za-z0-9])")
SESSION_TOKEN_SHAPE_RE = re.compile(r"(?<![A-Za-z0-9+/])(?=[A-Za-z+/]*[0-9])(?=[0-9+/]*[A-Za-z])[A-Za-z0-9+/]{20,}"
                                    r"={1,2}(?![A-Za-z0-9+/=])")
SHA256_RE = re.compile(r"[0-9a-f]{64}")

# The handoff follows the existing .team-work key set, plus the three keys spec section 15 adds.
EXISTING_HANDOFF_KEYS = {"completed_at", "starting_revision", "artifact_sha256", "protected_sha256", "reviews", "github",
                         "tests", "cloud_resources_provisioned", "paid_subscriptions_created", "limitations"}
ADDED_HANDOFF_KEYS = {"depends_on_sha256", "s0_patch", "merge_gate"}
HISTORICAL_RECEIPT = "reports/direct-collection-2026-10-10.json"
PROTECTED = {
    "betangel_research/stream.py", "betangel_research/microstructure.py", "betangel_research/motifs.py",
    "betangel_research/evaluation.py", "betangel_research/temporal.py", "betangel_research/collection.py",
    "betangel_research/collector.py", "betangel_research/feeds.py", "betangel_research/__main__.py",
    "betangel_research/__init__.py", "Dockerfile", "deploy/fly.toml", "scripts/backup_metadata.py",
    "research/collector-sources.json", "AGENTS.md", "README.md", ".gitignore", ".dockerignore",
    # Codex's as-deployed receipt of the 2026-10-10 Fly build: historical evidence, byte-identical forever
    # (Astra S0 review; amendment A3 section 7). S0 changes the local .dockerignore only.
    HISTORICAL_RECEIPT,
}
DEPENDS_ON = {"betangel_research/stream.py", "betangel_research/microstructure.py", "betangel_research/evaluation.py",
              "betangel_research/temporal.py"}
S0_TARGETS = {"AGENTS.md", "betangel_research/__init__.py", ".gitignore", ".dockerignore"}
DOCKERIGNORE_EXCLUSIONS = ("betangel_research/betfair/", "betangel_research/betfair/**")
GITIGNORE_CERT_PATTERNS = ("*.crt", "*.csr", "*.p12", "*.pfx")
# What the handoff's artifact_sha256 covers; brief.md's fill script imports this.
ARTIFACT_GLOBS = ("betangel_research/betfair/*.py", "tests/test_betfair_*.py", "docs/betfair-*.md",
                  "research/betfair-*.json", "reports/betfair-setup-2026-10-10.*",
                  ".team-work/betfair-exchange-20261010/final.txt",
                  ".team-work/betfair-exchange-20261010/amendment-v1-live-scope.md")
# Wording that is true only while S0 is not applied (R3#10): refused in docs/betfair-*.md and the brief's Status
# once s0_applied is true, so the S0 commit cannot leave them behind on main.
S0_PENDING_WORDING = ("未套用", "未改", "S0 會補上")
REFRESH_GUIDANCE = ("re-run R1-R3 (test_betfair_recorder), test_betfair_paper and test_betfair_pnl if it is a "
                    "depends_on file; then refresh protected_sha256 (handoff and report) and depends_on_sha256, and "
                    "for an S0 target regenerate s0_patch, s0_expected_sha256_after and the brief's diff block")


def read_text(path):
    return Path(path).read_text(encoding="utf-8")


def load_json(path):
    return json.loads(read_text(path))


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    return sha256_bytes(Path(path).read_bytes())


def is_sha256(value):
    return isinstance(value, str) and SHA256_RE.fullmatch(value) is not None


def scanned_files():
    files = sorted(ROOT.glob("reports/betfair-*.json")) + sorted(ROOT.glob("reports/betfair-*.md"))
    files += sorted(TEAM_DIR.glob("*.json")) + sorted(TEAM_DIR.glob("*.md"))
    return files


def artifact_paths(root):
    """Repository-relative paths the handoff's artifact_sha256 must cover, sorted."""
    return sorted({str(Path(p).relative_to(root)) for pattern in ARTIFACT_GLOBS
                   for p in glob.glob(str(Path(root) / pattern))})


def walk_keys(value):
    if isinstance(value, dict):
        for key, inner in value.items():
            yield key
            yield from walk_keys(inner)
    elif isinstance(value, list):
        for inner in value:
            yield from walk_keys(inner)


def parse_patch(text):
    """{path: [(old_block, new_block), ...]} from a plain unified diff with ---/+++ headers and @@ hunks."""
    files, path, hunk = {}, None, None
    for line in text.splitlines():
        if line.startswith("--- a/"):
            path, hunk = line[len("--- a/"):], None
            files[path] = []
        elif line.startswith("+++ b/"):
            if line[len("+++ b/"):] != path:
                raise ValueError("patch header paths differ")
        elif line.startswith("@@ "):
            hunk = [[], []]
            files[path].append(hunk)
        elif hunk is not None and line[:1] in (" ", "-", "+"):
            if line[0] in (" ", "-"):
                hunk[0].append(line[1:])
            if line[0] in (" ", "+"):
                hunk[1].append(line[1:])
        else:
            raise ValueError(f"unexpected patch line: {line[:40]!r}")
    return {p: [("\n".join(old) + "\n", "\n".join(new) + "\n") for old, new in hunks] for p, hunks in files.items()}


def apply_hunks(text, hunks):
    for old, new in hunks:
        if text.count(old) != 1:
            raise ValueError("hunk context no longer matches the file")
        text = text.replace(old, new)
    return text


def dockerignore_problems(text):
    """The Fly image must exclude betangel_research/betfair: both lines present, after every re-include."""
    lines = [line.strip() for line in text.splitlines()]
    if not all(rule in lines for rule in DOCKERIGNORE_EXCLUSIONS):
        return [".dockerignore lacks the betangel_research/betfair exclusion"]
    last_negation = max((i for i, line in enumerate(lines) if line.startswith("!")), default=-1)
    if min(len(lines) - 1 - lines[::-1].index(rule) for rule in DOCKERIGNORE_EXCLUSIONS) < last_negation:
        return [".dockerignore re-includes paths after the betangel_research/betfair exclusion"]
    return []


def gitignore_problems(text):
    lines = {line.strip() for line in text.splitlines()}
    missing = [pattern for pattern in GITIGNORE_CERT_PATTERNS if pattern not in lines]
    return [f".gitignore lacks {', '.join(missing)}"] if missing else []


def handoff_record_problems(handoff, report):
    """Spec section 14: the protected hashes are present and well formed; no comparison with the tree."""
    problems = []
    if set(handoff) != EXISTING_HANDOFF_KEYS | ADDED_HANDOFF_KEYS:
        problems.append(f"handoff keys differ: {sorted(set(handoff) ^ (EXISTING_HANDOFF_KEYS | ADDED_HANDOFF_KEYS))}")
    if handoff.get("starting_revision") != "0983037":
        problems.append("starting_revision is not 0983037")
    for key in ("cloud_resources_provisioned", "paid_subscriptions_created"):
        if handoff.get(key) is not False:
            problems.append(f"{key} is not false")
    protected = handoff.get("protected_sha256", {})
    if set(protected) != PROTECTED:
        problems.append(f"protected_sha256 paths differ: {sorted(set(protected) ^ PROTECTED)}")
    problems += [f"protected_sha256[{p}] is not a sha256" for p, d in sorted(protected.items()) if not is_sha256(d)]
    depends = handoff.get("depends_on_sha256", {})
    if set(depends) != DEPENDS_ON:
        problems.append(f"depends_on_sha256 paths differ: {sorted(set(depends) ^ DEPENDS_ON)}")
    problems += [f"depends_on_sha256[{p}] differs from protected_sha256" for p, d in sorted(depends.items())
                 if protected.get(p) != d]
    if report.get("protected_sha256") != protected:
        problems.append("the report does not copy protected_sha256 from the handoff")
    return problems


def s0_state_problems(root, handoff, report):
    """The S0 state the handoff and report claim holds, judged only on facts that later edits cannot change.

    A target byte-identical to its recorded pre-S0 text must take the recorded hunks to the recorded post-S0
    hash; a target at the post-S0 hash proves S0 landed. Partial S0 under s0_applied=true fails here because a
    pre-S0 target or a missing .dockerignore/.gitignore rule is a lasting fact. Other drift is the merge gate's.
    """
    gate = handoff["merge_gate"]
    problems = []
    if gate.get("s0_required") is not True:
        problems.append("merge_gate.s0_required is not true")
    if "must not merge to main without S0" not in gate.get("rule", ""):
        problems.append("merge_gate.rule does not state the S0 merge block")
    if set(gate.get("s0_needs_agreement_from", [])) != {"Yu", "Codex"}:
        problems.append("S0 needs agreement from Yu and Codex")
    if set(gate.get("s0_applies_to", [])) != S0_TARGETS:
        problems.append("s0_applies_to differs from the four S0 targets")
    expected_after = gate.get("s0_expected_sha256_after", {})
    if set(expected_after) != S0_TARGETS or not all(is_sha256(d) for d in expected_after.values()):
        problems.append("s0_expected_sha256_after needs one sha256 per S0 target")
        return problems
    if "dockerignore_receipt_refresh" in gate:
        problems.append("dockerignore_receipt_refresh must not exist: the historical deploy receipt stays byte-identical")
    historical = gate.get("historical_receipts", "")
    if HISTORICAL_RECEIPT not in historical or "byte-identical" not in historical:
        problems.append("historical_receipts does not keep Codex's deploy receipt byte-identical")
    for name in ("docs/betfair-integration.md", "docs/betfair-order-path.md", "brief.md"):
        if name not in gate.get("s0_commit", ""):
            problems.append(f"s0_commit does not list {name} wording to rewrite")
    for name in ("R1-R3", "test_betfair_paper", "test_betfair_pnl", "s0_patch", "code_sha256"):
        if name not in gate.get("after_codex_changes_to_depends_on", ""):
            problems.append(f"after_codex_changes_to_depends_on does not mention {name}")
    if "code_sha256" not in gate.get("s0_commit", ""):
        problems.append("s0_commit does not say to refresh the report's code_sha256")
    applied = gate.get("s0_applied")
    if applied not in (True, False):
        problems.append("merge_gate.s0_applied is not a boolean")
        return problems
    if report.get("s0_applied") is not applied:
        problems.append("report s0_applied differs from merge_gate.s0_applied")
    if (S0_NOT_APPLIED_LIMITATION in report.get("limitations", [])) is applied:
        problems.append("the report's S0 limitation line does not match s0_applied")
    try:
        hunks = parse_patch(handoff["s0_patch"])
    except ValueError as exc:
        return problems + [f"s0_patch does not parse: {exc}"]
    if set(hunks) != S0_TARGETS:
        return problems + ["s0_patch does not touch exactly the four S0 targets"]
    added = "".join(new for _, new in hunks[".dockerignore"])
    if not added.endswith("".join(rule + "\n" for rule in DOCKERIGNORE_EXCLUSIONS)):
        problems.append("s0_patch does not end .dockerignore with the betfair exclusion")
    protected = handoff["protected_sha256"]
    for path in sorted(S0_TARGETS):
        data = (Path(root) / path).read_bytes()
        current = sha256_bytes(data)
        if current == protected[path]:
            if applied:
                problems.append(f"{path}: merge_gate.s0_applied is true but the file is still pre-S0")
            try:
                after = apply_hunks(data.decode("utf-8"), hunks[path])
            except ValueError as exc:
                problems.append(f"{path}: {exc}")
                continue
            if sha256_bytes(after.encode("utf-8")) != expected_after[path]:
                problems.append(f"{path}: the recorded hunks do not give s0_expected_sha256_after")
        elif current == expected_after[path] and not applied:
            problems.append(f"{path} is exactly post-S0 but merge_gate.s0_applied is false")
    if applied:
        problems += s0_wording_problems(root)
        if not {"Yu", "Codex"} <= set(gate.get("s0_agreement_recorded", [])):
            problems.append("S0 is marked applied without Yu's and Codex's agreement recorded")
        problems += dockerignore_problems(read_text(Path(root) / ".dockerignore"))
        problems += gitignore_problems(read_text(Path(root) / ".gitignore"))
    return problems


def s0_wording_problems(root):
    """R3#10: once S0 is applied, no Betfair doc and not the brief's Status may still say S0 is pending."""
    root = Path(root)
    texts = [(path.relative_to(root).as_posix(), read_text(path)) for path in sorted(root.glob("docs/betfair-*.md"))]
    brief = root / ".team-work" / "betfair-exchange-20261010" / "brief.md"
    if brief.is_file():
        status = read_text(brief).split("\n## Status", 1)
        texts.append(("brief.md Status", status[1].split("\n## ", 1)[0] if len(status) == 2 else ""))
    return [f"{where} still says S0 is pending ({phrase})" for where, text in texts for phrase in S0_PENDING_WORDING
            if phrase in text]


def readme_commit_problems(text, readme):
    """The final README commit: the Start-here lines right after the collection-pipeline link, the old
    Current-state line replaced and, when the plan has one, the Betfair line once under Local verification."""
    lines = text.splitlines()
    problems = []
    planned = readme.get("local_verification_line")
    if planned is not None:
        heading = readme.get("local_verification_heading")
        if lines.count(planned) != 1 or heading not in lines or lines.index(planned) < lines.index(heading):
            problems.append("README needs the Betfair Local-verification line once, under its heading")
    if readme["current_state_old_line"] in lines:
        problems.append("README still has the old Current-state line")
    if lines.count(readme["current_state_new_line"]) != 1:
        problems.append("README needs the new Current-state line exactly once")
    anchor = readme["start_here_insert_after"]
    if lines.count(anchor) != 1:
        return problems + ["README Start-here anchor line missing or repeated"]
    start = lines.index(anchor) + 1
    if lines[start:start + len(readme["start_here_lines"])] != readme["start_here_lines"]:
        problems.append("README Start-here lines do not follow the collection-pipeline link")
    return problems


def merge_gate_problems(root, handoff, report):
    """Pre-merge only: the tree is exactly what the handoff records, S0 is fully applied and the report is final."""
    gate = handoff["merge_gate"]
    root = Path(root)
    problems = s0_state_problems(root, handoff, report)
    if report.get("status") != FINAL_STATUS:
        problems.append("the setup report is not final")
    if gate.get("s0_applied") is not True:
        problems.append("S0 is not applied: feat/betfair-exchange must not merge to main")
    if PLACEHOLDER in json.dumps(handoff):
        problems.append("handoff.json still has PENDING_INTEGRATION values")
    protected = handoff["protected_sha256"]
    for path in sorted(PROTECTED):
        current = sha256_file(root / path)
        if path in S0_TARGETS:
            if current == protected[path]:
                problems.append(f"{path} is still pre-S0: apply s0_patch")
            elif current != gate["s0_expected_sha256_after"][path]:
                problems.append(f"{path} is not the recorded post-S0 file: {REFRESH_GUIDANCE}")
            continue
        if path == "README.md":
            # The README commit is the branch's last commit before the merge (round 4: a README left at its
            # pre-merge bytes passed, so main could ship the client while README said there were no orders).
            if not readme_commit_problems(read_text(root / path), gate["readme_commit"]):
                continue
            if current == protected[path]:
                problems.append("README.md is still the pre-merge file: apply merge_gate.readme_commit as the last "
                                "commit before the merge")
                continue
        elif current == protected[path]:
            continue
        problems.append(f"{path} changed since the handoff: {REFRESH_GUIDANCE}")
    # The report's code_sha256 covers betangel_research/*.py too, S0's __init__.py included: it must be the tree's.
    if report.get("code_sha256") != code_hashes(root):
        problems.append("report code_sha256 differs from the tree: refresh it (code_hashes, or brief step 4), "
                        "then redo brief step 6")
    artifacts = handoff.get("artifact_sha256")
    if not isinstance(artifacts, dict) or not artifacts:
        return problems + ["artifact_sha256 is not filled in"]
    if sorted(artifacts) != artifact_paths(root):
        problems.append("artifact_sha256 does not cover exactly the betfair artifacts in the tree")
    for path, digest in sorted(artifacts.items()):
        if not (root / path).is_file() or sha256_file(root / path) != digest:
            problems.append(f"artifact {path} differs from artifact_sha256: re-run the brief's fill script")
    return problems


def verification_problems(report, md):
    """The md's Verification section quotes the report JSON's test numbers exactly. Round 4: re-running brief
    step 4 (which rewrites them in the JSON) without the manual step 5 left stale numbers in the md, and neither
    the suite nor the merge gate noticed."""
    tests = report["tests"]
    parts = md.split("\n## Verification\n", 1)
    section = parts[1].split("\n## ", 1)[0] if len(parts) == 2 else ""
    cells = [f"passed `{tests['passed']}`", f"failed `{tests['failed']}`", f"skipped `{tests['skipped']}`",
             f"`{tests['log_sha256']}`", f"`{tests['wall_seconds']}`", f"ran `{tests['property_tests_ran']}`",
             f"wall time `{tests['property_wall_seconds']}`", f"log sha256 `{tests['property_log_sha256']}`"]
    return [f"the md Verification section does not quote {cell} from the report JSON" for cell in cells
            if cell not in section]


def status_problems(report, md, handoff_text):
    """The report's status, its md and the handoff agree, and only a fully evidenced report leaves draft."""
    problems = []
    status = report.get("status")
    if status not in STATUSES:
        return [f"unknown report status {status!r}"]
    lines = STATUS_LINE_RE.findall(md)
    if lines != [status]:
        problems.append(f"the md needs exactly one 'Status：`{status}`' line, found {lines}")
    others = [s for s in STATUSES if s != status and s in md]
    if others:
        problems.append(f"the md names another status: {others}")
    handoff = json.loads(handoff_text)
    tests = report["tests"]
    if handoff["tests"].get("command") != tests.get("command"):
        problems.append("handoff tests.command differs from the report")
    if status == DRAFT_STATUS:
        if PLACEHOLDER not in json.dumps(report) and PLACEHOLDER not in md:
            problems.append("a draft report still names the values it is waiting for")
        return problems
    for where, text in (("report json", json.dumps(report)), ("report md", md), ("handoff.json", handoff_text)):
        if PLACEHOLDER in text:
            problems.append(f"{where} still has PENDING_INTEGRATION")
    for key in ("passed", "failed", "skipped", "property_tests_ran"):
        if type(tests.get(key)) is not int:
            problems.append(f"tests.{key} is not an int")
    for key in ("wall_seconds", "property_wall_seconds"):
        if not isinstance(tests.get(key), float):
            problems.append(f"tests.{key} is not a float")
    for key in ("log_sha256", "property_log_sha256"):
        if not is_sha256(tests.get(key)):
            problems.append(f"tests.{key} is not a sha256")
    code = report.get("code_sha256")
    if not isinstance(code, dict) or not code or not all(is_sha256(d) for d in code.values()):
        problems.append("code_sha256 is not a map of sha256 digests")
    for key in ("passed", "failed", "log_sha256"):
        if handoff["tests"].get(key) != tests.get(key):
            problems.append(f"handoff tests.{key} differs from the report")
    for key in ("completed_at", "reviews"):
        if not handoff.get(key):
            problems.append(f"handoff {key} is empty")
    artifacts = handoff.get("artifact_sha256")
    if not isinstance(artifacts, dict) or not artifacts or not all(is_sha256(d) for d in artifacts.values()):
        problems.append("handoff artifact_sha256 is not a map of sha256 digests")
    if problems:
        return problems
    problems += verification_problems(report, md)
    if status == FINAL_STATUS:
        if tests["failed"] != 0:
            problems.append("a passing report has failures")
        if tests["passed"] <= tests["preexisting_tests"]:
            problems.append("a passing report must count the new tests")
        if tests["property_tests_ran"] <= 0:
            problems.append("no property test ran")
    elif tests["failed"] <= 0:
        problems.append("a failed report needs failures")
    return problems


class ReportsNoAccountDataTests(unittest.TestCase):
    def setUp(self):
        support.block_network(self)

    def test_reports_have_no_betids_refs_balances(self):
        files = scanned_files()
        self.assertIn(REPORT_JSON, files)
        self.assertIn(REPORT_MD, files)
        self.assertIn(HANDOFF, files)
        # The verbatim amendment copy gets every check; only "exposure" (a risk term in its prose) is allowed.
        self.assertIn(AMENDMENT, files)
        self.assertEqual(SPEC_PROSE_WORDS, {AMENDMENT: frozenset({"exposure"})})
        self.assertIn(str(AMENDMENT.relative_to(ROOT)), artifact_paths(ROOT), "its hash is tracked like final.txt")
        for path in files:
            text = read_text(path)
            rel = path.relative_to(ROOT)
            with self.subTest(file=str(rel)):
                self.assertIsNone(BET_ID_RE.search(text), "digit run that could be a betId")
                self.assertIsNone(CUSTOMER_REF_RE.search(text), "customerRef/customerOrderRef value")
                allowed = SPEC_PROSE_WORDS.get(path, frozenset())
                found = [word for word in ACCOUNT_KEY_RE.findall(text) if word not in allowed]
                self.assertEqual(found, [], "account field name")
                self.assertIsNone(APP_KEY_SHAPE_RE.search(text), "16-character app-key-shaped string")
                self.assertIsNone(SESSION_TOKEN_SHAPE_RE.search(text), "base64 session-token-shaped string")
                self.assertNotIn(support.PLANTED_SECRET, text)
                if path.suffix == ".json":
                    data = json.loads(text)
                    secret.refuse_forbidden(data, where=str(rel))
                    self.assertFalse(set(walk_keys(data)) & set(ACCOUNT_KEYS))

    def test_credential_shape_patterns(self):
        self.assertIsNotNone(APP_KEY_SHAPE_RE.search("key: abcd1234EFGH5678."))
        self.assertIsNotNone(SESSION_TOKEN_SHAPE_RE.search('"sessionToken":"Zx8i4oigut5nc+l4L8qFb0DSxG+mwLn2t0AMGFxjrMJI="'))
        for text in ("PENDING_INTEGRATION", "a" * 64, "customerStrategyRefs=", "betangel-research.betfair.app-key.live",
                     "module1234567890.py",
                     "live-0123456789ab-20261010", "/wiki/spaces/1smk3cen4v3lu3yomq5qye0ni/pages/2687915"):
            with self.subTest(text=text[:30]):
                self.assertIsNone(APP_KEY_SHAPE_RE.search(text))
                self.assertIsNone(SESSION_TOKEN_SHAPE_RE.search(text))

    def test_report_status_matches_placeholders(self):
        report = load_json(REPORT_JSON)
        md = read_text(REPORT_MD)
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(report["network_calls_made"], 0)
        self.assertIs(report["credentials_present"], False)
        self.assertEqual(report["live_orders_placed"], 0)
        # This receipt's status says no network call was made; live tests go in their own dated reports.
        self.assertEqual(report["first_live_tests_run"], [])
        self.assertIsNone(report["economic_result"])
        self.assertEqual(report["python_310"], "syntax_checked_only")
        tests = report["tests"]
        self.assertEqual(tests["property_seeds"], 300)
        self.assertEqual(tests["property_steps"], 200)
        self.assertEqual(tests["wall_seconds_scope"], "full_suite_unittest_ran_line")
        for key in ("command", "property_command"):
            with self.subTest(command=key):
                for assignment in (FORBID_ASSIGNMENT,) + PROPERTY_ASSIGNMENTS:
                    self.assertIn(assignment, tests[key])
        self.assertIn("discover -s tests -v", tests["command"])
        self.assertIn("-p test_betfair_risk_properties.py", tests["property_command"])
        self.assertNotIn("證明", md, "case-based tests are 'checked on the tested cases', not proof")
        self.assertEqual(status_problems(report, md, read_text(HANDOFF)), [])

    def test_live_test_reports_list_known_tests(self):
        for path in sorted(ROOT.glob("reports/betfair-*.json")):
            runs = load_json(path).get("first_live_tests_run", [])
            with self.subTest(report=path.name):
                self.assertIsInstance(runs, list)
                for entry in runs:
                    self.assertEqual(set(entry), {"test_id", "receipt_sha256"})
                    self.assertIn(entry["test_id"], LIVE_TEST_IDS)
                    self.assertTrue(is_sha256(entry["receipt_sha256"]))

    def test_handoff_protected_sha_present(self):
        self.assertEqual(handoff_record_problems(load_json(HANDOFF), load_json(REPORT_JSON)), [])

    def test_handoff_s0_patch_and_merge_gate(self):
        handoff = load_json(HANDOFF)
        report = load_json(REPORT_JSON)
        self.assertEqual(s0_state_problems(ROOT, handoff, report), [])
        gate = handoff["merge_gate"]
        unittest_checks = [c for c in gate["pre_merge_checks"] if "unittest" in c]
        self.assertTrue(unittest_checks)
        for command in unittest_checks:
            with self.subTest(command=command[:60]):
                self.assertIn(FORBID_ASSIGNMENT, command)
        self.assertTrue(any(f"{MERGE_GATE_ENV}=1" in c for c in unittest_checks), "the merge gate run is listed")
        self.assertIn(FORBID_ASSIGNMENT, handoff["tests"]["command"])
        brief = read_text(BRIEF)
        start = brief.index("```diff\n") + len("```diff\n")
        self.assertEqual(brief[start:brief.index("```", start)], handoff["s0_patch"],
                         "brief.md shows the S0 patch verbatim")
        merge_section = read_text(REPORT_MD).split("## Merge gate", 1)[1].split("\n## ", 1)[0]
        self.assertEqual("未套用" in merge_section, not gate["s0_applied"], "the md merge-gate paragraph is current")

    def test_readme_plan_links_every_betfair_doc_and_report(self):
        """Regression (audit): README Start-here links every doc and report, but the planned README commit
        added only two of the four Betfair ones and no Local-verification line."""
        readme = load_json(HANDOFF)["merge_gate"]["readme_commit"]
        planned = "\n".join(readme["start_here_lines"])
        for path in sorted((ROOT / "docs").glob("betfair-*.md")) + sorted((ROOT / "reports").glob("betfair-*.md")):
            with self.subTest(path=path.name):
                self.assertIn(f"]({path.relative_to(ROOT).as_posix()})", planned)
        self.assertEqual(readme["local_verification_heading"], "## Local verification")
        self.assertIn("reports/betfair-setup-2026-10-10.md", readme["local_verification_line"])

    @unittest.skipUnless(os.environ.get(MERGE_GATE_ENV) == "1", f"pre-merge gate; set {MERGE_GATE_ENV}=1")
    def test_merge_gate_tree_matches_handoff(self):
        self.assertEqual(merge_gate_problems(ROOT, load_json(HANDOFF), load_json(REPORT_JSON)), [])


SYNTHETIC_S0 = {
    "AGENTS.md": ("# Agents\n- keep one\n- old scope line\n- keep two\n",
                  "# Agents\n- keep one\n- new scope line\n- betfair bullet\n- keep two\n"),
    "betangel_research/__init__.py": ('"""Offline only."""\n\n__version__ = "0.1.0"\n',
                                      '"""Research package.\nbetfair: paper by default."""\n\n__version__ = "0.1.0"\n'),
    ".gitignore": ("*.pem\n*.key\n", "*.pem\n*.key\n*.crt\n*.csr\n*.p12\n*.pfx\n"),
    ".dockerignore": ("**\n!betangel_research/\n!betangel_research/**\n**/.env*\n",
                      "**\n!betangel_research/\n!betangel_research/**\n**/.env*\nbetangel_research/betfair/\n"
                      "betangel_research/betfair/**\n"),
}
SYNTHETIC_PATCH = (
    "--- a/AGENTS.md\n+++ b/AGENTS.md\n@@ -1,4 +1,5 @@\n # Agents\n - keep one\n-- old scope line\n"
    "+- new scope line\n+- betfair bullet\n - keep two\n"
    "--- a/betangel_research/__init__.py\n+++ b/betangel_research/__init__.py\n@@ -1,3 +1,4 @@\n"
    '-"""Offline only."""\n+"""Research package.\n+betfair: paper by default."""\n \n __version__ = "0.1.0"\n'
    "--- a/.gitignore\n+++ b/.gitignore\n@@ -1,2 +1,6 @@\n *.pem\n *.key\n+*.crt\n+*.csr\n+*.p12\n+*.pfx\n"
    "--- a/.dockerignore\n+++ b/.dockerignore\n@@ -1,4 +1,6 @@\n **\n !betangel_research/\n !betangel_research/**\n"
    " **/.env*\n+betangel_research/betfair/\n+betangel_research/betfair/**\n"
)
SYNTHETIC_README = ("# Repo\n\n## Start here\n\n- [Collection pipeline](docs/collection-pipeline.md)\n- [Other](x.md)\n\n"
                    "## Current state\n\n- No purchases, live account connections or orders are part of this repository.\n")
SYNTHETIC_README_COMMIT = {
    "start_here_insert_after": "- [Collection pipeline](docs/collection-pipeline.md)",
    "start_here_lines": ["- [Betfair one](docs/betfair-integration.md)", "- [Betfair two](docs/betfair-order-path.md)"],
    "current_state_old_line": "- No purchases, live account connections or orders are part of this repository.",
    "current_state_new_line": "- A Betfair client exists; no live order has been made.",
}


class HandoffGateRegressionTests(unittest.TestCase):
    """The default checks tolerate later shared-file edits; the merge gate and the S0 checks refuse partial S0."""

    def setUp(self):
        support.block_network(self)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for path in sorted(PROTECTED):
            text = SYNTHETIC_S0[path][0] if path in S0_TARGETS else f"synthetic {path}\n"
            self.write(path, SYNTHETIC_README if path == "README.md" else text)
        self.write("docs/betfair-x.md", "synthetic artifact\n")
        protected = {path: sha256_file(self.root / path) for path in sorted(PROTECTED)}
        self.handoff = copy.deepcopy(load_json(HANDOFF))
        self.handoff_original_historical = self.handoff["merge_gate"].get("historical_receipts")
        self.handoff.update(
            protected_sha256=protected, depends_on_sha256={p: protected[p] for p in sorted(DEPENDS_ON)},
            s0_patch=SYNTHETIC_PATCH, completed_at="2026-10-10T00:00:00Z", reviews=["synthetic review"],
            artifact_sha256={"docs/betfair-x.md": sha256_file(self.root / "docs/betfair-x.md")})
        self.handoff["tests"] = {"command": "synthetic", "passed": 1, "failed": 0, "log_sha256": "b" * 64}
        gate = self.handoff["merge_gate"]
        gate.update(s0_applied=False, s0_agreement_recorded=[], readme_commit=SYNTHETIC_README_COMMIT,
                    s0_expected_sha256_after={p: sha256_bytes(SYNTHETIC_S0[p][1].encode("utf-8")) for p in S0_TARGETS})
        self.report = {"status": FINAL_STATUS, "protected_sha256": protected, "s0_applied": False,
                       "limitations": [S0_NOT_APPLIED_LIMITATION], "code_sha256": code_hashes(self.root)}

    def write(self, path, text):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def apply_s0(self, paths=S0_TARGETS, *, record=True):
        for path in paths:
            self.write(path, SYNTHETIC_S0[path][1])
        if record:
            # The S0 commit procedure: flags, agreement, limitation, and the report's code_sha256.
            self.handoff["merge_gate"].update(s0_applied=True, s0_agreement_recorded=["Yu", "Codex"])
            self.report.update(s0_applied=True, limitations=[], code_sha256=code_hashes(self.root))

    def apply_readme_commit(self):
        """The final README commit of SYNTHETIC_README_COMMIT (the last commit before the merge)."""
        readme = SYNTHETIC_README_COMMIT
        anchor = readme["start_here_insert_after"]
        text = read_text(self.root / "README.md").replace(anchor, "\n".join([anchor] + readme["start_here_lines"]))
        self.write("README.md", text.replace(readme["current_state_old_line"], readme["current_state_new_line"]))

    def merge_ready(self):
        self.apply_s0()
        self.apply_readme_commit()

    def test_synthetic_fixture_is_clean(self):
        self.assertEqual(handoff_record_problems(self.handoff, self.report), [])
        self.assertEqual(s0_state_problems(self.root, self.handoff, self.report), [])
        self.apply_s0()
        self.assertEqual(s0_state_problems(self.root, self.handoff, self.report), [])
        # Round 4: S0 alone is not enough: the README commit is part of the merge plan, and the gate requires it.
        self.assertEqual(merge_gate_problems(self.root, self.handoff, self.report),
                         ["README.md is still the pre-merge file: apply merge_gate.readme_commit as the last commit "
                          "before the merge"])
        self.apply_readme_commit()
        self.assertEqual(merge_gate_problems(self.root, self.handoff, self.report), [])

    def test_default_checks_ignore_later_shared_file_edits(self):
        self.apply_s0()
        self.write("betangel_research/collector.py", "Codex changed this after the merge\n")
        self.write("betangel_research/stream.py", "Codex changed a depends_on file\n")
        self.write("AGENTS.md", SYNTHETIC_S0["AGENTS.md"][1] + "- a later AGENTS.md bullet\n")
        self.assertEqual(handoff_record_problems(self.handoff, self.report), [])
        self.assertEqual(s0_state_problems(self.root, self.handoff, self.report), [])
        gate = merge_gate_problems(self.root, self.handoff, self.report)
        for path in ("betangel_research/collector.py", "betangel_research/stream.py", "AGENTS.md"):
            with self.subTest(path=path):
                found = [p for p in gate if p.startswith(path)]
                self.assertEqual(len(found), 1)
                self.assertIn("re-run R1-R3", found[0])
                self.assertIn("regenerate s0_patch, s0_expected_sha256_after", found[0])

    def test_stale_code_sha256_is_refused_by_the_gate(self):
        """Regression (audit): code_sha256 covers betangel_research/__init__.py (an S0 target) but nothing compared
        it with the tree, so a report could say s0_applied with the pre-S0 hash. The gate compares it now, and the
        S0 commit and the after-Codex steps both say to refresh it."""
        pre_s0 = code_hashes(self.root)
        self.merge_ready()
        self.assertEqual(merge_gate_problems(self.root, self.handoff, self.report), [])
        self.report["code_sha256"] = pre_s0
        self.assertIn("report code_sha256 differs from the tree: refresh it (code_hashes, or brief step 4), "
                      "then redo brief step 6", merge_gate_problems(self.root, self.handoff, self.report))
        gate = self.handoff["merge_gate"]
        gate["s0_commit"] = gate["s0_commit"].replace("code_sha256", "code hashes")
        self.assertIn("s0_commit does not say to refresh the report's code_sha256",
                      s0_state_problems(self.root, self.handoff, self.report))

    def test_partial_s0_is_refused(self):
        self.apply_s0({"AGENTS.md"})
        problems = s0_state_problems(self.root, self.handoff, self.report)
        self.assertIn(".dockerignore: merge_gate.s0_applied is true but the file is still pre-S0", problems)
        self.assertIn(".dockerignore lacks the betangel_research/betfair exclusion", problems)
        self.assertTrue(merge_gate_problems(self.root, self.handoff, self.report))

    def test_s0_flag_true_on_pristine_files_is_refused(self):
        self.handoff["merge_gate"].update(s0_applied=True, s0_agreement_recorded=["Yu", "Codex"])
        self.report.update(s0_applied=True, limitations=[])
        problems = s0_state_problems(self.root, self.handoff, self.report)
        self.assertEqual(len([p for p in problems if "still pre-S0" in p]), 4)

    def test_unrecorded_s0_is_refused(self):
        self.apply_s0({".dockerignore"}, record=False)
        self.assertIn(".dockerignore is exactly post-S0 but merge_gate.s0_applied is false",
                      s0_state_problems(self.root, self.handoff, self.report))

    def test_s0_needs_agreement_and_matching_report(self):
        self.apply_s0()
        self.handoff["merge_gate"]["s0_agreement_recorded"] = ["Yu"]
        self.assertIn("S0 is marked applied without Yu's and Codex's agreement recorded",
                      s0_state_problems(self.root, self.handoff, self.report))
        self.report["s0_applied"] = False
        self.assertIn("report s0_applied differs from merge_gate.s0_applied",
                      s0_state_problems(self.root, self.handoff, self.report))
        self.report.update(s0_applied=True, limitations=[S0_NOT_APPLIED_LIMITATION])
        self.assertIn("the report's S0 limitation line does not match s0_applied",
                      s0_state_problems(self.root, self.handoff, self.report))

    def test_historical_deploy_receipt_stays_byte_identical(self):
        """Astra S0 review: S0 changes the local .dockerignore, not the as-deployed receipt of the 2026-10-10 build.
        The handoff must not ask anyone to refresh it, must say it stays byte-identical, and the merge gate refuses
        any edit to it (it is a protected file)."""
        self.assertIn(HISTORICAL_RECEIPT, PROTECTED)
        gate = self.handoff["merge_gate"]
        self.assertNotIn("dockerignore_receipt_refresh", gate)
        self.assertIn(HISTORICAL_RECEIPT, gate["historical_receipts"])
        gate["dockerignore_receipt_refresh"] = f"refresh {HISTORICAL_RECEIPT} after S0"
        self.assertIn("dockerignore_receipt_refresh must not exist: the historical deploy receipt stays byte-identical",
                      s0_state_problems(self.root, self.handoff, self.report))
        del gate["dockerignore_receipt_refresh"]
        gate["historical_receipts"] = "receipts are refreshed after S0"
        self.assertIn("historical_receipts does not keep Codex's deploy receipt byte-identical",
                      s0_state_problems(self.root, self.handoff, self.report))
        gate["historical_receipts"] = self.handoff_original_historical
        self.merge_ready()
        self.assertEqual(merge_gate_problems(self.root, self.handoff, self.report), [])
        self.write(HISTORICAL_RECEIPT, "refreshed .dockerignore hash\n")
        self.assertTrue(any(p.startswith(f"{HISTORICAL_RECEIPT} changed since the handoff") for p in
                            merge_gate_problems(self.root, self.handoff, self.report)))

    def test_s0_pending_wording_is_refused_once_applied(self):
        """Regression (R3#10): the S0 commit procedure omitted doc lines that become false once S0 lands
        (".gitignore ... S0 會補上", "... 未改", "S0 ... 未套用", the brief's Status), and the gate checked only the
        report md. With s0_applied true those phrases in docs/betfair-*.md or the brief's Status are refused."""
        self.write("docs/betfair-y.md", "S0 未套用；未套用之前唔准 merge 去 main。\n")
        brief = ".team-work/betfair-exchange-20261010/brief.md"
        self.write(brief, "# Brief\n\nS0 未套用 here is history, not Status.\n\n## Status\n\nS0 仍然未套用。\n")
        self.assertEqual(s0_wording_problems(self.root), [
            "docs/betfair-y.md still says S0 is pending (未套用)", "brief.md Status still says S0 is pending (未套用)"])
        self.assertEqual(s0_state_problems(self.root, self.handoff, self.report), [], "not applied: wording is true")
        self.apply_s0()
        problems = s0_state_problems(self.root, self.handoff, self.report)
        self.assertIn("docs/betfair-y.md still says S0 is pending (未套用)", problems)
        self.assertIn("brief.md Status still says S0 is pending (未套用)", problems)
        self.write("docs/betfair-y.md", "S0 已經套用。\n")
        self.write(brief, "# Brief\n\nS0 未套用 here is history, not Status.\n\n## Status\n\nS0 已經套用。\n")
        self.assertEqual(s0_state_problems(self.root, self.handoff, self.report), [])
        gate = self.handoff["merge_gate"]
        gate["s0_commit"] = gate["s0_commit"].replace("docs/betfair-order-path.md", "the order-path doc")
        self.assertIn("s0_commit does not list docs/betfair-order-path.md wording to rewrite",
                      s0_state_problems(self.root, self.handoff, self.report))

    def test_dockerignore_reinclude_after_exclusion_is_refused(self):
        self.apply_s0()
        self.write(".dockerignore", SYNTHETIC_S0[".dockerignore"][1] + "!betangel_research/**\n")
        self.assertIn(".dockerignore re-includes paths after the betangel_research/betfair exclusion",
                      s0_state_problems(self.root, self.handoff, self.report))

    def test_merge_gate_needs_final_report_and_s0(self):
        problems = merge_gate_problems(self.root, self.handoff, self.report)
        self.assertIn("S0 is not applied: feat/betfair-exchange must not merge to main", problems)
        self.apply_s0()
        self.report["status"] = DRAFT_STATUS
        self.assertIn("the setup report is not final", merge_gate_problems(self.root, self.handoff, self.report))
        self.report["status"] = FINAL_STATUS
        self.write("docs/betfair-x.md", "edited after the hashes were taken\n")
        self.assertIn("artifact docs/betfair-x.md differs from artifact_sha256: re-run the brief's fill script",
                      merge_gate_problems(self.root, self.handoff, self.report))

    def test_readme_commit_must_replace_old_line(self):
        readme = SYNTHETIC_README_COMMIT
        anchor = readme["start_here_insert_after"]
        inserted = "\n".join([anchor] + readme["start_here_lines"])
        good = SYNTHETIC_README.replace(anchor, inserted).replace(readme["current_state_old_line"],
                                                                  readme["current_state_new_line"])
        self.assertEqual(readme_commit_problems(good, readme), [])
        both = SYNTHETIC_README.replace(anchor, inserted) + readme["current_state_new_line"] + "\n"
        self.assertIn("README still has the old Current-state line", readme_commit_problems(both, readme))
        misplaced = good.replace(inserted, anchor) + "\n".join(readme["start_here_lines"]) + "\n"
        self.assertIn("README Start-here lines do not follow the collection-pipeline link",
                      readme_commit_problems(misplaced, readme))
        self.write("README.md", both)
        self.apply_s0()
        self.assertTrue(any(p.startswith("README.md changed") for p in
                            merge_gate_problems(self.root, self.handoff, self.report)))
        self.write("README.md", good)
        self.assertEqual(merge_gate_problems(self.root, self.handoff, self.report), [])
        # A planned Local-verification line must then appear once, under its heading.
        planned = dict(readme, local_verification_heading="## Local verification",
                       local_verification_line="Betfair client: all offline tests passed.")
        self.assertIn("README needs the Betfair Local-verification line once, under its heading",
                      readme_commit_problems(good, planned))
        verified = good + "\n## Local verification\n\nBetfair client: all offline tests passed.\n"
        self.assertEqual(readme_commit_problems(verified, planned), [])

    def test_status_needs_matching_md_and_complete_handoff(self):
        command = "BETANGEL_BETFAIR_FORBID_NETWORK=1 python3 -B -m unittest discover -s tests -v"
        report = {"status": FINAL_STATUS, "checked_at": "2026-10-10T00:00:00Z", "code_sha256": {"a.py": "c" * 64},
                  "tests": {"command": command, "passed": 500, "failed": 0, "skipped": 0, "preexisting_tests": 142,
                            "log_sha256": "d" * 64, "wall_seconds": 12.5, "property_tests_ran": 3,
                            "property_wall_seconds": 4.25, "property_log_sha256": "e" * 64}}
        handoff = {"completed_at": "2026-10-10T00:00:00Z", "reviews": ["synthetic review"],
                   "artifact_sha256": {"a.py": "f" * 64},
                   "tests": {"command": command, "passed": 500, "failed": 0, "log_sha256": "d" * 64}}
        md = (f"# Report\n\nStatus：`{FINAL_STATUS}`\n\n## Verification\n\n| suite | passed `500`，failed `0`，skipped "
              f"`0` |\n| log | `{'d' * 64}` |\n| wall | `12.5` |\n| property | ran `3` 個，wall time `4.25` 秒，log sha256 "
              f"`{'e' * 64}` |\n\n## Reviews\n\nnone\n")
        self.assertEqual(status_problems(report, md, json.dumps(handoff)), [])
        # Round 4: the md quotes the JSON's numbers; a JSON refilled without the md (brief step 5) is refused.
        refilled = copy.deepcopy(report)
        refilled["tests"].update(log_sha256="c" * 64, wall_seconds=13.0)
        self.assertEqual(status_problems(refilled, md, json.dumps(dict(handoff, tests=dict(handoff["tests"],
                                                                                            log_sha256="c" * 64)))),
                         [f"the md Verification section does not quote `{'c' * 64}` from the report JSON",
                          "the md Verification section does not quote `13.0` from the report JSON"])
        stale = md.replace(f"Status：`{FINAL_STATUS}`", f"Status：`{DRAFT_STATUS}`。Later `{FINAL_STATUS}`")
        self.assertTrue(status_problems(report, stale, json.dumps(handoff)), "md Status line still draft")
        self.assertTrue(status_problems(report, md + f"\n`{DRAFT_STATUS}`\n", json.dumps(handoff)))
        self.assertTrue(status_problems(report, md + f"\n{PLACEHOLDER}\n", json.dumps(handoff)))
        self.assertIn("handoff.json still has PENDING_INTEGRATION",
                      status_problems(report, md, json.dumps(dict(handoff, reviews=PLACEHOLDER))))
        drifted = copy.deepcopy(handoff)
        drifted["tests"]["passed"] = 499
        self.assertIn("handoff tests.passed differs from the report",
                      status_problems(report, md, json.dumps(drifted)))
        report["tests"]["property_tests_ran"] = 0
        self.assertIn("no property test ran", status_problems(report, md, json.dumps(handoff)))
        draft = dict(report, status=DRAFT_STATUS)
        draft_md = md.replace(FINAL_STATUS, DRAFT_STATUS)
        self.assertIn("a draft report still names the values it is waiting for",
                      status_problems(draft, draft_md, json.dumps(handoff)))
        self.assertEqual(status_problems(draft, draft_md + f"`{PLACEHOLDER}`\n", json.dumps(handoff)), [])

if __name__ == "__main__":
    unittest.main()
