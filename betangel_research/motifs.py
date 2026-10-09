"""Offline, unvalidated motif policies. Outputs are hypotheses, never orders.

Context is an explicit supplied assessment, not an LLM or a news truth oracle.
Every policy uses a frozen information cutoff; latency delays issuance only.
"""

import json
import math
from collections import Counter, defaultdict
from pathlib import Path
import time


def finite(value):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def milliseconds(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def context_expiry(event, config):
    return min(event["expires_ms"], event["event_ms"] + round(config["max_context_age_s"] * 1000))


def validate_config(config):
    if not isinstance(config, dict):
        raise ValueError("config must be an object")
    if config.get("experiment_id") != "motif-prototypes-v1":
        raise ValueError("unsupported motif experiment_id")
    for key in ("max_relative_spread", "min_relative_move", "min_volume_delta",
                "max_feature_age_s", "signal_lifetime_s", "max_context_age_s"):
        if not finite(config.get(key)) or config[key] <= 0:
            raise ValueError(f"{key} must be finite and positive")
    delays = config.get("processing_delays_s")
    if not isinstance(delays, list) or not delays or any(not finite(d) or d < 0 for d in delays):
        raise ValueError("processing delays must be nonnegative finite numbers")
    if len(delays) != len(set(delays)) or 0 not in delays:
        raise ValueError("include unique delays and zero-delay baseline")
    if any(d * 1000 != round(d * 1000) for d in delays):
        raise ValueError("sub-millisecond delays unsupported")


def visible_context(events, market_id, cutoff, config):
    """Immutable assessments; future versions cannot change an earlier decision.

    All four availability clocks must be known. Supplied timestamps/assessments
    are not independently authenticated by this function. Context with missing
    provenance is unusable, not inferred to mean no news.
    """
    selected = {}
    clocks = ("published_ms", "received_ms", "link_known_ms", "analysis_completed_ms")
    for item in events:
        if item.get("market_id") != market_id:
            continue
        if any(not milliseconds(item.get(k)) for k in clocks):
            continue
        if max(item[k] for k in clocks) > cutoff:
            continue
        event = item.get("event_ms")
        expiry = item.get("expires_ms")
        if not milliseconds(event) or not milliseconds(expiry):
            continue
        if not (event <= item["published_ms"] <= item["received_ms"] <= item["analysis_completed_ms"]
                and item["link_known_ms"] <= item["analysis_completed_ms"] < expiry):
            continue
        if context_expiry(item, config) <= cutoff:
            continue
        if any(not isinstance(item.get(k), str) or not item[k].strip() for k in
               ("id", "source", "explanation", "alternative_explanation", "content_sha256")):
            continue
        digest = item["content_sha256"]
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            continue
        if item.get("kind") not in ("public_tip", "previous_race_finish", "context_assessment"):
            continue
        selection = item.get("selection_id")
        if selection is not None and (not isinstance(selection, str) or not selection.strip()):
            continue
        if item["kind"] == "previous_race_finish" and (
                not isinstance(item.get("related_market_id"), str)
                or not item["related_market_id"].strip() or item["related_market_id"] == market_id
                or item.get("event_status") != "confirmed"):
            continue
        if item["kind"] == "context_assessment" and item.get("assessment") not in (
                "compatible", "contradicted", "uncertain"):
            continue
        if item["kind"] == "context_assessment":
            scope = item.get("coverage_scope")
            if (not isinstance(scope, list) or not scope
                    or any(not isinstance(s, str) or not s.strip() for s in scope)):
                continue
        if item["id"] in selected and selected[item["id"]] != item:
            raise ValueError(f"conflicting immutable context ID: {item['id']}")
        selected[item["id"]] = item
    return [selected[k] for k in sorted(selected)]


def evidence_trace(events):
    keys = ("id", "source", "content_sha256", "event_ms", "published_ms", "received_ms",
            "link_known_ms", "analysis_completed_ms", "expires_ms", "kind", "assessment",
            "explanation", "alternative_explanation", "coverage_scope", "selection_id",
            "related_market_id", "event_status", "reasoner")
    return [{key: event[key] for key in keys if key in event} for event in events]


def timing_reasons(record, issue_ms, config):
    reasons = []
    for key in ("decision_ms", "observed_ms", "start_ms"):
        if not milliseconds(record.get(key)):
            return ["invalid_or_missing_feature_time"]
    cutoff = record["decision_ms"]
    if record["observed_ms"] > cutoff:
        reasons.append("future_feature")
    if issue_ms - record["observed_ms"] >= config["max_feature_age_s"] * 1000:
        reasons.append("feature_too_old_at_issue")
    if issue_ms >= cutoff + config["signal_lifetime_s"] * 1000:
        reasons.append("signal_expired")
    if issue_ms >= record["start_ms"]:
        reasons.append("scheduled_start_reached")
    return reasons


def continuation(record, issue_ms, config):
    reasons = timing_reasons(record, issue_ms, config)
    if record.get("eligible") is not True:
        reasons.extend(record.get("invalid_reasons") or ["ineligible_features"])
    prices = [record.get(k) for k in ("back", "lay", "previous_back", "previous_lay")]
    if any(not finite(p) or p <= 1 for p in prices):
        reasons.append("missing_or_invalid_quotes")
        move = spread = None
    else:
        back, lay, previous_back, previous_lay = prices
        mid, previous_mid = (back + lay) / 2, (previous_back + previous_lay) / 2
        spread = (lay - back) / mid
        move = (previous_mid - mid) / previous_mid
        if back > lay or previous_back > previous_lay:
            reasons.append("crossed_quotes")
        if spread > config["max_relative_spread"]:
            reasons.append("spread_too_wide")
        if abs(move) < config["min_relative_move"]:
            reasons.append("move_below_threshold")
    if any(not finite(record.get(k)) or record[k] <= 0 for k in ("back_size", "lay_size")):
        reasons.append("missing_positive_available_sizes")
    volume = record.get("volume_delta")
    if not finite(volume) or volume < config["min_volume_delta"]:
        reasons.append("matched_activity_not_established")
    previous_time = record.get("previous_observed_ms")
    if (not milliseconds(previous_time) or previous_time >= record["decision_ms"]
            or previous_time > record["observed_ms"]):
        reasons.append("invalid_previous_feature_time")
    reasons = sorted(set(reasons))
    return {"status": "abstain" if reasons else "hypothesis",
            "odds_direction": None if reasons else ("shorten" if move > 0 else "drift"),
            "reasons": reasons or ["price_move_with_unsigned_matched_activity"],
            "measurements": {"relative_midpoint_shortening": move, "relative_spread": spread,
                             "unsigned_volume_delta": volume,
                             "available_size_imbalance_diagnostic_only": record.get("book_imbalance")}}


def evaluate_policies(records, events, config):
    validate_config(config)
    if not isinstance(records, list) or any(not isinstance(r, dict) for r in records):
        raise ValueError("records must be a list of objects")
    if not isinstance(events, list) or any(not isinstance(e, dict) for e in events):
        raise ValueError("context must be a list of objects")
    decisions = []
    grouped = defaultdict(list)
    seen = set()
    for record in records:
        if not milliseconds(record.get("decision_ms")):
            raise ValueError("record decision_ms must be nonnegative integer milliseconds")
        if any(not milliseconds(record.get(k)) for k in ("observed_ms", "start_ms")):
            raise ValueError("record observed_ms and start_ms must be nonnegative integer milliseconds")
        if (not isinstance(record.get("invalid_reasons", []), list)
                or any(not isinstance(r, str) for r in record.get("invalid_reasons", []))):
            raise ValueError("invalid_reasons must be a list of strings")
        if any(not isinstance(record.get(k), str) or not record[k] for k in ("market_id", "selection_id")):
            raise ValueError("explicit string market and selection IDs required")
        identity = (record["market_id"], record["selection_id"], record["decision_ms"])
        if identity in seen:
            raise ValueError(f"duplicate feature decision: {identity}")
        seen.add(identity)
        grouped[(record["market_id"], record["decision_ms"])].append(record)
    for (market_id, cutoff), market_records in sorted(grouped.items()):
        contexts = visible_context(events, market_id, cutoff, config)
        for delay in config["processing_delays_s"]:
            issue = cutoff + round(delay * 1000)
            for record in sorted(market_records, key=lambda r: r["selection_id"]):
                common = {"market_id": market_id, "selection_id": record["selection_id"],
                          "cutoff_ms": cutoff, "issue_ms": issue, "processing_delay_s": delay,
                          "expires_ms": min(cutoff + round(config["signal_lifetime_s"] * 1000),
                                            record["observed_ms"] + round(config["max_feature_age_s"] * 1000),
                                            record.get("start_ms", cutoff)),
                          "feature_observed_ms": record.get("observed_ms"),
                          "unit": "runner_decision"}
                base = continuation(record, issue, config)
                decisions.append({**common, "policy": "P01", **base, "evidence": []})
                applicable = [event for event in contexts if event["kind"] == "context_assessment"
                              and event.get("selection_id") in (None, record["selection_id"])]
                context_reasons = []
                if not applicable:
                    context_reasons.append("context_unknown")
                if any(context_expiry(e, config) <= issue for e in applicable):
                    context_reasons.append("context_expired_at_issue")
                if any(e["assessment"] == "contradicted" for e in applicable):
                    context_reasons.append("context_contradicted")
                if any(e["assessment"] == "uncertain" for e in applicable):
                    context_reasons.append("context_uncertain")
                overlay = {**base}
                if context_reasons:
                    overlay.update(status="abstain", odds_direction=None,
                                   reasons=sorted(set((base["reasons"] if base["status"] == "abstain" else [])
                                                      + context_reasons)))
                else:
                    overlay["reasons"] = base["reasons"] + ["compatible_with_declared_context_scope"]
                decisions.append({**common, "policy": "P03", **overlay,
                                  "expires_ms": min([common["expires_ms"]]
                                                    + [context_expiry(e, config) for e in applicable]),
                                  "evidence": evidence_trace(applicable)})
            runner_ids = {r["selection_id"] for r in market_records}
            attention = [e for e in contexts if e["kind"] in ("public_tip", "previous_race_finish")
                         and e.get("selection_id") in runner_ids | {None}]
            reasons = []
            if not attention:
                reasons.append("no_timely_linked_attention_event")
            if any(context_expiry(e, config) <= issue for e in attention):
                reasons.append("attention_event_expired_at_issue")
            # One market-level watch, not one apparent independent event per runner.
            usable = [r for r in market_records if r.get("eligible") is True
                      and not timing_reasons(r, issue, config)]
            if not usable:
                reasons.append("no_timely_eligible_market_features")
            decisions.append({"market_id": market_id, "selection_id": None, "cutoff_ms": cutoff,
                              "issue_ms": issue, "processing_delay_s": delay, "policy": "P02",
                              "expires_ms": min([cutoff + round(config["signal_lifetime_s"] * 1000)]
                                                + [r["start_ms"] for r in market_records]
                                                + [context_expiry(e, config) for e in attention]
                                                + [max((r["observed_ms"] for r in usable), default=cutoff)
                                                   + round(config["max_feature_age_s"] * 1000)]),
                              "unit": "market_decision", "status": "abstain" if reasons else "watch",
                              "odds_direction": None,
                              "reasons": reasons or ["attention_may_change_activity_direction_unknown"],
                              "target_selection_ids": sorted({e["selection_id"] for e in attention
                                                               if e.get("selection_id") is not None}),
                              "evidence": evidence_trace(attention)})
    return decisions


def summarize(decisions):
    groups = defaultdict(list)
    for item in decisions:
        groups[(item["policy"], item["processing_delay_s"])].append(item)
    return [{"policy": policy, "processing_delay_s": delay, "unit": rows[0]["unit"],
             "total": len(rows), "statuses": dict(Counter(r["status"] for r in rows)),
             "reasons": dict(Counter(reason for r in rows for reason in r["reasons"]))}
            for (policy, delay), rows in sorted(groups.items())]


def run_motif_lab(input_path, config_path, output_path, input_format="synthetic", context_path=None):
    """Write a reproducible engineering trace. No target labels, scoring or P&L."""
    from .__main__ import sha256
    import platform
    import subprocess
    from datetime import datetime, timezone
    started = time.perf_counter()
    output = Path(output_path)
    if output.exists():
        raise ValueError(f"output already exists: {output}")
    config = json.loads(Path(config_path).read_text())
    validate_config(config)
    source_hash = sha256(input_path)
    if input_format == "synthetic":
        fixture = json.loads(Path(input_path).read_text())
        if not isinstance(fixture, dict) or fixture.get("data_kind") != "synthetic":
            raise ValueError("fixture must explicitly declare synthetic data_kind")
        records, events = fixture["records"], fixture.get("context", [])
        audit = {"data_kind": "synthetic", "fixture_purpose": fixture.get("purpose")}
    elif input_format == "adv":
        from .microstructure import sample_features
        sample = sample_features(input_path)
        records, events, audit = sample["records"], [], sample["audit"]
    else:
        raise ValueError("input format must be synthetic or adv")
    if context_path:
        if events:
            raise ValueError("use embedded context or context file, not both")
        events = json.loads(Path(context_path).read_text())
        if not isinstance(events, list):
            raise ValueError("context file must be a list")
    decisions = evaluate_policies(records, events, config)
    root = Path(__file__).resolve().parent.parent
    git = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True)
    dirty = subprocess.run(["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True)
    report = {"schema_version": 1, "status": "offline_engineering_prototypes_not_validated_strategies",
              "created_at": datetime.now(timezone.utc).isoformat(), "config": config,
              "config_sha256": sha256(config_path), "input_sha256": source_hash,
              "context_sha256": sha256(context_path) if context_path else None,
              "code_sha256": {str(p.relative_to(root)): sha256(p)
                              for p in sorted((root / "betangel_research").glob("*.py"))},
              "git_revision": git.stdout.strip() if git.returncode == 0 else None,
              "working_tree_dirty": bool(dirty.stdout) if dirty.returncode == 0 else None,
              "python": platform.python_version(), "input_format": input_format, "audit": audit,
              "feature_records": len(records), "context_records_supplied": len(events),
              "summary": summarize(decisions), "economic_result": None, "forecast_score": None,
              "context_reasoning": "supplied structured assessments; no model inference performed",
              "elapsed_seconds": round(time.perf_counter() - started, 4),
              "limitations": [
                  "Illustrative thresholds; no fitting, labels, holdout or profitability test.",
                  "P01 and P03 are runner-level hypotheses; P02 is a market-level activity watch.",
                  "ADV pt is publisher time, not proven receipt time; processing delays are injected.",
                  "Matched volume is unsigned; aggregate books do not establish trader intent.",
                  "Context timestamps/content hashes are supplied evidence, not independent authentication.",
                  "Missing context means unknown. Compatibility is scoped to supplied sources, not absence of news.",
                  "Modern model knowledge can leak historical outcomes; use synthetic/prospective context.",
                  "P03 selective abstention requires full-cohort and matched-retained comparisons later.",
                  "Engineering runtime and issued-hypothesis counts do not measure market advantage.",
              ]}
    # Complete validation before creating artifacts; exclusive directory prevents overwrite.
    encoded_report = json.dumps(report, indent=2, allow_nan=False) + "\n"
    encoded_decisions = "".join(json.dumps(d, allow_nan=False) + "\n" for d in decisions)
    encoded_features = "".join(json.dumps(r, allow_nan=False) + "\n" for r in records)
    output.mkdir(parents=True)
    (output / "report.json").write_text(encoded_report)
    (output / "decisions.jsonl").write_text(encoded_decisions)
    (output / "features.jsonl").write_text(encoded_features)
    ledger = {"created_at": report["created_at"], "experiment_id": config["experiment_id"],
              "input_sha256": source_hash, "config_sha256": report["config_sha256"],
              "report": str((output / "report.json").resolve()),
              "report_sha256": sha256(output / "report.json"), "status": report["status"]}
    with (output.parent / "run-ledger.jsonl").open("a") as handle:
        handle.write(json.dumps(ledger) + "\n")
    return report
