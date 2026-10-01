"""Print the README results tables from a benchmark report, so no number is typed by hand.

    python scripts/readme_tables.py                 # reports/benchmark-test.json
    python scripts/readme_tables.py reports/benchmark-tune.json
    python scripts/readme_tables.py --update-readme  # rewrite the tables in README.md in place
"""

from __future__ import annotations

import json
import sys

LABELS = {
    "static_baseline": "Static thresholds (baseline)",
    "robust_z+ewma": "Robust z + EWMA",
    "+iforest": "+ Isolation Forest",
    "+forecast": "+ Saturation forecast",
    "+log mining": "+ Log mining",
    "+incident splitting": "+ Incident splitting",
    "+silent nodes": "+ Silent-node evidence",
    "+multi-root RCA": "+ Multi-root RCA",
    "+calibrated log bursts": "+ Calibrated log bursts",
    "+change events": "+ Change events (RCA signal)",
    "sentinel": "+ Change-aware paging (full)",
}
HEADERS = {
    "precision": "Precision", "recall": "Recall", "f1": "F1", "mttd_min": "MTTD (min)", "rca_top1": "RCA top-1",
    "rca_top3": "RCA top-3", "classification_acc": "Runbook match", "alert_compression": "Alerts per incident",
    "wrong_merge": "Wrong merges", "benign_paged": "Benign paged",
}
CLEAN = [("precision", 2, True), ("recall", 2, True), ("f1", 2, True), ("mttd_min", 1, True), ("rca_top1", 2, False),
         ("classification_acc", 2, True), ("alert_compression", 1, False)]
HARD = [("precision", 2, True), ("recall", 2, True), ("mttd_min", 1, True), ("rca_top1", 2, True), ("rca_top3", 2, True),
        ("classification_acc", 2, True), ("wrong_merge", 2, True), ("benign_paged", 2, True)]


def cell(m: dict, digits: int, ci: bool) -> str:
    if m["mean"] is None:
        return "n/a"
    out = f"{m['mean']:.{digits}f}"
    return f"{out} [{m['lo']:.{digits}f}, {m['hi']:.{digits}f}]" if ci and m["lo"] is not None else out


def table(configs: dict, cols) -> str:
    head = "| Configuration | " + " | ".join(HEADERS[c] for c, _, _ in cols) + " |"
    lines = [head, "|---|" + "---|" * len(cols)]
    for name, cfg in configs.items():
        o = cfg["overall"]
        lines.append(f"| {LABELS.get(name, name)} | " + " | ".join(cell(o[c], d, ci) for c, d, ci in cols) + " |")
    return "\n".join(lines)


def tables(path: str = "reports/benchmark-test.json") -> dict[str, str]:
    report = json.load(open(path))
    return {name: table(sc["configs"], CLEAN if name == "clean" else HARD) for name, sc in report["scenarios"].items()}


def update_readme(readme: str = "README.md", path: str = "reports/benchmark-test.json") -> None:
    """Replace the table under each '### Clean scenario' / '### Hard scenario' heading in place."""
    lines = open(readme).read().split("\n")
    for name, tbl in tables(path).items():
        head = next(i for i, ln in enumerate(lines) if ln.lower().startswith(f"### {name} scenario"))
        start = next(i for i in range(head, len(lines)) if lines[i].startswith("| Configuration |"))
        end = next(i for i in range(start, len(lines)) if not lines[i].startswith("|"))
        lines[start:end] = tbl.split("\n")
    open(readme, "w").write("\n".join(lines))


def main() -> None:
    args = [a for a in sys.argv[1:] if a != "--update-readme"]
    path = args[0] if args else "reports/benchmark-test.json"
    if "--update-readme" in sys.argv:
        update_readme(path=path)
        return
    report = json.load(open(path))
    for name, sc in report["scenarios"].items():
        print(f"## {name} ({sc['faults_per_day']} faults/day)\n")
        print(table(sc["configs"], CLEAN if name == "clean" else HARD))
        print(f"\nSample sizes (full pipeline): {sc['configs']['sentinel']['counts']}\n")


if __name__ == "__main__":
    main()
