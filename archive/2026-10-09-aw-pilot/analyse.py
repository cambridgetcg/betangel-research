"""Recompute the descriptive pilot offline: python analyse.py

Standard library writes analysis.json and derived.csv. Add --plot with matplotlib
installed to write a standalone PNG/SVG figure. No network or account access.
"""

import argparse
import csv
import hashlib
import json
from collections import defaultdict
from decimal import Decimal
from pathlib import Path


BASE = Path(__file__).resolve().parent


def analyse():
    meta = json.loads((BASE / "sources.json").read_text())
    with (BASE / "runners.csv").open(newline="") as f:
        rows = list(csv.DictReader(f))
    groups = defaultdict(list)
    for row in rows:
        groups[row["race_id"]].append(row)
    assert set(groups) == {r["race_id"] for r in meta["races"]}
    report = {
        "status": "Descriptive observations; no model fitted and no predictive edge measured.",
        "input_sha256": hashlib.sha256((BASE / "runners.csv").read_bytes()).hexdigest(),
        "races": [],
    }
    derived = []
    for race in meta["races"]:
        members = groups[race["race_id"]]
        n = race["starters"]
        assert len(members) == n
        assert len({r["horse"] for r in members}) == n
        assert sorted(int(r["finish_position"]) for r in members) == list(range(1, n + 1))
        errors = []
        implied = []
        for row in members:
            splits = [Decimal(row[f"f{i}_s"]) for i in range(1, 7)]
            assert all(s > 0 for s in splits)
            total = Decimal(row["total_time_s"])
            # Six rounded splits plus a rounded total: conservative rounding envelope.
            error = sum(splits) - total
            assert abs(error) <= Decimal("0.035"), (row["horse"], error)
            errors.append(float(abs(error)))
            closing = sum(splits[-2:])
            # Account for +/- 0.005s rounding of each displayed value and FSP.
            fsp = Decimal(row["fsp_pct"])
            fsp_low = (total - Decimal("0.005")) / (3 * (closing + Decimal("0.01"))) * 100
            fsp_high = (total + Decimal("0.005")) / (3 * (closing - Decimal("0.01"))) * 100
            assert fsp_low - Decimal("0.005") <= fsp <= fsp_high + Decimal("0.005"), row["horse"]
            numerator, denominator = map(Decimal, row["sp_fractional"].split("/"))
            decimal_odds = 1 + numerator / denominator
            assert decimal_odds > 1
            implied.append(1 / decimal_odds)
            row["final_2f_s"] = f"{closing:.2f}"
            row["first_4f_s"] = f"{sum(splits[:4]):.2f}"
            row["decimal_sp"] = str(decimal_odds)
        book = sum(implied)
        for row, p in zip(members, implied):
            row["normalised_sp_benchmark"] = f"{p / book:.8f}"
            derived.append({key: row[key] for key in [
                "race_id", "horse", "finish_position", "first_4f_s", "final_2f_s",
                "total_time_s", "fsp_pct", "decimal_sp", "normalised_sp_benchmark",
            ]})
        leader = max(members, key=lambda row: Decimal(row["fsp_pct"]))
        winner = next(row for row in members if row["finish_position"] == "1")
        report["races"].append({
            "race_id": race["race_id"], "starters": n,
            "complete_split_rows": n, "max_split_sum_error_s": max(errors),
            "sp_implied_probability_sum": float(book),
            "sp_overround_pct": float((book - 1) * 100),
            "highest_fsp_runner": leader["horse"],
            "highest_fsp_finish_position": int(leader["finish_position"]),
            "winner": winner["horse"],
            "winner_fsp_rank": 1 + sum(Decimal(r["fsp_pct"]) > Decimal(winner["fsp_pct"]) for r in members),
        })
    report["runner_count"] = len(rows)
    report["checks"] = [
        "Declared starter counts and complete placing sequences agree.",
        "Runner identities unique within each race.",
        "Reported totals and splits consistent within rounding precision.",
        "Reported FSP consistent with a two-furlong finish within rounding bounds.",
        "All starting prices parse; normalisation is performed within each race.",
    ]
    (BASE / "analysis.json").write_text(json.dumps(report, indent=2) + "\n")
    with (BASE / "derived.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(derived[0]))
        writer.writeheader()
        writer.writerows(derived)
    return groups, report


def plot(groups):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D

    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10})
    fig, axes = plt.subplots(1, 2, figsize=(13, 7.4), gridspec_kw={"wspace": .60})
    fig.patch.set_facecolor("#f8fafc")
    for ax, (race_id, members) in zip(axes, sorted(groups.items())):
        members = sorted(members, key=lambda r: int(r["finish_position"]))
        high = max(members, key=lambda r: Decimal(r["fsp_pct"]))["horse"]
        for y, row in enumerate(members):
            color = "#0f766e" if row["finish_position"] == "1" else "#a21caf" if row["horse"] == high else "#64748b"
            value = float(row["final_2f_s"])
            ax.scatter(value, y, s=65, color=color, zorder=3)
            ax.annotate(f"{value:.2f}s", (value, y), xytext=(7, 0), textcoords="offset points", va="center", color=color, fontsize=9)
        ax.set_yticks(range(len(members)), [f"{r['finish_position']}. {r['horse']}" for r in members])
        ax.set_xlim(22.5, 24.65)
        ax.set_ylim(len(members) - .5, -.5)
        ax.set_xticks([22.5, 23, 23.5, 24, 24.5])
        ax.set_xlabel("Final 2 furlongs (seconds) · shorter = faster", labelpad=12)
        ax.set_title("18:15 · Division 1" if race_id.endswith("1815") else "18:45 · Division 2", loc="left", fontsize=13, fontweight="bold", pad=18)
        ax.grid(axis="x", color="#e2e8f0")
        ax.set_axisbelow(True)
        ax.set_facecolor("#f8fafc")
        for spine in ax.spines.values():
            spine.set_visible(False)
        ax.tick_params(length=0, pad=8)
    fig.suptitle("Closing speed and finishing order", x=.07, y=.96, ha="left", fontsize=22, fontweight="bold", color="#0f172a")
    fig.text(.07, .902, "Kempton · 5 October 2026 · Class 4 · 6f Polytrack · 19 starters", fontsize=12, color="#475569")
    fig.legend(handles=[
        Line2D([], [], color="#0f766e", marker="o", linestyle="", label="Race winner"),
        Line2D([], [], color="#a21caf", marker="o", linestyle="", label="Highest finishing speed percentage (FSP)"),
    ], loc="lower left", bbox_to_anchor=(.062, .095), frameon=False, ncol=2)
    fig.text(.07, .075, "Descriptive sample only. All measurements were published after these races; this is not a prediction test.", fontsize=10, color="#475569")
    fig.text(.07, .045, "Source: Racing TV / RaceiQ results, 18:15 and 18:45. Closing times sum the displayed rounded splits.", fontsize=9, color="#64748b")
    fig.subplots_adjust(left=.17, right=.96, top=.82, bottom=.23)
    fig.savefig(BASE / "closing-speed.png", dpi=180, facecolor=fig.get_facecolor())
    fig.savefig(BASE / "closing-speed.svg", facecolor=fig.get_facecolor())
    plt.close(fig)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plot", action="store_true")
    args = parser.parse_args()
    groups, report = analyse()
    if args.plot:
        plot(groups)
    print(json.dumps(report, indent=2))
