"""Command line entry point for reproducible offline forecast experiments."""

import argparse
import csv
import hashlib
import json
from pathlib import Path
import platform
import statistics
import subprocess
import time
from datetime import datetime, timezone
from .evaluation import evaluate_records, chronological_folds
from .stream import replay_snapshots, build_records


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_replay(input_path, config_path, output_path):
    started = time.perf_counter()
    output = Path(output_path)
    if output.exists():
        raise ValueError(f"output already exists; choose a new run directory: {output}")
    config = json.loads(Path(config_path).read_text())
    if config.get("candidates") != ["no_change", "momentum", "mean_reversion", "rival_linkage"]:
        raise ValueError("config must declare the four implemented fixed candidates in order")
    source = {"path": str(Path(input_path).resolve()), "sha256": sha256(input_path),
              "bytes": Path(input_path).stat().st_size}
    config_hash = sha256(config_path)
    states, replay = replay_snapshots(input_path, config)
    rows, exclusions = build_records(states, config)
    scores = {key: evaluate_records(values) if values else
              {"status": "no_eligible_observations", "counts": {"records": 0, "markets": 0, "days": 0}}
              for key, values in rows.items()}
    for delay, score in scores.items():
        if float(delay) > 0 and score["status"] == "ok":
            # The later reference cancels in MAE, but its sign cannot inform a
            # delayed decision. Do not report this retrospective sign as accuracy.
            for group in [score, *score["per_day"].values()]:
                for metric in group["models"].values():
                    metric["directional_accuracy"] = None
                    metric["race_balanced_directional_accuracy"] = None
            score["direction_note"] = "suppressed: undelayed decision reference unavailable to delayed forecast"
    dates = sorted({row["event_date"] for values in rows.values() for row in values})
    root = Path(__file__).resolve().parent.parent
    code = {p.relative_to(root).as_posix(): sha256(p) for p in sorted((root / "betangel_research").glob("*.py"))}
    revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True)
    dirty = subprocess.run(["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True)
    report = {
        "schema_version": 1,
        "status": "exploratory_forecast_replay_not_profitability_evidence",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": source, "config": config, "config_sha256": config_hash,
        "code_sha256": code, "git_revision": revision.stdout.strip() if revision.returncode == 0 else None,
        "working_tree_dirty": bool(dirty.stdout) if dirty.returncode == 0 else None,
        "python": platform.python_version(), "replay": replay,
        "scores_by_delay_seconds": scores, "exclusions_by_delay_seconds": exclusions,
        "independent_event_dates": dates,
        "available_chronological_folds": chronological_folds(dates),
        "holdout_status": "not_evaluated; public samples and these results are already inspected",
        "forecast_variants_reported": len(config["candidates"]) * len(config["delays_s"]),
        "economic_result": None,
        "limitations": [
            "LTP is last traded price, not an available back/lay quote. No fills or P&L are inferred.",
            "Normalized inverse-LTP share is a price index, not a validated outcome probability.",
            "pt is publisher time; actual receipt time is unavailable. Delays are injected scenarios.",
            "One meeting and multiple packages of the same races are not independent replications.",
            "No fitted model, strategy selection, confidence interval or profitability test is performed.",
            "All delays use a common eligible-market cohort; exclusions include future label availability and must be examined.",
            "Snapshot age measures market-message freshness; LTP can be older than that message.",
        ],
        "ltp_age_seconds_by_delay": {},
        "elapsed_seconds": round(time.perf_counter() - started, 4),
    }
    for delay, values in rows.items():
        ages = [row["ltp_age_s"] for row in values]
        report["ltp_age_seconds_by_delay"][delay] = {
            "min": min(ages), "median": statistics.median(ages), "max": max(ages)} if ages else None
    output.mkdir(parents=True)
    (output / "report.json").write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    with (output / "predictions.csv").open("w", newline="") as handle:
        names = ["delay_s", "market_id", "selection_id", "event_date", "decision_time", "data_cutoff",
                 "latest_feature_pt", "ltp_age_s", "actual_change", *config["candidates"]]
        writer = csv.DictWriter(handle, fieldnames=names)
        writer.writeheader()
        for delay, values in rows.items():
            for row in values:
                writer.writerow({"delay_s": delay, **{k: v for k, v in row.items() if k != "predictions"},
                                 **row["predictions"]})
    lines = ["# Exploratory forecast replay", "", "No execution or profitability estimate.", "",
             "| Delay (s) | Candidate | Race-balanced MAE (probability-share points) | Direction accuracy |",
             "| --- | --- | --- | --- |"]
    for delay, score in scores.items():
        for name, metric in score.get("models", {}).items():
            direction = metric["race_balanced_directional_accuracy"]
            formatted = "not reported" if direction is None else f"{direction:.3f}"
            lines.append(f"| {delay} | {name} | {100 * metric['race_balanced_mae']:.6f} | {formatted} |")
    lines.extend(["", *[f"- {item}" for item in report["limitations"]], ""])
    (output / "summary.md").write_text("\n".join(lines))
    ledger = {"created_at": report["created_at"], "experiment_id": config["experiment_id"],
              "source_sha256": source["sha256"], "config_sha256": config_hash,
              "code_sha256": code, "report": str((output / "report.json").resolve()),
              "report_sha256": sha256(output / "report.json"), "status": report["status"]}
    with (output.parent / "run-ledger.jsonl").open("a") as handle:
        handle.write(json.dumps(ledger, allow_nan=False) + "\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    replay = commands.add_parser("replay", help="Score fixed forecasts on a local historical stream")
    replay.add_argument("--input", required=True)
    replay.add_argument("--config", default="research/experiments/sample-preoff-v1.json")
    replay.add_argument("--out", required=True)
    motifs = commands.add_parser("motifs", help="Trace offline motif hypotheses; no orders or scoring")
    motifs.add_argument("--input", required=True)
    motifs.add_argument("--format", choices=["synthetic", "adv"], default="synthetic")
    motifs.add_argument("--context", help="Optional local structured context JSON list")
    motifs.add_argument("--config", default="research/experiments/motif-prototypes-v1.json")
    motifs.add_argument("--out", required=True)
    args = parser.parse_args()
    try:
        if args.command == "motifs":
            from .motifs import run_motif_lab
            report = run_motif_lab(args.input, args.config, args.out, args.format, args.context)
        else:
            report = run_replay(args.input, args.config, args.out)
    except (ValueError, KeyError, OSError) as exc:
        parser.exit(2, f"Research input error: {exc}\n")
    if args.command == "motifs":
        print(json.dumps({"status": report["status"], "output": args.out,
                          "feature_records": report["feature_records"], "summary": report["summary"],
                          "elapsed_seconds": report["elapsed_seconds"]}, indent=2))
        return
    print(json.dumps({"status": report["status"], "output": args.out,
                      "counts_by_delay": {k: v["counts"] for k, v in report["scores_by_delay_seconds"].items()},
                      "elapsed_seconds": report["elapsed_seconds"]}, indent=2))


if __name__ == "__main__":
    main()
