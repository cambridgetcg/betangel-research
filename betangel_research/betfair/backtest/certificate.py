"""Engine certificate (task c2b3b2b): the protocol's case registry, the source and interpreter descriptors, the
structural P0 profile and its published parameters, the binding key, the certificate envelope, its validation, and
its keyed storage and loading. This is the only certification module the runner imports (task c2b3b3); it imports
neither certify nor the runner, and runs no case. certify.certify_engine builds a certificate from certify.run_cases
and stores it here.

A certificate is a local reproducibility receipt (Astra's decisions 3 to 5 and control B on report_8cc414cf): the
engine at these exact sources, run by this exact interpreter under these limits, gave every registered case its
hand-labelled answer. It does not attest the loaded bytecode, native libraries, the standard library or a hostile
process, and its fixtures are finite witnesses, not proof for every parameter value.

Registry (CASES): the protocol's cases, each (id, kind, the exact counts its projection reports); certify runs
them. A certificate is certified only when its cases are exactly these, in this order, each with its kind and
result as_expected.

Source descriptor: {kind, version 1, files: [{path, size, sha256}] sorted by path}, over every *.py directly in
betangel_research/betfair/backtest and PARENT_SOURCES (the two parent package initialisers and temporal.py, the one
module outside the package the backtest imports; a boundaries test pins this set to the static import closure). It
refuses (CertificateProblem source:<path>:<reason>) a symlink anywhere on those paths, a subdirectory of the backtest
package other than a plain __pycache__ directory, any other non-.py entry there (an extension module, a sourceless
.pyc or a stray file would be loaded or would hide one), anything beside a parent initialiser or temporal.py that the
importer would load instead (a temporal package, an __init__ or temporal extension or .pyc, compared without case, as
macOS file systems ignore it), an entry at the checkout root named like a standard-library module (the checkout root
is sys.path[0] under "python3 -m" from it, so such a file would replace that module: the boundaries test's "./" rule),
more than MAX_SOURCE_FILES files, a file over MAX_SOURCE_BYTES and anything unreadable. Its digest is
sha256(DESCRIPTOR_TAG + canonical JSON). Outside it, under decision 5's disclosed limit: bytecode in __pycache__ (or
under PYTHONPYCACHEPREFIX) that does not match its source, another working directory, PYTHONPATH, site or user
packages, .pth or sitecustomize files, the standard library and native libraries.

Interpreter descriptor: implementation, cache tag, version_info, the full sys.version build string, platform,
byteorder, maxsize, float_repr_style and the optimize flag (an -O run drops asserts). A certificate from one
interpreter is not another's (decision 5: certify on the lab, in the runtime that will run).

Profile: P0_PROFILE holds every semantic selector of the P0 contract (profile_of reads the same selectors from a
contract): registration kind, population (provider, tier, sport, countries, market types, evaluated split), the
engineering criteria and rule, decision timing (anchor, view, delay, equal-pt order, image handling), capabilities,
features (name, capability, as of, lookback), fitting, staking (side, price, acceptance, impact), settlement,
commission basis, metrics, uncertainty method and the variants (id, role, features). A contract whose profile differs
in any of them is outside this certificate (decision 3). PARAMETERS publishes the variable parameters a contract may
set without leaving the profile, each with its domain (from the constants that enforce it) and the values the cases
witness; two points are witnesses, not proof. Texts, ids, status, dates, schema_version, financial_rule.kind (fixed by
the registration kind), costs.rate_source (provenance) and the holdout's state are neither: no engine module reads
them (a test walks every leaf of the P0 contract to keep this list exact).

Key: the tagged sha256 of {protocol version, registry digest, criteria, profile, parameters, limits, source
descriptor digest, interpreter descriptor}: no clock, path or attempt id, so the same sources, interpreter and
cases give the same key and the same certificate bytes (libbz2 permitting: a different library may give different
fixture bytes, and so a different certificate for the same key, which storage refuses as existing_differs).

Certificate: canonical JSON {kind, schema_version, protocol_version, key, status, registry_sha256, criteria,
profile, parameters, limits, source {descriptor, sha256}, interpreter, cases [{id, kind, result,
observed_sha256, expected_sha256}], counts}. status is certified or not_certified; only a certified one is stored.

Storage: <lab-state-root>/certifications/<key>.json, written by exclusive create after guarding each path (a
failure after the file exists is uncertain, and the partial file is left as evidence). The same bytes already there
are already_certified; other bytes refuse as existing_differs and both are kept. Both store and load refuse a
certifications directory or certificate that is a symlink or not a plain directory or regular file (a keyed lookup
never reads elsewhere, and a FIFO never blocks it). load_certificate reads at most MAX_CERTIFICATE_BYTES, requires
canonical bytes and validates the whole envelope against the registry and the key asked for; it never trusts a
certificate's own totals or status: every value is compared by its canonical bytes (so true is not 1), every
as_expected case must show observed and expected digests equal, and the counts are recomputed.

Standard library only; nothing here reads an account, a home directory, a clock or real data.
"""

from dataclasses import asdict
import hashlib
from pathlib import Path
import sys

from .accounting import MAX_RATE_PLACES
from .archive import DEFAULT_LIMITS, check_data_root
from .contract import (ENGINEERING_CRITERIA, EXPOSED_MONTHS, INSPECTED_MONTHS, MAX_TRIALS_RANGE, NOT_PROSPECTIVE_DATES,
                       OFFSET_RANGE_S, RESAMPLE_RANGE, ROUNDING, SEALED_HOLDOUT, SEED_RANGE, STAKE_RANGE_PENCE,
                       canonical_json)
from .engineering import PREFIX_LIMIT_BYTES, PREFIX_METHOD
from .market_index import loads_strict
from .path_guard import PathDenied, guard_path

CERTIFICATE_KIND, SCHEMA_VERSION, PROTOCOL_VERSION = "betangel_backtest_engine_certificate", 1, 1
DESCRIPTOR_KIND, DESCRIPTOR_VERSION = "betangel_backtest_source_descriptor", 1
DESCRIPTOR_TAG = b"betangel-research/backtest/source-descriptor/v1\x00"
REGISTRY_TAG = b"betangel-research/backtest/certification-registry/v1\x00"
KEY_TAG = b"betangel-research/backtest/certificate-key/v1\x00"
CERTIFIED, NOT_CERTIFIED = "certified", "not_certified"
BACKTEST_DIR = ("betangel_research", "betfair", "backtest")
PARENT_SOURCES = ("betangel_research/__init__.py", "betangel_research/betfair/__init__.py",
                  "betangel_research/temporal.py")
MAX_SOURCE_FILES, MAX_SOURCE_BYTES = 64, 4 << 20
MAX_CERTIFICATE_BYTES = 4 << 20
CERTIFICATIONS_DIR, WORK_DIR = "certifications", "work"

CHECK, NEGATIVE, INCOMPLETE = "check", "negative_control", "incomplete_control"
AS_EXPECTED, NOT_AS_EXPECTED, ERROR = "as_expected", "not_as_expected", "error"
# The protocol's case registry: (id, kind, the exact counts the observed projection must report).
CASES = (
    ("kat", CHECK, {"kat_cases": 13}),
    ("scenario_full", CHECK, {"tally_records": 22, "tally_compared": 19, "markets": 22}),
    ("scenario_cut", CHECK, {"compared_markets": 20, "changed_outcomes": 19}),
    ("scenario_deleted", CHECK, {"compared_markets": 20, "changed_outcomes": 17}),
    ("scenario_revised", CHECK, {"compared_markets": 20, "changed_outcomes": 16}),
    ("second_point", CHECK, {"markets": 22}),
    ("settlement_witness", CHECK, {"points": 2}),
    ("market_conditions", CHECK, {"markets": 28}),
    ("sources_named_file_decides", CHECK, {}),
    ("sources_primary_rules", CHECK, {"worlds": 6}),
    ("membership_gates", CHECK, {"refused_members": 2}),
    ("duplicate_across_archives", NEGATIVE, {}),
    ("limit_small_exact", CHECK, {}),
    ("limit_small_over", CHECK, {}),
    ("limit_batch_exact", CHECK, {}),
    ("limit_batch_inside", CHECK, {}),
    ("limit_64mib_exact", CHECK, {}),
    ("limit_64mib_over", CHECK, {}),
    ("read_limits_exact", CHECK, {}),
    ("read_limits_unexercised", CHECK, {}),
    ("abstaining_incomplete", INCOMPLETE, {}),
    ("unexercised_incomplete", INCOMPLETE, {}),
    ("nc_future_leak", NEGATIVE, {}),
    ("nc_pre_t_change", NEGATIVE, {}),
    ("nc_borrowed_rows", NEGATIVE, {}),
    ("nc_ledger_byte", NEGATIVE, {}),
    ("nc_swapped_bets", NEGATIVE, {}),
    ("nc_dropped_decision", NEGATIVE, {}),
    ("nc_short_settlement", NEGATIVE, {}),
    ("nc_decision_without_record", NEGATIVE, {}),
    ("nc_duplicate_record", NEGATIVE, {}),
    ("nc_other_variant_row", NEGATIVE, {}),
    ("nc_settlement_other_variants", NEGATIVE, {}),
    ("nc_wrong_decision_time", NEGATIVE, {}),
    ("nc_member_tampered", NEGATIVE, {}),
    ("nc_markets_changed", NEGATIVE, {}),
    ("nc_same_key_substitution", NEGATIVE, {}),
    ("nc_rejected_tally_addition", NEGATIVE, {}),
)
CASE_IDS = tuple(case_id for case_id, _, _ in CASES)

# The structural P0 profile (module docstring): research/experiments/backtest-p0-bsp-v1.json's selectors, with the
# delay P0 freezes (0; the draft leaves it null and the runner refuses any other).
P0_PROFILE = {
    "registration": "engineering_check",
    "population": {"provider": "betfair_historical_data", "tier": "BASIC", "sport": "horse_racing",
                   "countries": ["GB", "IE"], "market_types": ["WIN"], "evaluate_split": "engineering"},
    "engineering_acceptance": {"criteria": sorted(ENGINEERING_CRITERIA),
                               "rule": "all_criteria_must_hold_outcome_metrics_not_judged"},
    "decision_timing": {"anchor": "first_published_scheduled_off", "view": "observed_prefix_only", "delay_s": 0,
                        "equal_pt_order": "timer_after_equal_pt_batch", "image_handling": "img_replaces_market_state"},
    "capabilities": {"decision": {"required": ["ltp", "market_definition"], "on_missing": "unsupported_abstain"},
                     "outcome": {"required": ["final_bsp", "results"], "on_missing": "unresolved_settlement"}},
    "features": [["active_runner_count", "market_definition", "decision_time", 0],
                 ["ltp_at_decision", "ltp", "decision_time", 0]],
    "fitting": "none",
    "staking": {"side": "BACK", "price": "BSP", "acceptance": "unconditional_pre_off",
                "market_impact": "assumed_negligible"},
    "settlement": {"dead_heat": "unresolved", "market_void": "unresolved", "missing_bsp_or_result": "unresolved",
                   "other_runner_removed_after_decision": "unresolved",
                   "own_runner_removed_after_decision": "unresolved", "result_revised_after_close": "unresolved"},
    "costs": {"basis": "positive_net_market_winnings"},
    "primary_metric": ["roi", "sum_net_over_sum_stakes_resolved"],
    "secondary_metrics": ["abstentions_by_reason", "bet_count", "coverage", "ltp_to_bsp_movement_non_executable",
                          "max_drawdown_global_chronology", "net_pnl", "roi_worst_case_unresolved_lost",
                          "total_stakes", "unresolved_exposure"],
    "uncertainty": {"method": "block_bootstrap", "net_by": "market", "block": "race_day"},
    "variants": [["ltp_favourite", "candidate", ["ltp_at_decision"]], ["seeded_random_runner", "baseline", []]],
}
# The variable parameters (module docstring): each with its domain and the values the certification cases witness
# (certification_fixtures.p0_rules and second_point_rules); an empty list is a parameter no case witnesses.
PARAMETERS = {
    "costs.commission_rate": {"domain": {"type": "json_float", "min": 0.0, "min_inclusive": True, "max": 1.0,
                                         "max_inclusive": False, "max_decimal_places": MAX_RATE_PLACES},
                              "witnessed": ["0.02", "0.05"]},
    "costs.rounding": {"domain": list(ROUNDING), "witnessed": sorted(ROUNDING)},
    "decision_timing.offset_s": {"domain": list(OFFSET_RANGE_S), "witnessed": [-600, -300]},
    "population.splits": {"domain": {"sealed_holdout": list(SEALED_HOLDOUT), "inspected_months": list(INSPECTED_MONTHS),
                                     "exposed_months": sorted(EXPOSED_MONTHS),
                                     "not_prospective_dates": sorted(NOT_PROSPECTIVE_DATES)},
                          "witnessed": [["2015-05", "2016-04"]]},
    "seeds.bootstrap": {"domain": list(SEED_RANGE), "witnessed": []},
    "seeds.selection": {"domain": list(SEED_RANGE), "witnessed": [1, 7]},
    "staking.stake_pence": {"domain": list(STAKE_RANGE_PENCE), "witnessed": [100, 250]},
    "stopping_rule.max_trials": {"domain": [len(P0_PROFILE["variants"]), MAX_TRIALS_RANGE[1]], "witnessed": []},
    "uncertainty.confidence_level": {"domain": {"type": "json_float", "min": 0.0, "min_inclusive": False, "max": 1.0,
                                                "max_inclusive": False}, "witnessed": []},
    "uncertainty.resamples": {"domain": list(RESAMPLE_RANGE), "witnessed": []},
}
LIMITS = {"read": asdict(DEFAULT_LIMITS), "prefix_limit_bytes": PREFIX_LIMIT_BYTES}
CRITERIA = {"engineering": sorted(ENGINEERING_CRITERIA), "prefix_method": PREFIX_METHOD}
CERTIFICATE_KEYS = ("cases", "counts", "criteria", "interpreter", "key", "kind", "limits", "parameters", "profile",
                    "protocol_version", "registry_sha256", "schema_version", "source", "status")
INTERPRETER_KEYS = ("byteorder", "cache_tag", "float_repr_style", "implementation", "maxsize", "optimize",
                    "platform", "version", "version_info")


# Standard-library module names, casefolded, that an entry at the checkout root may not take (module docstring).
STDLIB_FOLDED = frozenset(name.casefold() for name in sys.stdlib_module_names)
# Each parent directory and the stems of the described modules in it that nothing beside them may shadow.
SHADOWED_STEMS = {("betangel_research",): ("__init__", "temporal"), ("betangel_research", "betfair"): ("__init__",)}


class CertificateProblem(Exception):
    """A certificate, source or storage problem; str() is "<where>:<reason>"."""


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def _is_sha256(value):
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


# --- descriptors ----------------------------------------------------------------------------------------------------

def source_root():
    """The directory holding the betangel_research package these modules were loaded from."""
    return Path(__file__).resolve().parents[3]


def _source_file(root, relative):
    """{path, size, sha256} of one source file: a regular file, no symlink on the way below root, bounded."""
    where = f"source:{relative}"
    place = root
    try:
        for part in relative.split("/"):
            place = place / part
            if place.is_symlink():
                raise CertificateProblem(f"{where}:symlink")
        if not place.is_file():
            raise CertificateProblem(f"{where}:not_a_regular_file")
        with place.open("rb") as handle:
            data = handle.read(MAX_SOURCE_BYTES + 1)
    except OSError:
        raise CertificateProblem(f"{where}:unreadable") from None
    if len(data) > MAX_SOURCE_BYTES:
        raise CertificateProblem(f"{where}:too_large")
    return {"path": relative, "size": len(data), "sha256": _sha256(data)}


def source_descriptor(root=None):
    """(descriptor, its tagged sha256) of the sources under root (default: source_root()); CertificateProblem as the
    module docstring says."""
    root = source_root() if root is None else Path(root)
    backtest = root.joinpath(*BACKTEST_DIR)
    relatives = []
    try:
        for entry in sorted(backtest.iterdir(), key=lambda path: path.name):
            relative = "/".join((*BACKTEST_DIR, entry.name))
            if entry.is_symlink():
                raise CertificateProblem(f"source:{relative}:symlink")
            if entry.name == "__pycache__" and entry.is_dir():           # a plain directory (not a symlink: above)
                continue
            if entry.is_dir():
                raise CertificateProblem(f"source:{relative}:subdirectory")
            if not entry.name.endswith(".py"):
                raise CertificateProblem(f"source:{relative}:not_a_python_source")
            relatives.append(relative)
        for parts, stems in SHADOWED_STEMS.items():
            for entry in sorted(root.joinpath(*parts).iterdir(), key=lambda path: path.name):
                folded = entry.name.casefold()
                for stem in stems:
                    if (folded == stem or folded.startswith(stem + ".")) and entry.name != f"{stem}.py":
                        raise CertificateProblem(f"source:{'/'.join((*parts, entry.name))}:shadows_{stem.strip('_')}")
        for entry in sorted(root.iterdir(), key=lambda path: path.name):
            name, dot, suffix = entry.name.partition(".")
            importable = (not dot and entry.is_dir()) or suffix.casefold() in ("py", "pyc") or \
                suffix.casefold().endswith(("so", "pyd"))
            if importable and name.casefold() in STDLIB_FOLDED:
                raise CertificateProblem(f"source:{entry.name}:shadows_stdlib")
    except OSError:
        raise CertificateProblem("source:betangel_research:unreadable") from None
    relatives += PARENT_SOURCES
    if len(relatives) > MAX_SOURCE_FILES:
        raise CertificateProblem("source:too_many_files")
    descriptor = {"kind": DESCRIPTOR_KIND, "version": DESCRIPTOR_VERSION,
                  "files": sorted((_source_file(root, relative) for relative in relatives),
                                  key=lambda item: item["path"])}
    return descriptor, descriptor_sha256(descriptor)


def descriptor_sha256(descriptor):
    return _sha256(DESCRIPTOR_TAG + canonical_json(descriptor))


def _descriptor_problem(descriptor):
    """None, or why a descriptor does not have the shape source_descriptor gives."""
    if not (isinstance(descriptor, dict) and sorted(descriptor) == ["files", "kind", "version"]
            and (descriptor["kind"], descriptor["version"]) == (DESCRIPTOR_KIND, DESCRIPTOR_VERSION)
            and isinstance(descriptor["files"], list) and 0 < len(descriptor["files"]) <= MAX_SOURCE_FILES):
        return "shape"
    paths = []
    for item in descriptor["files"]:
        if not (isinstance(item, dict) and sorted(item) == ["path", "sha256", "size"]
                and isinstance(item["path"], str) and type(item["size"]) is int
                and 0 <= item["size"] <= MAX_SOURCE_BYTES and _is_sha256(item["sha256"])):
            return "file"
        paths.append(item["path"])
    if paths != sorted(set(paths)) or not set(PARENT_SOURCES) <= set(paths):
        return "paths"
    return None


def interpreter_descriptor():
    """The running interpreter (module docstring)."""
    return {"implementation": sys.implementation.name, "cache_tag": sys.implementation.cache_tag,
            "version_info": list(sys.version_info[:5]), "version": sys.version, "platform": sys.platform,
            "byteorder": sys.byteorder, "maxsize": sys.maxsize, "float_repr_style": sys.float_repr_style,
            "optimize": sys.flags.optimize}


# --- profile ---------------------------------------------------------------------------------------------------------

def profile_of(contract):
    """The structural profile (module docstring) of a contract that validates; KeyError or TypeError otherwise."""
    population, timing, capabilities = contract["population"], contract["decision_timing"], contract["capabilities"]
    acceptance, metric, uncertainty = (contract["engineering_acceptance"], contract["primary_metric"],
                                       contract["uncertainty"])
    return {
        "registration": contract["registration"]["kind"],
        "population": {"provider": population["provider"], "tier": population["tier"], "sport": population["sport"],
                       "countries": sorted(population["countries"]),
                       "market_types": sorted(population["market_types"]),
                       "evaluate_split": population["evaluate_split"]},
        "engineering_acceptance": {"criteria": sorted(item["name"] for item in acceptance["criteria"]),
                                   "rule": acceptance["rule"]},
        "decision_timing": {key: timing[key] for key in ("anchor", "view", "delay_s", "equal_pt_order",
                                                         "image_handling")},
        "capabilities": {kind: {"required": sorted(capabilities[kind]["required"]),
                                "on_missing": capabilities[kind]["on_missing"]} for kind in ("decision", "outcome")},
        "features": sorted([item["name"], item["capability"], item["as_of"], item["lookback_s"]]
                           for item in contract["features"]),
        "fitting": contract["fitting"]["kind"],
        "staking": {key: contract["staking"][key] for key in ("side", "price", "acceptance", "market_impact")},
        "settlement": dict(sorted(contract["settlement"].items())),
        "costs": {"basis": contract["costs"]["basis"]},
        "primary_metric": [metric["name"], metric["definition"]],
        "secondary_metrics": sorted(contract["secondary_metrics"]),
        "uncertainty": {key: uncertainty[key] for key in ("method", "net_by", "block")},
        "variants": sorted([item["id"], item["role"], sorted(item["uses_features"] or [])]
                           for item in contract["variants"]),
    }


# --- key and envelope ------------------------------------------------------------------------------------------------

def registry_sha256():
    return _sha256(REGISTRY_TAG + canonical_json([[case_id, kind, counts] for case_id, kind, counts in CASES]))


def certificate_key(source_sha256, interpreter):
    """The binding key (module docstring) for these sources and this interpreter."""
    material = {"protocol_version": PROTOCOL_VERSION, "registry_sha256": registry_sha256(), "criteria": CRITERIA,
                "profile": P0_PROFILE, "parameters": PARAMETERS, "limits": LIMITS,
                "source_descriptor_sha256": source_sha256, "interpreter": interpreter}
    return _sha256(KEY_TAG + canonical_json(material))


def current_key(root=None):
    """(key, source descriptor, its sha256, interpreter descriptor) for the sources under root and this process."""
    descriptor, source_sha256 = source_descriptor(root)
    interpreter = interpreter_descriptor()
    return certificate_key(source_sha256, interpreter), descriptor, source_sha256, interpreter


def _digest(value):
    return _sha256(canonical_json(value))


def build_certificate(results, descriptor, interpreter):
    """The certificate of one certify.run_cases result list ([{id, kind, result, observed, expected}]) for these
    sources and this interpreter: certified only when the results are exactly the registry's cases, in order, each
    with its kind and as_expected."""
    source_sha256 = descriptor_sha256(descriptor)
    cases = [{"id": item["id"], "kind": item["kind"], "result": item["result"],
              "observed_sha256": _digest(item["observed"]), "expected_sha256": _digest(item["expected"])}
             for item in results]
    certificate = {"kind": CERTIFICATE_KIND, "schema_version": SCHEMA_VERSION, "protocol_version": PROTOCOL_VERSION,
                   "key": certificate_key(source_sha256, interpreter), "status": NOT_CERTIFIED,
                   "registry_sha256": registry_sha256(), "criteria": CRITERIA, "profile": P0_PROFILE,
                   "parameters": PARAMETERS, "limits": LIMITS,
                   "source": {"descriptor": descriptor, "sha256": source_sha256}, "interpreter": interpreter,
                   "cases": cases, "counts": _counts(cases)}
    if _cases_problem(cases) is None:
        certificate["status"] = CERTIFIED
    return certificate


def _counts(cases):
    counts = {"cases": len(cases), AS_EXPECTED: 0, NOT_AS_EXPECTED: 0, ERROR: 0}
    for item in cases:
        if item["result"] in counts:
            counts[item["result"]] += 1
    return counts


def _cases_problem(cases):
    """None when the cases are exactly the registry's, in order, each well formed and as_expected with equal observed
    and expected digests; else the reason (not_the_registry, malformed_case or not_all_as_expected)."""
    if not (isinstance(cases, list)
            and [item.get("id") if isinstance(item, dict) else None for item in cases] == list(CASE_IDS)):
        return "not_the_registry"
    passed = True
    for item, (_, kind, _) in zip(cases, CASES):
        if not (sorted(item) == ["expected_sha256", "id", "kind", "observed_sha256", "result"]
                and type(item["kind"]) is str and item["kind"] == kind
                and type(item["result"]) is str and item["result"] in (AS_EXPECTED, NOT_AS_EXPECTED, ERROR)
                and _is_sha256(item["observed_sha256"]) and _is_sha256(item["expected_sha256"])):
            return "malformed_case"
        if item["result"] == AS_EXPECTED and item["observed_sha256"] != item["expected_sha256"]:
            return "malformed_case"                   # run_case says as_expected only when they are equal
        passed = passed and item["result"] == AS_EXPECTED
    return None if passed else "not_all_as_expected"


def certificate_bytes(certificate):
    return canonical_json(certificate) + b"\n"


def validate_certificate(certificate, key=None):
    """The problems of a certificate value ([] when it is a certified certificate for key, or for its own key when
    key is None): every field is checked against this protocol's registry, profile, parameters, limits and criteria;
    the descriptor digest and the key are recomputed; no total or status is trusted."""
    if not (isinstance(certificate, dict) and sorted(certificate) == sorted(CERTIFICATE_KEYS)):
        return ["certificate:malformed:keys"]
    problems = []
    if canonical_json([certificate["kind"], certificate["schema_version"], certificate["protocol_version"]]) != \
            canonical_json([CERTIFICATE_KIND, SCHEMA_VERSION, PROTOCOL_VERSION]):
        problems.append("certificate:malformed:version")
    for name, wanted in (("registry_sha256", registry_sha256()), ("criteria", CRITERIA), ("profile", P0_PROFILE),
                         ("parameters", PARAMETERS), ("limits", LIMITS)):
        if canonical_json(certificate[name]) != canonical_json(wanted):
            problems.append(f"certificate:not_this_protocol:{name}")
    source, interpreter = certificate["source"], certificate["interpreter"]
    if not (isinstance(source, dict) and sorted(source) == ["descriptor", "sha256"]
            and _descriptor_problem(source["descriptor"]) is None
            and source["sha256"] == descriptor_sha256(source["descriptor"])):
        problems.append("certificate:malformed:source")
    if not (isinstance(interpreter, dict) and sorted(interpreter) == sorted(INTERPRETER_KEYS)):
        problems.append("certificate:malformed:interpreter")
    if problems:
        return problems
    if certificate["key"] != certificate_key(source["sha256"], interpreter):
        problems.append("certificate:malformed:key")
    elif key is not None and certificate["key"] != key:
        problems.append("certificate:not_this_key")
    cases_problem = _cases_problem(certificate["cases"])
    if cases_problem in ("not_the_registry", "malformed_case"):
        problems.append(f"certificate:malformed:{cases_problem}")
    elif cases_problem is not None or canonical_json(certificate["status"]) != canonical_json(CERTIFIED):
        problems.append("certificate:not_all_passed")
    if cases_problem in (None, "not_all_as_expected") and \
            canonical_json(certificate["counts"]) != canonical_json(_counts(certificate["cases"])):
        problems.append("certificate:malformed:counts")
    return problems


# --- storage ---------------------------------------------------------------------------------------------------------

def _guard(path, where, prospective=False):
    try:
        return guard_path(path, where, prospective)
    except PathDenied as error:
        raise CertificateProblem(str(error)) from None


def lab_root(lab_state_root):
    """The resolved lab-state root after the runner's checks (guarded; check_data_root: present, a listable
    directory outside this repository and every Git worktree); nothing is created."""
    _guard(lab_state_root, "lab_state_root")
    lab, problems = check_data_root(lab_state_root)
    if problems:
        raise CertificateProblem(problems[0].replace("data_root:", "lab_state_root:", 1))
    return lab


def directory(path, where):
    """path as an existing plain directory, created when missing after guarding it."""
    try:
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise CertificateProblem(f"{where}:not_a_directory")
        if not path.exists():
            _guard(path, where, prospective=True)
            path.mkdir()
    except OSError:
        raise CertificateProblem(f"{where}:unwritable") from None
    return _guard(path, where)


def _present(path, where):
    """Whether anything, a dangling symlink included, is at path; an inspection error refuses, never reads as absent."""
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        raise CertificateProblem(f"{where}:uninspectable") from None
    return True


def _plain(path, where, want_directory):
    """path, guarded, when it is a plain directory (want_directory) or a regular file, not a symlink; else
    CertificateProblem <where>:not_a_directory or <where>:not_a_regular_file (<where>:uninspectable on an error)."""
    try:
        plain = not path.is_symlink() and (path.is_dir() if want_directory else path.is_file())
    except OSError:
        raise CertificateProblem(f"{where}:uninspectable") from None
    if not plain:
        raise CertificateProblem(f"{where}:{'not_a_directory' if want_directory else 'not_a_regular_file'}")
    return _guard(path, where)


def _read_bounded(path, where):
    try:
        with path.open("rb") as handle:
            data = handle.read(MAX_CERTIFICATE_BYTES + 1)
    except OSError:
        raise CertificateProblem(f"{where}:unreadable") from None
    if len(data) > MAX_CERTIFICATE_BYTES:
        raise CertificateProblem(f"{where}:too_large")
    return data


def write_new(path, data, where):
    """Write data to a new file by exclusive create after guarding it; CertificateProblem <where>:not_created when
    nothing was created, <where>:uncertain when anything failed after it was (the file is left as evidence)."""
    _guard(path, where, prospective=True)
    try:
        handle = path.open("xb")
    except OSError:
        raise CertificateProblem(f"{where}:not_created") from None
    try:
        with handle:
            handle.write(data)
    except Exception:
        raise CertificateProblem(f"{where}:uncertain") from None


def certificate_path(lab, key):
    if not _is_sha256(key):
        raise CertificateProblem("certification:key_malformed")
    return lab / CERTIFICATIONS_DIR / f"{key}.json"


def store_certificate(lab, certificate):
    """(already_certified or certified, path, sha256) for a certified certificate in the lab root (module docstring);
    CertificateProblem certification:existing_differs when other bytes hold its key."""
    problems = validate_certificate(certificate)
    if problems:
        raise CertificateProblem(f"certification:not_storable:{problems[0]}")
    data = certificate_bytes(certificate)
    path = certificate_path(lab, certificate["key"])
    directory(path.parent, "certifications")
    if _present(path, "certificate"):
        existing = _read_bounded(_plain(path, "certificate", False), "certificate")
        if existing != data:
            raise CertificateProblem("certification:existing_differs")
        return "already_certified", path, _sha256(data)
    write_new(path, data, "certificate")
    return CERTIFIED, path, _sha256(data)


def load_certificate(lab, key):
    """(certificate, sha256) of the certified certificate stored for key in the lab root; CertificateProblem
    certification:missing, certification:malformed[:<detail>], certification:not_this_key or
    certification:not_all_passed otherwise."""
    path = certificate_path(lab, key)
    if not _present(path.parent, "certifications"):
        raise CertificateProblem("certification:missing")
    path = _plain(path.parent, "certifications", True) / path.name
    if not _present(path, "certificate"):
        raise CertificateProblem("certification:missing")
    data = _read_bounded(_plain(path, "certificate", False), "certification")
    try:
        value = loads_strict(data)
    except (ValueError, RecursionError):
        raise CertificateProblem("certification:malformed:not_json") from None
    try:
        canonical = certificate_bytes(value)
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError):
        canonical = None
    if canonical != data:
        raise CertificateProblem("certification:malformed:not_canonical")
    problems = validate_certificate(value, key)
    if problems:
        raise CertificateProblem(problems[0].replace("certificate:", "certification:", 1))
    return value, _sha256(data)


def certificate_summary(certificate):
    """A short JSON-ready description of a certificate for the CLI: its key, status, counts and failing cases."""
    return {"key": certificate["key"], "status": certificate["status"], "counts": certificate["counts"],
            "source_descriptor_sha256": certificate["source"]["sha256"],
            "failed_cases": [item["id"] for item in certificate["cases"] if item["result"] != AS_EXPECTED]}
