"""Capability and coverage audit over every indexed archive under a data root (report schema_version 1).

Groups are tier (plan from the path) x month (the UTC month of the first-published marketTime; "unknown" when there
is none) x countryCode x marketType (first published values; "missing" when absent), and each group reports counts
only: markets; which stream fields appear anywhere in the member (fields_present, whole file: KNOWN_FIELDS and any
other field seen, with the number of markets showing it), which appear strictly before the decision cutoff
(fields_present_before_cutoff, what a decision-time capability can rest on, plan section 2.3) and which of the
market_index.VALUED_FIELDS carry a usable value there (fields_valued_before_cutoff: an rc.ltp above 0, a finite
rc.spn or rc.spf); which KNOWN_FIELDS never appear (fields_absent and fields_absent_before_cutoff, listed by name:
missing is reported as missing, never as a zero rate); bspMarket true, false or missing; markets missing a final
BSP for any runner not REMOVED, REMOVED_VACANT or HIDDEN at the end (overall and among bspMarket markets) and
markets with a final bsp present but unusable (such as "NaN"); markets with removals and the removals before and
after the decision cutoff; dead heats, closed markets whose dead heat cannot be judged (numberOfWinners missing or
below 1, or a market type the rule does not cover), and results revised after CLOSED; markets never CLOSED;
path/marketTime month, date and event id mismatches; markets whose marketTime was revised; markets read from a
member that could not be read whole; markets with an unrecognised runner status; markets held by more than one
member and those whose sources disagree (whole record, and decision_time alone). Across the root: archives with
their refused members by reason and whether their manifest's month gate is the current one, unreadable files with
reasons and how many markets they still gave, refused markets, archives not yet indexed, and markets seen in more
than one archive of the same tier (counted once, in the first archive by sha256).

No outcome statistic beyond those counts is computed: no win rates, prices, BSP values or returns, so the report
cannot be used to look at outcomes; every number in it is an integer count. The audit reads the evaluator-only part
of each record only to count those coverage cases. There is no minimum group size: a group of one market reveals
that market's settlement coverage (closed or not, dead heat, BSP missing), never its winners or prices
(docs/backtest-plan.md section 10). It verifies each index head against its manifest's sha256, the markets file
against the head's markets_sha256 and line count, and every record against the current month gate (a record whose
path month or marketTime month the gate now refuses refuses the audit as stale_gate: an index built under an older
gate is never counted); it writes nothing, every file-system failure is a refusal, and its JSON is deterministic for
one data root.
"""

import hashlib

from .archive import ArchiveProblem, check_data_root, gate_snapshot, month_gate, safe_path
from .contract import canonical_json
from .market_index import (FILE_STATUSES, HEAD_FILE, HEAD_KIND, INDEX_SCHEMA_VERSION, MARKETS_FILE, IndexProblem,
                           archive_shas, load_manifest, loads_strict)

AUDIT_SCHEMA_VERSION = 1
AUDIT_KIND = "betfair_historical_coverage_audit"
KNOWN_FIELDS = ("mc.con", "mc.img", "mc.marketDefinition", "mc.tv", "md.bspMarket", "md.bspReconciled",
                "md.marketTime", "md.numberOfWinners", "md.runners.adjustmentFactor", "md.runners.bsp",
                "md.runners.removalDate", "md.runners.status", "md.settledTime", "md.status", "rc.atb", "rc.atl",
                "rc.batb", "rc.batl", "rc.ltp", "rc.spb", "rc.spf", "rc.spl", "rc.spn", "rc.trd", "rc.tv")
COUNTS = ("bsp_market_false", "bsp_market_missing", "bsp_market_true", "closed_dead_heat_not_assessable",
          "date_mismatches", "dead_heats", "event_id_mismatches", "market_time_revised", "markets",
          "markets_missing_final_bsp", "markets_missing_final_bsp_in_bsp_markets", "markets_never_closed",
          "markets_read_incomplete", "markets_with_removals", "markets_with_unrecognised_runner_status",
          "markets_with_unusable_final_bsp", "month_mismatches", "multiple_sources", "removals_after_cutoff",
          "removals_before_cutoff", "results_revised_after_close", "sources_disagree", "sources_disagree_decision_time")
# Record coverage part -> the group's field counts it feeds.
FIELD_PARTS = (("whole_file", "fields_present"), ("before_cutoff", "fields_present_before_cutoff"),
               ("valued_before_cutoff", "fields_valued_before_cutoff"))


def _new_group():
    return {**{name: {} for _, name in FIELD_PARTS}, **dict.fromkeys(COUNTS, 0)}


def _first(record, key):
    """The first published value of a definition field, from the decision part or else from after the cutoff."""
    for part, name in (("decision_time", "definition"), ("evaluator_only_outcome", "definition_after_cutoff")):
        timeline = record[part][name].get(key)
        if timeline:
            return timeline[0][1]
    return None


def _count(group, record):
    outcome, checks = record["evaluator_only_outcome"], record["checks"]
    group["markets"] += 1
    for part, name in FIELD_PARTS:
        for field in record["coverage"][part]:
            group[name][field] = group[name].get(field, 0) + 1
    bsp_market = _first(record, "bspMarket")
    group["bsp_market_true" if bsp_market is True else "bsp_market_false" if bsp_market is False
          else "bsp_market_missing"] += 1
    if outcome["missing_final_bsp_runner_ids"]:
        group["markets_missing_final_bsp"] += 1
        if bsp_market is True:
            group["markets_missing_final_bsp_in_bsp_markets"] += 1
    group["markets_with_unusable_final_bsp"] += 1 if outcome["unusable_final_bsp_runner_ids"] else 0
    before, after = len(record["decision_time"]["removals"]), len(outcome["removals_after_cutoff"])
    group["removals_before_cutoff"] += before
    group["removals_after_cutoff"] += after
    group["markets_with_removals"] += 1 if before + after else 0
    group["dead_heats"] += 1 if outcome["dead_heat"] is True else 0
    group["closed_dead_heat_not_assessable"] += 1 if outcome["closed_reached"] and outcome["dead_heat"] is None else 0
    group["results_revised_after_close"] += 1 if outcome["results_revised_after_close"] is True else 0
    group["markets_never_closed"] += 0 if outcome["closed_reached"] else 1
    group["markets_read_incomplete"] += 0 if outcome["read_complete"] is True else 1
    group["month_mismatches"] += 1 if checks["month_mismatch"] is True else 0
    group["date_mismatches"] += 1 if checks["date_mismatch"] is True else 0
    group["event_id_mismatches"] += 1 if checks["event_id_mismatch"] is True else 0
    group["markets_with_unrecognised_runner_status"] += 1 if checks["unrecognised_runner_statuses"] else 0
    times = (len(record["decision_time"]["definition"].get("marketTime", []))
             + len(outcome["definition_after_cutoff"].get("marketTime", [])))
    group["market_time_revised"] += 1 if times > 1 else 0
    sources = record["provenance"]["other_sources"]
    group["multiple_sources"] += 1 if sources else 0
    group["sources_disagree"] += 1 if any(item["agrees"] is not True for item in sources) else 0
    group["sources_disagree_decision_time"] += 1 if any(item["agrees_decision_time"] is not True
                                                        for item in sources) else 0


def _read_head(root, sha, manifest_sha):
    """The verified index head of one archive (with its index_sha256), None when not indexed; IndexProblem when it is
    not canonical v1, was built from another manifest, or has an unexpected shape."""
    where = f"index:{sha}"
    try:
        head_path = safe_path(root, "index", sha, HEAD_FILE)
        if not head_path.exists():
            return None
        data = head_path.read_bytes()
        head = loads_strict(data)
    except ArchiveProblem as error:
        raise IndexProblem(f"{where}:{error}") from None
    except (OSError, ValueError, RecursionError):
        raise IndexProblem(f"{where}:head_unreadable") from None
    if (not isinstance(head, dict) or canonical_json(head) + b"\n" != data
            or (head.get("schema_version"), head.get("kind")) != (INDEX_SCHEMA_VERSION, HEAD_KIND)):
        raise IndexProblem(f"{where}:head_not_canonical_v1")
    if head.get("manifest_sha256") != manifest_sha or head.get("archive_sha256") != sha:
        raise IndexProblem(f"{where}:stale_for_manifest")
    files, refused = head.get("files"), head.get("refused_markets")
    if (head.get("markets_file") != MARKETS_FILE or not isinstance(files, list) or not isinstance(refused, list)
            or type(head.get("markets")) is not int or not isinstance(head.get("markets_sha256"), str)
            or not all(isinstance(item, dict) and {"complete_before_pt", "markets", "path", "reason", "status"}
                       <= set(item) and isinstance(item["status"], str) and item["status"] in FILE_STATUSES
                       and isinstance(item["markets"], list) for item in files)
            or not all(isinstance(item, dict) and {"market_id", "member_path", "reason"} <= set(item)
                       for item in refused)):
        raise IndexProblem(f"{where}:head_malformed")
    head["index_sha256"] = hashlib.sha256(data).hexdigest()
    return head


def _count_archive(root, sha, head, seen):
    """(groups, keys counted, duplicates) for one archive's markets file, streamed line by line; IndexProblem when
    the file is not the one the head names (hash or line count), a record is malformed, or a record's month is one
    the current gate refuses (stale_gate). Nothing is merged into the caller's totals until the whole file has
    been verified."""
    where = f"index:{sha}"
    groups, counted, duplicates, lines, digest = {}, set(), 0, 0, hashlib.sha256()
    try:
        path = safe_path(root, "index", sha, head["markets_file"])
        with open(path, "rb") as handle:
            for line in handle:
                digest.update(line)
                lines += 1
                record = loads_strict(line)
                months = {record["checks"]["path_month"], *record["evaluator_only_outcome"]["market_time_months"]}
                if any(month_gate(month) for month in months):
                    raise IndexProblem(f"{where}:stale_gate")
                identity = (record["provenance"]["plan"], record["market_id"])
                if identity in seen or identity in counted:
                    duplicates += 1
                    continue
                counted.add(identity)
                key = (identity[0], record["checks"]["market_time_month"] or "unknown",
                       _first(record, "countryCode") or "missing", _first(record, "marketType") or "missing")
                if not all(isinstance(part, str) for part in key):
                    raise IndexProblem(f"{where}:markets_malformed")
                _count(groups.setdefault(key, _new_group()), record)
    except ArchiveProblem as error:
        raise IndexProblem(f"{where}:{error}") from None
    except (OSError, ValueError, RecursionError):
        raise IndexProblem(f"{where}:markets_unreadable") from None
    except (KeyError, TypeError, AttributeError, IndexError):
        raise IndexProblem(f"{where}:markets_malformed") from None
    if digest.hexdigest() != head["markets_sha256"] or lines != head["markets"]:
        raise IndexProblem(f"{where}:markets_do_not_match_head")
    return groups, counted, duplicates


def audit(data_root):
    """The coverage report for every archive under data_root: status audited, empty (no manifest) or refused."""
    root, problems = check_data_root(data_root)
    if problems:
        return {"status": "refused", "problems": problems}
    try:
        return _audit(root)
    except IndexProblem as error:
        return {"status": "refused", "problems": [str(error)]}
    except OSError as error:
        return {"status": "refused", "problems": [f"io:{type(error).__name__}"]}


def _audit(root):
    archives, not_indexed, unreadable, refused_markets, groups, seen = [], [], [], [], {}, set()
    duplicates, problems = 0, []
    for sha in archive_shas(root):
        try:
            manifest, manifest_sha = load_manifest(root, sha)
            head = _read_head(root, sha, manifest_sha)
            if head is None:
                not_indexed.append(sha)
                continue
            counted_groups, counted, archive_duplicates = _count_archive(root, sha, head, seen)
        except IndexProblem as error:
            problems.append(str(error))
            continue
        seen |= counted
        duplicates += archive_duplicates
        for key, counts in counted_groups.items():
            group = groups.setdefault(key, _new_group())
            for _, name in FIELD_PARTS:
                for field, value in counts.pop(name).items():
                    group[name][field] = group[name].get(field, 0) + value
            for name, value in counts.items():
                group[name] += value
        reasons = {}
        for item in manifest["refused"]:
            reasons[item["reason"]] = reasons.get(item["reason"], 0) + 1
        statuses = {}
        for item in head["files"]:
            statuses[item["status"]] = statuses.get(item["status"], 0) + 1
            if item["status"] == "unreadable":
                unreadable.append({"archive_sha256": sha, "path": item["path"], "reason": item["reason"],
                                   "complete_before_pt": item["complete_before_pt"], "markets": len(item["markets"])})
        refused_markets += [{"archive_sha256": sha, **item} for item in head["refused_markets"]]
        archives.append({"archive_sha256": sha, "manifest_sha256": manifest_sha, "index_sha256": head["index_sha256"],
                         "plan": manifest["plan"], "months": manifest["months"], "members": len(manifest["members"]),
                         "month_gate_current": manifest["month_gate"] == gate_snapshot(),
                         "refused_members": len(manifest["refused"]),
                         "refused_by_reason": dict(sorted(reasons.items())),
                         "files_indexed": statuses.get("indexed", 0), "files_unreadable": statuses.get("unreadable", 0),
                         "files_refused": statuses.get("refused", 0), "markets": head["markets"],
                         "markets_refused": len(head["refused_markets"])})
    if problems:
        return {"status": "refused", "problems": sorted(problems)}
    report_groups = []
    for (tier, month, country, market_type), group in sorted(groups.items()):
        present, early = group["fields_present"], group["fields_present_before_cutoff"]
        report_groups.append({"tier": tier, "month": month, "country": country, "market_type": market_type,
                              **{name: dict(sorted(group[name].items())) for _, name in FIELD_PARTS},
                              "fields_absent": [name for name in KNOWN_FIELDS if name not in present],
                              "fields_absent_before_cutoff": [name for name in KNOWN_FIELDS if name not in early],
                              **{name: group[name] for name in sorted(COUNTS)}})
    totals = {name: sum(group[name] for group in report_groups) for name in sorted(COUNTS)}
    return {"status": "audited" if archives or not_indexed else "empty", "schema_version": AUDIT_SCHEMA_VERSION,
            "kind": AUDIT_KIND, "note": "Integer counts for coverage only: no win rates, prices, BSP values or "
                                        "returns are computed.",
            "known_fields": list(KNOWN_FIELDS), "archives": archives, "archives_not_indexed": not_indexed,
            "groups": report_groups, "totals": totals, "markets_in_more_than_one_archive": duplicates,
            "unreadable_files": unreadable, "refused_markets": refused_markets}
