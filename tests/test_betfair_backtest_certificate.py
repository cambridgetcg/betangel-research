"""Engine certificate (betangel_research.betfair.backtest.certificate) and certify-engine (certify.certify_engine and
the CLI; task c2b3b2b).

These show: the source descriptor covers exactly the backtest sources and the parent sources, and refuses symlinks,
subdirectories, stray or shadowing files, oversized and unreadable sources; the interpreter descriptor is the running
interpreter; the P0 profile is the P0 contract's selectors (variable parameters and texts left out) and matches the
rules the certification cases run under; the key moves with every input it binds and with nothing else; a
certificate is certified only for exactly the registry's cases, all as expected, and validation catches every field
tampered with; storage writes by exclusive create, reuses identical bytes and refuses other bytes, keeping both; the
work root and every derived path are checked before anything is written (Astra's N1); certify_engine's outcomes and
exit codes, with canned case results; the CLI; and, in a fresh process, the real certifier writes a certificate the
loader accepts. Nothing here reads real data or an account.
"""

import contextlib
import copy
from decimal import Decimal
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from betangel_research.betfair.backtest import __main__ as cli
from betangel_research.betfair.backtest import certificate, certification_fixtures as cf, certify
from betangel_research.betfair.backtest.certificate import CertificateProblem
from betangel_research.betfair.backtest.contract import canonical_json

REPO = Path(__file__).resolve().parent.parent
P0_CONTRACT = REPO / "research" / "experiments" / "backtest-p0-bsp-v1.json"
ENV = {"BETANGEL_BETFAIR_FORBID_NETWORK": "1", "PYTHONDONTWRITEBYTECODE": "1"}
# The P0 contract's leaves that profile_of does not read (patterns: [] for a list index): the variable parameters,
# texts, ids, status and dates, schema_version, the financial rule kind (fixed by the registration kind), the rate's
# provenance and the holdout's state (fixed by the validator). Any other leaf must move the profile.
INERT_LEAVES = {
    "cohort.exclusions.[]", "cohort.market_rule", "cohort.runner_rule", "costs.commission_rate", "costs.note",
    "costs.rate_source", "costs.rounding", "decision_timing.offset_s", "engineering_acceptance.criteria.[].definition",
    "experiment_id", "features.[].definition", "financial_rule.kind", "financial_rule.reason", "frozen_on",
    "population.splits.engineering.end_month", "population.splits.engineering.start_month",
    "population.splits.holdout.end_month", "population.splits.holdout.start_month", "population.splits.holdout.state",
    "population.splits.research.end_month", "population.splits.research.start_month", "registration.note",
    "schema_version", "seeds.bootstrap", "seeds.selection", "staking.stake_pence", "status", "stopping_rule.max_trials",
    "stopping_rule.rule", "uncertainty.confidence_level", "uncertainty.resamples", "variants.[].description"}


def leaves(value, path=()):
    """(path, leaf) of every leaf of a JSON value; an empty list or dict, and null, are leaves too."""
    if isinstance(value, (dict, list)) and value:
        for key, item in (value.items() if isinstance(value, dict) else enumerate(value)):
            yield from leaves(item, path + (key,))
    else:
        yield path, value


def other(value):
    if isinstance(value, bool):
        return not value
    if isinstance(value, (int, float)):
        return value + 1
    if isinstance(value, str):
        return value + "_other"
    return ["other"] if isinstance(value, list) else 1


def reseal(value):
    """A deep copy of a certificate with its source digest and key recomputed, so only the tampered shape remains."""
    value = copy.deepcopy(value)
    value["source"]["sha256"] = certificate.descriptor_sha256(value["source"]["descriptor"])
    value["key"] = certificate.certificate_key(value["source"]["sha256"], value["interpreter"])
    return value


@contextlib.contextmanager
def recorded_reads():
    """Every size passed to read() on a file opened through Path.open, in order."""
    sizes, real_open = [], Path.open

    class Handle:
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

        def write(self, data):
            return self.handle.write(data)

    with mock.patch.object(Path, "open", lambda path, *args, **kwargs: Handle(real_open(path, *args, **kwargs))):
        yield sizes


def canned(results=None):
    """run_cases results for every registered case, as_expected unless results overrides an id's result."""
    results = results or {}
    return [{"id": case_id, "kind": kind, "result": results.get(case_id, certificate.AS_EXPECTED),
             "observed": {"case": case_id}, "expected": {"case": case_id}} for case_id, kind, _ in certificate.CASES]


def built(results=None):
    descriptor, _ = certificate.source_descriptor()
    return certificate.build_certificate(canned(results), descriptor, certificate.interpreter_descriptor())


def p0_contract(**changes):
    contract = json.loads(P0_CONTRACT.read_text(encoding="utf-8"))
    contract["decision_timing"]["delay_s"] = 0
    for dotted, value in changes.items():
        *path, last = dotted.split(".")
        target = contract
        for key in path:
            target = target[key]
        target[last] = value
    return contract


class Workspace(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()

    def fake_sources(self):
        """A minimal source tree with the descriptor's layout."""
        root = self.tmp / "src"
        backtest = root / "betangel_research" / "betfair" / "backtest"
        backtest.mkdir(parents=True)
        for relative in (*certificate.PARENT_SOURCES, "betangel_research/betfair/backtest/__init__.py",
                         "betangel_research/betfair/backtest/a.py"):
            (root / relative).write_text(f"# {relative}\n")
        return root, backtest


class DescriptorTests(Workspace):
    def test_it_covers_the_backtest_sources_and_the_parents(self):
        descriptor, digest = certificate.source_descriptor()
        paths = [item["path"] for item in descriptor["files"]]
        backtest = sorted(f"betangel_research/betfair/backtest/{path.name}"
                          for path in (REPO / "betangel_research" / "betfair" / "backtest").glob("*.py"))
        self.assertEqual(paths, sorted(backtest + list(certificate.PARENT_SOURCES)))
        self.assertEqual(digest, certificate.descriptor_sha256(descriptor))
        self.assertEqual(certificate.source_descriptor(), (descriptor, digest))
        for item in descriptor["files"]:
            data = (REPO / item["path"]).read_bytes()
            self.assertEqual((item["size"], item["sha256"]), (len(data), certificate._sha256(data)))

    def test_a_changed_byte_changes_the_digest(self):
        root, backtest = self.fake_sources()
        _, before = certificate.source_descriptor(root)
        (backtest / "a.py").write_text("# changed\n")
        self.assertNotEqual(certificate.source_descriptor(root)[1], before)

    def test_a_pycache_directory_is_skipped(self):
        root, backtest = self.fake_sources()
        _, before = certificate.source_descriptor(root)
        (backtest / "__pycache__").mkdir()
        (backtest / "__pycache__" / "a.cpython-314.pyc").write_bytes(b"x")
        self.assertEqual(certificate.source_descriptor(root)[1], before)

    def test_what_it_cannot_describe_is_refused(self):
        def layout(change):
            root, backtest = self.fake_sources()
            change(root, backtest)
            return root

        def link(root, backtest):
            (backtest / "b.py").symlink_to(backtest / "a.py")

        cases = {
            "symlink": (link, "backtest/b.py:symlink"),
            "subdirectory": (lambda root, b: (b / "sub").mkdir(), "backtest/sub:subdirectory"),
            "extension module": (lambda root, b: (b / "a.cpython-314-darwin.so").write_bytes(b""),
                                 "not_a_python_source"),
            "sourceless pyc": (lambda root, b: (b / "c.pyc").write_bytes(b""), "not_a_python_source"),
            "stray file": (lambda root, b: (b / ".DS_Store").write_bytes(b""), "not_a_python_source"),
            "temporal package": (lambda root, b: (root / "betangel_research" / "temporal").mkdir(),
                                 "shadows_temporal"),
            "too large": (lambda root, b: (b / "a.py").write_bytes(b"#" * (certificate.MAX_SOURCE_BYTES + 1)),
                          "a.py:too_large"),
            "one file too many": (lambda root, b: [(b / f"m{i}.py").write_text("")       # 5 files already
                                                   for i in range(certificate.MAX_SOURCE_FILES - 4)], "too_many_files"),
            "missing parent": (lambda root, b: (root / "betangel_research" / "temporal.py").unlink(),
                               "temporal.py:not_a_regular_file"),
            "symlinked temporal.py": (lambda root, b: (
                (root / "t.py").write_text(""), (root / "betangel_research" / "temporal.py").unlink(),
                (root / "betangel_research" / "temporal.py").symlink_to(root / "t.py")),
                "betangel_research/temporal.py:symlink"),
            "symlinked betfair": (lambda root, b: (
                (root / "betangel_research" / "betfair").rename(root / "real-betfair"),
                (root / "betangel_research" / "betfair").symlink_to(root / "real-betfair")), ":symlink"),
            "symlinked pycache": (lambda root, b: ((root / "cache").mkdir(),
                                                   (b / "__pycache__").symlink_to(root / "cache")),
                                  "backtest/__pycache__:symlink"),
            "a file named __pycache__": (lambda root, b: (b / "__pycache__").write_text(""), "not_a_python_source"),
            "temporal extension": (lambda root, b: (root / "betangel_research" / "temporal.cpython-314-darwin.so")
                                   .write_bytes(b""), "shadows_temporal"),
            "top initialiser extension": (
                lambda root, b: (root / "betangel_research" / "__init__.cpython-314-darwin.so").write_bytes(b""),
                "betangel_research/__init__.cpython-314-darwin.so:shadows_init"),
            "betfair initialiser extension": (
                lambda root, b: (root / "betangel_research" / "betfair" / "__init__.abi3.so").write_bytes(b""),
                "betfair/__init__.abi3.so:shadows_init"),
            "an initialiser shadow in capitals": (lambda root, b: (
                root / "betangel_research" / "betfair" / "__INIT__.ABI3.SO").write_bytes(b""), "shadows_init"),
            "a sourceless initialiser": (lambda root, b: (root / "betangel_research" / "betfair" / "__init__.pyc")
                                         .write_bytes(b""), "shadows_init"),
            "a stdlib module at the root": (lambda root, b: (root / "decimal.py").write_text(""),
                                            "source:decimal.py:shadows_stdlib"),
            "a stdlib package at the root": (lambda root, b: (root / "json").mkdir(), "source:json:shadows_stdlib"),
            "a stdlib extension at the root": (lambda root, b: (root / "Bz2.cpython-314-darwin.so").write_bytes(b""),
                                               "shadows_stdlib"),
        }
        for name, (change, reason) in cases.items():
            with self.subTest(case=name), self.assertRaisesRegex(CertificateProblem, reason):
                certificate.source_descriptor(layout(change))
            self.tmp = Path(tempfile.mkdtemp(dir=self.tmp)).resolve()

    def test_unreadable_sources_refuse_and_the_bounds_are_inclusive(self):
        root, backtest = self.fake_sources()
        (backtest / "a.py").chmod(0)
        try:
            with self.assertRaisesRegex(CertificateProblem, "backtest/a.py:unreadable"):
                certificate.source_descriptor(root)
        finally:
            (backtest / "a.py").chmod(0o644)
        backtest.chmod(0)
        try:
            with self.assertRaisesRegex(CertificateProblem, "source:betangel_research:unreadable"):
                certificate.source_descriptor(root)
        finally:
            backtest.chmod(0o755)
        (backtest / "a.py").write_bytes(b"#" * certificate.MAX_SOURCE_BYTES)
        for index in range(certificate.MAX_SOURCE_FILES - 5):
            (backtest / f"m{index}.py").write_text("")
        (root / "tests").mkdir()
        (root / "README.md").write_text("")
        descriptor, _ = certificate.source_descriptor(root)
        self.assertEqual(len(descriptor["files"]), certificate.MAX_SOURCE_FILES)

    def test_sources_are_read_with_a_bound(self):
        with recorded_reads() as sizes:
            certificate.source_descriptor()
        self.assertTrue(sizes and set(sizes) == {certificate.MAX_SOURCE_BYTES + 1})

    def test_the_interpreter_descriptor_is_this_interpreter(self):
        value = certificate.interpreter_descriptor()
        self.assertEqual(value, {"implementation": sys.implementation.name, "cache_tag": sys.implementation.cache_tag,
                                 "version_info": list(sys.version_info[:5]), "version": sys.version,
                                 "platform": sys.platform, "byteorder": sys.byteorder, "maxsize": sys.maxsize,
                                 "float_repr_style": sys.float_repr_style, "optimize": sys.flags.optimize})
        sentinel = SimpleNamespace(implementation=SimpleNamespace(name="impl", cache_tag="tag"),
                                   version_info=(9, 8, 7, "rc", 6, "past the fifth"), version="build", platform="os",
                                   byteorder="middle", maxsize=5, float_repr_style="long",
                                   flags=SimpleNamespace(optimize=7))
        with mock.patch.object(certificate, "sys", sentinel):
            self.assertEqual(certificate.interpreter_descriptor(),
                             {"implementation": "impl", "cache_tag": "tag", "version_info": [9, 8, 7, "rc", 6],
                              "version": "build", "platform": "os", "byteorder": "middle", "maxsize": 5,
                              "float_repr_style": "long", "optimize": 7})
        env = {key: value for key, value in os.environ.items() if key != "PYTHONOPTIMIZE"}
        for flag, level in (("-O", "1"), ("-OO", "2")):
            done = subprocess.run([sys.executable, "-B", flag, "-c", "from betangel_research.betfair.backtest import "
                                   "certificate; print(certificate.interpreter_descriptor()['optimize'])"],
                                  cwd=REPO, capture_output=True, text=True, env=env, timeout=60)
            self.assertEqual(done.stdout.strip(), level, done.stderr)

    def test_the_digests_carry_their_literal_tags(self):
        descriptor, digest = certificate.source_descriptor()
        self.assertEqual(digest, hashlib.sha256(b"betangel-research/backtest/source-descriptor/v1\x00"
                                                + canonical_json(descriptor)).hexdigest())
        registry = [[case_id, kind, counts] for case_id, kind, counts in certificate.CASES]
        self.assertEqual(certificate.registry_sha256(), hashlib.sha256(
            b"betangel-research/backtest/certification-registry/v1\x00" + canonical_json(registry)).hexdigest())
        interpreter = certificate.interpreter_descriptor()
        material = {"protocol_version": 1, "registry_sha256": certificate.registry_sha256(),
                    "criteria": certificate.CRITERIA, "profile": certificate.P0_PROFILE,
                    "parameters": certificate.PARAMETERS, "limits": certificate.LIMITS,
                    "source_descriptor_sha256": digest, "interpreter": interpreter}
        self.assertEqual(certificate.certificate_key(digest, interpreter), hashlib.sha256(
            b"betangel-research/backtest/certificate-key/v1\x00" + canonical_json(material)).hexdigest())


class ProfileTests(unittest.TestCase):
    def test_the_p0_contract_has_the_p0_profile(self):
        self.assertEqual(certificate.profile_of(p0_contract()), certificate.P0_PROFILE)

    def test_a_semantic_selector_leaves_the_profile(self):
        for dotted, value in (("staking.side", "LAY"), ("staking.price", "LIMIT"), ("population.tier", "PRO"),
                              ("population.countries", ["GB"]), ("population.evaluate_split", "research"),
                              ("decision_timing.delay_s", 1), ("decision_timing.anchor", "other"),
                              ("fitting.kind", "walk_forward"), ("settlement.dead_heat", "split"),
                              ("costs.basis", "gross"), ("uncertainty.block", "iso_week"),
                              ("registration.kind", "registered_hypothesis"),
                              ("capabilities.decision.required", ["ltp"]), ("secondary_metrics", ["coverage"])):
            with self.subTest(selector=dotted):
                self.assertNotEqual(certificate.profile_of(p0_contract(**{dotted: value})), certificate.P0_PROFILE)
        contract = p0_contract()
        contract["variants"][1]["uses_features"] = ["ltp_at_decision"]
        self.assertNotEqual(certificate.profile_of(contract), certificate.P0_PROFILE)
        contract = p0_contract()
        contract["features"][0]["lookback_s"] = 60
        self.assertNotEqual(certificate.profile_of(contract), certificate.P0_PROFILE)

    def test_every_other_leaf_moves_the_profile(self):
        base, inert = p0_contract(), set()
        for path, value in leaves(base):
            changed = copy.deepcopy(base)
            target = changed
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = other(value)
            if certificate.profile_of(changed) == certificate.P0_PROFILE:
                inert.add(".".join("[]" if isinstance(key, int) else key for key in path))
        self.assertEqual(inert, INERT_LEAVES)
        for name, change in (("a variant dropped", lambda c: c["variants"].pop()),
                             ("a feature added", lambda c: c["features"].append(dict(c["features"][0], name="x"))),
                             ("a country dropped", lambda c: c["population"]["countries"].pop()),
                             ("a criterion dropped", lambda c: c["engineering_acceptance"]["criteria"].pop())):
            with self.subTest(structure=name):
                changed = p0_contract()
                change(changed)
                self.assertNotEqual(certificate.profile_of(changed), certificate.P0_PROFILE)

    def test_the_published_domains_are_the_enforcing_constants(self):
        from betangel_research.betfair.backtest import accounting, contract
        domains = {name: item["domain"] for name, item in certificate.PARAMETERS.items()}
        self.assertEqual(domains["decision_timing.offset_s"], list(contract.OFFSET_RANGE_S))
        self.assertEqual(domains["staking.stake_pence"], list(contract.STAKE_RANGE_PENCE))
        self.assertEqual(domains["costs.rounding"], list(contract.ROUNDING))
        self.assertEqual(domains["seeds.selection"], list(contract.SEED_RANGE))
        self.assertEqual(domains["seeds.bootstrap"], list(contract.SEED_RANGE))
        self.assertEqual(domains["uncertainty.resamples"], list(contract.RESAMPLE_RANGE))
        self.assertEqual(domains["stopping_rule.max_trials"],
                         [len(certificate.P0_PROFILE["variants"]), contract.MAX_TRIALS_RANGE[1]])
        self.assertEqual(domains["costs.commission_rate"]["max_decimal_places"], accounting.MAX_RATE_PLACES)
        self.assertEqual(domains["population.splits"]["sealed_holdout"], list(contract.SEALED_HOLDOUT))
        self.assertEqual(sorted(domains), sorted(certificate.PARAMETERS))
        contract_problems = []
        for dotted, value in (("decision_timing.offset_s", contract.OFFSET_RANGE_S[0] - 1),
                              ("decision_timing.offset_s", contract.OFFSET_RANGE_S[1] + 1),
                              ("staking.stake_pence", contract.STAKE_RANGE_PENCE[1] + 1),
                              ("costs.commission_rate", 1.0), ("uncertainty.confidence_level", 0.0),
                              ("uncertainty.resamples", contract.RESAMPLE_RANGE[0] - 1), ("seeds.selection", -1)):
            problems, _ = contract.validate_contract(p0_contract(**{dotted: value}))
            contract_problems.append(bool(problems))
        self.assertTrue(all(contract_problems))

    def test_a_variable_parameter_or_a_text_does_not(self):
        for dotted, value in (("decision_timing.offset_s", -600), ("staking.stake_pence", 250),
                              ("costs.commission_rate", 0.02), ("costs.rounding", "per_market_half_up_penny"),
                              ("seeds.selection", 7), ("seeds.bootstrap", 3), ("uncertainty.resamples", 100),
                              ("uncertainty.confidence_level", 0.9), ("stopping_rule.max_trials", 4),
                              ("status", "frozen"), ("frozen_on", "2026-10-20"), ("experiment_id", "other"),
                              ("cohort.market_rule", "another text"),
                              ("costs.rate_source", "account_statement_verified")):
            with self.subTest(parameter=dotted):
                self.assertEqual(certificate.profile_of(p0_contract(**{dotted: value})), certificate.P0_PROFILE)

    def test_the_cases_run_under_the_profile_and_witness_the_parameters(self):
        points = (cf.p0_rules(), cf.second_point_rules())
        profile = certificate.P0_PROFILE
        for cohort, accounting, _ in points:
            self.assertEqual(list(cohort.variants), [item[0] for item in profile["variants"]])
            self.assertEqual((cohort.tier, list(cohort.countries), list(cohort.market_types), cohort.delay_s),
                             (profile["population"]["tier"], profile["population"]["countries"],
                              profile["population"]["market_types"], profile["decision_timing"]["delay_s"]))
            self.assertTrue(cohort.axis.startswith(profile["decision_timing"]["anchor"]))
            self.assertEqual(dict(accounting.settlement), profile["settlement"])
            self.assertEqual(list(accounting.variants), list(cohort.variants))
        witnessed = {name: item["witnessed"] for name, item in certificate.PARAMETERS.items()}
        self.assertEqual(witnessed["decision_timing.offset_s"], sorted(c.offset_s for c, _, _ in points))
        self.assertEqual(witnessed["staking.stake_pence"], sorted(c.stake_pence for c, _, _ in points))
        self.assertEqual(witnessed["seeds.selection"], sorted(c.selection_seed for c, _, _ in points))
        self.assertEqual(witnessed["costs.commission_rate"], sorted(str(a.commission_rate) for _, a, _ in points))
        self.assertEqual(witnessed["costs.rounding"], sorted(a.rounding for _, a, _ in points))
        self.assertEqual([tuple(split) for split in witnessed["population.splits"]],
                         sorted({(c.start_month, c.end_month) for c, _, _ in points}))
        self.assertEqual([Decimal(rate) for rate in witnessed["costs.commission_rate"]],
                         sorted(a.commission_rate for _, a, _ in points))


class KeyTests(unittest.TestCase):
    def test_the_key_moves_with_what_it_binds_and_nothing_else(self):
        interpreter = certificate.interpreter_descriptor()
        key = certificate.certificate_key("a" * 64, interpreter)
        self.assertEqual(certificate.certificate_key("a" * 64, dict(interpreter)), key)
        self.assertNotEqual(certificate.certificate_key("b" * 64, interpreter), key)
        for field in certificate.INTERPRETER_KEYS:
            with self.subTest(field=field):
                changed = dict(interpreter, **{field: "other"})
                self.assertNotEqual(certificate.certificate_key("a" * 64, changed), key)
        first = certificate.CASES[0]
        for name, value in (("P0_PROFILE", dict(certificate.P0_PROFILE, fitting="other")),
                            ("PARAMETERS", {}), ("LIMITS", dict(certificate.LIMITS, prefix_limit_bytes=1)),
                            ("CRITERIA", {}), ("PROTOCOL_VERSION", 2), ("CASES", certificate.CASES[:-1]),
                            ("CASES", ((first[0], first[1], {"kat_cases": 12}),) + certificate.CASES[1:]),
                            ("CASES", ((first[0], certificate.NEGATIVE, first[2]),) + certificate.CASES[1:])):
            with self.subTest(changed=name), mock.patch.object(certificate, name, value):
                self.assertNotEqual(certificate.certificate_key("a" * 64, interpreter), key)

    def test_current_key_is_the_key_of_these_sources_and_this_interpreter(self):
        key, descriptor, digest, interpreter = certificate.current_key()
        self.assertEqual((descriptor, digest), certificate.source_descriptor())
        self.assertEqual(key, certificate.certificate_key(digest, interpreter))


class EnvelopeTests(unittest.TestCase):
    def test_it_is_certified_only_for_exactly_the_registry_all_as_expected(self):
        good = built()
        self.assertEqual((good["status"], certificate.validate_certificate(good)), (certificate.CERTIFIED, []))
        self.assertEqual(good["counts"], {"cases": 38, "as_expected": 38, "not_as_expected": 0, "error": 0})
        descriptor, _ = certificate.source_descriptor()
        interpreter = certificate.interpreter_descriptor()
        for name, results in (
                ("one not as expected", canned({"kat": certificate.NOT_AS_EXPECTED})),
                ("one error", canned({"nc_ledger_byte": certificate.ERROR})),
                ("one missing", canned()[:-1]), ("one extra", canned() + canned()[:1]),
                ("reordered", canned()[1:] + canned()[:1]), ("empty", []),
                ("another kind", [dict(item, kind=certificate.CHECK) if item["id"] == "nc_future_leak" else item
                                  for item in canned()])):
            with self.subTest(case=name):
                value = certificate.build_certificate(results, descriptor, interpreter)
                self.assertEqual(value["status"], certificate.NOT_CERTIFIED)
                self.assertTrue(certificate.validate_certificate(value))

    def test_the_evidence_digests_and_counts_are_recomputed(self):
        results = canned({"kat": certificate.NOT_AS_EXPECTED, "nc_ledger_byte": certificate.ERROR})
        results[0] = dict(results[0], observed={"seen": 1}, expected={"wanted": 2})
        results[-13] = dict(results[-13], observed="RuntimeError:boom")
        descriptor, _ = certificate.source_descriptor()
        value = certificate.build_certificate(results, descriptor, certificate.interpreter_descriptor())
        for item, case in zip(results, value["cases"]):
            self.assertEqual((case["observed_sha256"], case["expected_sha256"]),
                             (hashlib.sha256(canonical_json(item["observed"])).hexdigest(),
                              hashlib.sha256(canonical_json(item["expected"])).hexdigest()))
        self.assertEqual(value["counts"], {"cases": 38, "as_expected": 36, "not_as_expected": 1, "error": 1})

    def test_shapes_hidden_behind_a_resealed_key_are_caught(self):
        good = built()

        def tamper(change):
            value = copy.deepcopy(good)
            change(value)
            return reseal(value)

        files = good["source"]["descriptor"]["files"]
        cases = {
            "an extra descriptor key": lambda v: v["source"]["descriptor"].update(extra=1),
            "temporal.py dropped": lambda v: v["source"]["descriptor"].update(
                files=[f for f in files if f["path"] != "betangel_research/temporal.py"]),
            "a duplicate path": lambda v: v["source"]["descriptor"].update(files=files + files[:1]),
            "a negative size": lambda v: v["source"]["descriptor"]["files"][0].update(size=-1),
            "a non-hex sha": lambda v: v["source"]["descriptor"]["files"][0].update(sha256="Z" * 64),
            "no files": lambda v: v["source"]["descriptor"].update(files=[]),
            "too many files": lambda v: v["source"]["descriptor"].update(
                files=sorted(files + [dict(files[0], path=f"x{i}.py") for i in range(64)], key=lambda f: f["path"])),
            "an extra interpreter key": lambda v: v["interpreter"].update(extra=1),
            "an extra case field": lambda v: v["cases"][0].update(extra=1),
            "a malformed expected digest": lambda v: v["cases"][0].update(expected_sha256="x"),
            "as expected with other digests": lambda v: v["cases"][0].update(observed_sha256="0" * 64),
            "a list result": lambda v: v["cases"][0].update(result=[]),
            "a dict result": lambda v: v["cases"][0].update(result={}),
            "a true schema version": lambda v: v.update(schema_version=True),
            "a float protocol version": lambda v: v.update(protocol_version=1.0),
            "float counts": lambda v: v["counts"].update(cases=38.0),
            "false error count": lambda v: v["counts"].update(error=False),
            "a case not as expected, counted, under a certified status": lambda v: (
                v["cases"][0].update(result=certificate.NOT_AS_EXPECTED),
                v.update(counts=certificate._counts(v["cases"]))),
        }
        for name, change in cases.items():
            with self.subTest(case=name):
                self.assertTrue(certificate.validate_certificate(tamper(change)))
        for value in (dict(good, source="x"), dict(good, interpreter="x"), dict(good, cases="x"),
                      dict(good, counts="x")):
            self.assertTrue(certificate.validate_certificate(value))

    def test_every_tampered_field_is_caught(self):
        good = built()
        other_descriptor = json.loads(json.dumps(good["source"]["descriptor"]))
        other_descriptor["files"][0]["size"] += 1
        tampered = {
            "kind": dict(good, kind="other"), "schema": dict(good, schema_version=2),
            "protocol": dict(good, protocol_version=2), "status": dict(good, status="other"),
            "registry": dict(good, registry_sha256="0" * 64), "criteria": dict(good, criteria={}),
            "profile": dict(good, profile=dict(good["profile"], fitting="other")),
            "parameters": dict(good, parameters={}), "limits": dict(good, limits={}),
            "descriptor": dict(good, source=dict(good["source"], descriptor=other_descriptor)),
            "source sha": dict(good, source=dict(good["source"], sha256="0" * 64)),
            "interpreter": dict(good, interpreter=dict(good["interpreter"], version="other")),
            "interpreter keys": dict(good, interpreter={}), "key": dict(good, key="0" * 64),
            "a result": dict(good, cases=[dict(good["cases"][0], result="not_as_expected")] + good["cases"][1:]),
            "a digest": dict(good, cases=[dict(good["cases"][0], observed_sha256="x")] + good["cases"][1:]),
            "a dropped case": dict(good, cases=good["cases"][1:]),
            "counts": dict(good, counts=dict(good["counts"], cases=39)),
            "an extra field": dict(good, extra=1), "a missing field": {k: v for k, v in good.items() if k != "counts"},
        }
        for name, value in tampered.items():
            with self.subTest(field=name):
                self.assertTrue(certificate.validate_certificate(value))
        self.assertEqual(certificate.validate_certificate(good, key=good["key"]), [])
        self.assertEqual(certificate.validate_certificate(good, key="0" * 64), ["certificate:not_this_key"])


class StorageTests(Workspace):
    def lab(self):
        lab = self.tmp / "lab"
        lab.mkdir()
        return certificate.lab_root(lab)

    def test_store_load_reuse_and_refuse(self):
        lab, good = self.lab(), built()
        outcome, path, digest = certificate.store_certificate(lab, good)
        self.assertEqual((outcome, path.name), (certificate.CERTIFIED, f"{good['key']}.json"))
        self.assertEqual(path.read_bytes(), certificate.certificate_bytes(good))
        self.assertEqual(certificate.load_certificate(lab, good["key"]), (good, digest))
        self.assertEqual(certificate.store_certificate(lab, good), ("already_certified", path, digest))
        path.chmod(0o600)
        path.write_bytes(certificate.certificate_bytes(dict(good, counts=dict(good["counts"]))) + b" ")
        kept = path.read_bytes()
        with self.assertRaisesRegex(CertificateProblem, "existing_differs"):
            certificate.store_certificate(lab, good)
        self.assertEqual(path.read_bytes(), kept)
        with self.assertRaisesRegex(CertificateProblem, "not_storable"):
            certificate.store_certificate(lab, built({"kat": certificate.ERROR}))

    def test_load_refuses_what_is_not_a_certified_certificate_for_the_key(self):
        lab, good = self.lab(), built()
        with self.assertRaisesRegex(CertificateProblem, "certification:missing"):
            certificate.load_certificate(lab, good["key"])
        _, path, _ = certificate.store_certificate(lab, good)
        path.chmod(0o600)
        for name, data, reason in (
                ("not canonical", json.dumps(good).encode(), "malformed:not_canonical"),
                ("not JSON", b"{", "malformed:not_json"),
                ("too large", b" " * (certificate.MAX_CERTIFICATE_BYTES + 1), "too_large"),
                ("not passed", certificate.certificate_bytes(dict(good, status="not_certified")), "not_all_passed"),
                ("tampered", certificate.certificate_bytes(dict(good, key="0" * 64)), "malformed:key")):
            with self.subTest(case=name):
                path.write_bytes(data)
                with self.assertRaisesRegex(CertificateProblem, reason):
                    certificate.load_certificate(lab, good["key"])
        path.write_bytes(certificate.certificate_bytes(good))
        other = path.with_name(f"{'0' * 64}.json")
        other.write_bytes(certificate.certificate_bytes(good))
        with self.assertRaisesRegex(CertificateProblem, "not_this_key"):
            certificate.load_certificate(lab, "0" * 64)
        with self.assertRaisesRegex(CertificateProblem, "key_malformed"):
            certificate.load_certificate(lab, "../x")

    def test_symlinks_and_other_kinds_of_file_are_never_followed(self):
        lab, good = self.lab(), built()
        _, path, _ = certificate.store_certificate(lab, good)
        elsewhere = self.tmp / "elsewhere.json"
        elsewhere.write_bytes(path.read_bytes())
        denied = self.tmp / "Library" / "Application Support" / "betangel-research" / "betfair"
        denied.mkdir(parents=True)
        (denied / "c.json").write_bytes(path.read_bytes())
        path.unlink()
        for name, make in (("to elsewhere", lambda: path.symlink_to(elsewhere)),
                           ("into a denied tree", lambda: path.symlink_to(denied / "c.json")),
                           ("a FIFO", lambda: os.mkfifo(path))):
            with self.subTest(file=name):
                make()
                with recorded_reads() as sizes:
                    with self.assertRaisesRegex(CertificateProblem, "certificate:not_a_regular_file"):
                        certificate.load_certificate(lab, good["key"])
                    with self.assertRaisesRegex(CertificateProblem, "certificate:not_a_regular_file"):
                        certificate.store_certificate(lab, good)
                self.assertEqual(sizes, [])
                path.unlink()
        (lab / "certifications").rename(lab / "moved")
        (lab / "certifications").symlink_to(lab / "moved")
        with self.assertRaisesRegex(CertificateProblem, "certifications:not_a_directory"):
            certificate.load_certificate(lab, good["key"])

    def test_storage_keeps_exclusive_create_and_its_error_mappings(self):
        lab, good = self.lab(), built()
        target = self.tmp / "raced.json"
        target.write_bytes(b"old")
        with mock.patch.object(certificate, "_guard", lambda path, where, prospective=False: path):
            with self.assertRaisesRegex(CertificateProblem, "^where:not_created$"):
                certificate.write_new(target, b"new", "where")
        self.assertEqual(target.read_bytes(), b"old")
        real_lstat = Path.lstat

        def failing(path, *args, **kwargs):
            if path.name.endswith(".json"):
                raise PermissionError("no")
            return real_lstat(path, *args, **kwargs)

        with mock.patch.object(Path, "lstat", failing), self.assertRaisesRegex(CertificateProblem,
                                                                                "certificate:uninspectable"):
            certificate.store_certificate(lab, good)
        (lab / "certifications").rmdir()                    # the failed store made it, empty
        (lab / "certifications").write_text("")
        with self.assertRaisesRegex(CertificateProblem, "certifications:not_a_directory"):
            certificate.store_certificate(lab, good)
        (lab / "certifications").unlink()
        with mock.patch.object(Path, "mkdir", mock.Mock(side_effect=PermissionError("no"))), \
                self.assertRaisesRegex(CertificateProblem, "certifications:unwritable"):
            certificate.store_certificate(lab, good)
        _, path, _ = certificate.store_certificate(lab, good)
        path.chmod(0)
        try:
            with self.assertRaisesRegex(CertificateProblem, "certificate:unreadable"):
                certificate.store_certificate(lab, good)
        finally:
            path.chmod(0o600)
        with recorded_reads() as sizes:
            certificate.load_certificate(lab, good["key"])
        self.assertEqual(sizes, [certificate.MAX_CERTIFICATE_BYTES + 1])

    def test_a_write_that_fails_after_its_file_exists_is_uncertain_and_kept(self):
        target = self.tmp / "new.json"
        with self.assertRaisesRegex(CertificateProblem, "where:uncertain"):
            certificate.write_new(target, "not bytes", "where")
        self.assertTrue(target.exists())
        with self.assertRaisesRegex(CertificateProblem, "already_exists"):
            certificate.write_new(target, b"x", "where")

    def test_a_symlinked_or_occupied_certifications_directory_refuses(self):
        lab = self.lab()
        (lab / "elsewhere").mkdir()
        (lab / certificate.CERTIFICATIONS_DIR).symlink_to(lab / "elsewhere")
        with self.assertRaisesRegex(CertificateProblem, "certifications:not_a_directory"):
            certificate.store_certificate(lab, built())


class RootTests(Workspace):
    """Astra's N1: the work root and the case directories' parent are checked before anything is written."""

    def bad_roots(self):
        repo = self.tmp / "repo"
        (repo / ".git").mkdir(parents=True)
        (repo / "work").mkdir()
        (self.tmp / "file").write_text("")
        denied = self.tmp / "Library" / "Application Support" / "betangel-research" / "betfair" / "work"
        denied.mkdir(parents=True)
        return {"inside a Git worktree": (repo / "work", "inside_git_worktree"),
                "this repository": (REPO, "inside_repository"), "missing": (self.tmp / "absent", "missing"),
                "a file": (self.tmp / "file", "not_a_directory"), "a denied tree": (denied, "denied_live_state_tree")}

    def test_run_cases_refuses_a_bad_work_root_with_nothing_written(self):
        for name, (root, reason) in self.bad_roots().items():
            before = sorted(root.iterdir()) if root.is_dir() and root != REPO else None
            with self.subTest(root=name), self.assertRaisesRegex(certify.CaseProblem, f"^work_root:{reason}"):
                certify.run_cases(root)
            if before is not None:
                self.assertEqual(sorted(root.iterdir()), before)

    def test_run_case_refuses_a_case_directory_that_completes_a_denied_tree(self):
        """report_f77a8fde: an allowed parent, but a final component that completes a denied live-state sequence."""
        for parts in ((".config", "betangel-research", "betfair"),
                      ("Library", "Application Support", "betangel-research", "betfair"),
                      (".CONFIG", "Betangel-Research", "BETFAIR")):
            with self.subTest(target="/".join(parts)):
                parent = self.tmp.joinpath(*parts[:-1])
                parent.mkdir(parents=True, exist_ok=True)
                before = sorted(self.tmp.rglob("*"))
                with self.assertRaisesRegex(certify.CaseProblem, "^case:denied_live_state_tree"):
                    certify.run_case("kat", parent / parts[-1])
                self.assertEqual(sorted(self.tmp.rglob("*")), before)
        allowed = self.tmp / ".config" / "betangel-research" / "other"
        self.assertEqual(certify.run_case("kat", allowed)["result"], certify.AS_EXPECTED)
        self.assertTrue(allowed.is_dir())
        with self.assertRaises(FileExistsError):                       # never reused, as before
            certify.run_case("kat", allowed)

    def test_run_case_refuses_a_bad_parent_with_nothing_written(self):
        for name, (root, reason) in self.bad_roots().items():
            with self.subTest(root=name), self.assertRaisesRegex(certify.CaseProblem, f"^case_root:{reason}"):
                certify.run_case("kat", root / "kat")
            self.assertFalse((root / "kat").exists())


class CertifyEngineTests(Workspace):
    def lab(self):
        lab = self.tmp / "lab"
        lab.mkdir()
        return lab

    def engine(self, lab, attempt="a1", results=None):
        with mock.patch.object(certify, "run_cases", lambda work_root: canned(results)):
            return certify.certify_engine(lab, attempt)

    def test_certified_then_already_certified(self):
        lab = self.lab()
        result, code = self.engine(lab)
        self.assertEqual((result["status"], code, result["problems"]), ("certified", 0, []))
        stored = Path(result["certificate_path"])
        self.assertEqual(stored, lab.resolve() / "certifications" / f"{result['key']}.json")
        attempt = Path(result["attempt"])
        self.assertEqual(attempt, lab.resolve() / "certifications" / "work" / "a1")
        self.assertEqual((attempt / "certificate.json").read_bytes(), stored.read_bytes())
        again, code = self.engine(lab, "a2")
        self.assertEqual((again["status"], code, again["certificate_sha256"]),
                         ("already_certified", 0, result["certificate_sha256"]))
        value, digest = certificate.load_certificate(certificate.lab_root(lab), result["key"])
        self.assertEqual(digest, result["certificate_sha256"])

    def test_not_certified_stores_nothing_but_keeps_the_attempt(self):
        lab = self.lab()
        result, code = self.engine(lab, results={"nc_ledger_byte": certificate.NOT_AS_EXPECTED})
        self.assertEqual((result["status"], code, result["failed_cases"]), ("not_certified", 4, ["nc_ledger_byte"]))
        self.assertEqual(sorted(p.name for p in (lab / "certifications").iterdir()), ["work"])
        self.assertTrue((Path(result["attempt"]) / "certificate.json").exists())

    def test_a_source_change_during_certification_is_not_certified(self):
        lab, real = self.lab(), certificate.source_descriptor
        calls = []

        def changing(root=None):
            calls.append(root)
            descriptor, digest = real(root)
            return (descriptor, digest) if len(calls) == 1 else (descriptor, "0" * 64)

        with mock.patch.object(certificate, "source_descriptor", changing):
            result, code = self.engine(lab)
        self.assertEqual((result["status"], code, result["problems"]),
                         ("not_certified", 4, ["source:changed_during_certification"]))
        self.assertEqual(sorted(p.name for p in (lab / "certifications").iterdir()), ["work"])

    def test_other_bytes_under_the_key_refuse_and_both_are_kept(self):
        lab = self.lab()
        first, _ = self.engine(lab)
        stored = Path(first["certificate_path"])
        stored.chmod(0o600)
        stored.write_bytes(stored.read_bytes() + b" ")
        kept = stored.read_bytes()
        result, code = self.engine(lab, "a2")
        self.assertEqual((result["status"], code, result["problems"]),
                         ("refused", 2, ["certification:existing_differs"]))
        self.assertEqual(stored.read_bytes(), kept)
        self.assertTrue((lab / "certifications" / "work" / "a2" / "certificate.json").exists())

    def test_refusals_before_anything_is_written(self):
        lab = self.lab()
        repo = self.tmp / "repo"
        (repo / ".git").mkdir(parents=True)
        (repo / "lab").mkdir()
        for name, root, attempt, reason in (
                ("lab root in a Git worktree", repo / "lab", "a1", "lab_state_root:inside_git_worktree"),
                ("missing lab root", self.tmp / "absent", "a1", "lab_state_root:missing_path"),
                ("malformed attempt id", lab, "../a", "attempt_id:malformed"),
                ("empty attempt id", lab, "", "attempt_id:malformed")):
            with self.subTest(case=name):
                result, code = self.engine(root, attempt)
                self.assertEqual((result["status"], code, result["problems"]), ("refused", 2, [reason]))
                self.assertFalse((root / "certifications").exists())
        with mock.patch.object(certify, "LOADED", certify.LOADED + (mock.Mock(__file__="/elsewhere/x.py"),)):
            result, code = self.engine(lab)
        self.assertEqual((result["status"], code, result["problems"]), ("refused", 2, ["source:not_this_checkout"]))
        self.assertFalse((lab / "certifications").exists())

    def test_the_sources_are_hashed_again_after_the_cases_and_the_attempt_says_so(self):
        lab, real, ran = self.lab(), certificate.source_descriptor, []

        def changed_after_the_cases(root=None):
            descriptor, digest = real(root)
            return (descriptor, "0" * 64) if ran else (descriptor, digest)

        def cases(work_root):
            ran.append(work_root)
            return canned()

        with mock.patch.object(certificate, "source_descriptor", changed_after_the_cases), \
                mock.patch.object(certify, "run_cases", cases):
            result, code = certify.certify_engine(lab, "a1")
        self.assertEqual((result["status"], code, result["problems"]),
                         ("not_certified", 4, ["source:changed_during_certification"]))
        self.assertEqual(ran, [lab.resolve() / "certifications" / "work" / "a1" / "cases"])
        attempt = json.loads((Path(result["attempt"]) / "certificate.json").read_bytes())
        self.assertEqual(attempt["status"], certificate.NOT_CERTIFIED)
        self.assertEqual(certificate.validate_certificate(attempt), ["certificate:not_all_passed"])

    def test_sources_changed_since_import_refuse(self):
        lab = self.lab()
        descriptor, _ = certificate.source_descriptor()
        for imported, reason in (((descriptor, "0" * 64), "source:changed_since_import"),
                                 (CertificateProblem("source:x:symlink"), "source:x:symlink")):
            with self.subTest(reason=reason), mock.patch.object(certify, "_IMPORTED", imported):
                result, code = self.engine(lab)
                self.assertEqual((result["status"], code, result["problems"]), ("refused", 2, [reason]))
                self.assertFalse((lab / "certifications").exists())

    def test_every_loaded_module_must_be_a_described_source(self):
        lab, root = self.lab(), certificate.source_root()
        namespace = {}
        exec(compile("def guard_path(*a, **k):\n    return None\n", "/elsewhere/path_guard.py", "exec"), namespace)
        for name, patch, reason in (
                ("an undescribed module", mock.patch.object(certify, "LOADED", certify.LOADED + (mock.Mock(
                    __file__=str(root / "betangel_research" / "betfair" / "gateway.py")),)),
                 "source:betangel_research/betfair/gateway.py:not_described"),
                ("certify elsewhere", mock.patch.object(certify, "__file__", "/elsewhere/certify.py"),
                 "source:not_this_checkout"),
                ("path_guard elsewhere", mock.patch.object(certify, "guard_path", namespace["guard_path"]),
                 "source:not_this_checkout")):
            with self.subTest(case=name), patch:
                result, code = self.engine(lab)
                self.assertEqual((result["status"], code, result["problems"]), ("refused", 2, [reason]))
        self.assertFalse((lab / "certifications").exists())
        done = subprocess.run([sys.executable, "-B", "-c", "import sys; from betangel_research.betfair.backtest "
                               "import certify; print(sorted(m for m in sys.modules if m.split('.')[0] == "
                               "'betangel_research'))"], cwd=REPO, capture_output=True, text=True, timeout=60)
        loaded = set(json.loads(done.stdout.replace("'", '"')))
        checked = {module.__name__ for module in certify.LOADED} | {certify.__name__, "betangel_research",
                                                                     "betangel_research.betfair",
                                                                     "betangel_research.betfair.backtest.path_guard"}
        self.assertTrue(loaded <= checked, loaded - checked)

    def test_derived_directories_are_checked_before_anything_is_written(self):
        for name, layout, reason in (
                ("a symlinked certifications directory", lambda lab: (
                    (lab / "target").mkdir(), (lab / "certifications").symlink_to(lab / "target")),
                 "certifications:not_a_directory"),
                ("a symlinked work directory", lambda lab: (
                    (lab / "target").mkdir(), (lab / "certifications").mkdir(),
                    (lab / "certifications" / "work").symlink_to(lab / "target")),
                 "certification_work:not_a_directory"),
                ("a Git marker under certifications",
                 lambda lab: (lab / "certifications" / ".git").mkdir(parents=True),
                 "certifications:inside_git_worktree")):
            with self.subTest(layout=name):
                lab = self.tmp / name.replace(" ", "-")
                lab.mkdir()
                layout(lab)
                before = sorted(str(p) for p in lab.rglob("*"))
                result, code = self.engine(lab)
                self.assertEqual((result["status"], code, result["problems"]), ("refused", 2, [reason]))
                self.assertEqual(sorted(str(p) for p in lab.rglob("*")), before)

    def test_outcomes_keep_their_names(self):
        lab = self.lab()
        with mock.patch.object(certificate, "write_new",
                               mock.Mock(side_effect=CertificateProblem("attempt_certificate:uncertain"))):
            result, code = self.engine(lab)
        self.assertEqual((result["status"], code, result["problems"]),
                         ("uncertain", 2, ["attempt_certificate:uncertain"]))
        with mock.patch.dict(certify.RUNNERS, {"kat": None}):
            del certify.RUNNERS["kat"]
            result, code = certify.certify_engine(lab, "a2")
        self.assertEqual((result["status"], code, result["problems"]), ("refused", 2, ["certify:registry_mismatch"]))

    def test_an_attempt_id_is_never_reused(self):
        lab = self.lab()
        self.engine(lab, results={"kat": certificate.ERROR})
        result, code = self.engine(lab)
        self.assertEqual((result["status"], code, result["problems"]), ("refused", 2, ["attempt:exists"]))

    def test_an_unexpected_exception_is_a_failure_never_a_certificate(self):
        lab = self.lab()
        with mock.patch.object(certify, "run_cases", mock.Mock(side_effect=RuntimeError("boom"))):
            result, code = certify.certify_engine(lab, "a1")
        self.assertEqual((result["status"], code, result["problems"]), ("failed", 2, ["RuntimeError:boom"]))
        self.assertEqual([p.name for p in (lab / "certifications").iterdir()], ["work"])


class CliTests(Workspace):
    def test_the_command_prints_one_json_line_and_exits_with_its_code(self):
        for status, code in (("certified", 0), ("already_certified", 0), ("not_certified", 4), ("refused", 2)):
            out = io.StringIO()
            with self.subTest(status=status), contextlib.redirect_stdout(out), mock.patch.object(
                    certify, "certify_engine", return_value=({"status": status}, code)) as engine:
                self.assertEqual(cli.main(["certify-engine", "--lab-state-root", "L", "--attempt-id", "a"]), code)
                engine.assert_called_once_with("L", "a")
                self.assertEqual(json.loads(out.getvalue()), {"status": status})

    def test_the_real_certifier_and_loader_agree_in_a_fresh_process(self):
        """The integration control (Astra's control B): the real certify-engine in its own process, then the
        loader here, on the same interpreter and sources."""
        lab = self.tmp / "lab"
        lab.mkdir()
        done = subprocess.run([sys.executable, "-B", "-m", "betangel_research.betfair.backtest", "certify-engine",
                               "--lab-state-root", str(lab), "--attempt-id", "fresh"], cwd=REPO,
                              capture_output=True, text=True, env=dict(ENV, PATH="/usr/bin:/bin"), timeout=600)
        result = json.loads(done.stdout)
        self.assertEqual((done.returncode, result["status"], result["counts"]["as_expected"]), (0, "certified", 38))
        key = certificate.current_key()[0]
        value, digest = certificate.load_certificate(certificate.lab_root(lab), key)
        self.assertEqual((result["key"], result["certificate_sha256"]), (key, digest))
        self.assertEqual(value["status"], certificate.CERTIFIED)


if __name__ == "__main__":
    unittest.main()
