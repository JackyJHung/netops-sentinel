"""Which log alerts are false positives, and which ones are we actually relying on?

    python scripts/diagnose_logs.py                   # tuning split, both scenarios
    python scripts/diagnose_logs.py --split test      # reporting only: never tune on this

Part 1 labels every log alert:
  fault   on a node in a fault's blast radius, inside the fault window (with slack)
  benign  on the node of a benign event, around it
  false   neither
and breaks them down by template and trigger ("new event type", "burst", "surge").
For false alerts it also reports whether a metric alert on the same or a
dependency-related node overlapped it (would corroboration have kept it?).

Part 2 finds the faults that log mining is needed for: faults the metric-only
pipeline misses, or classifies with the wrong runbook, that the full pipeline
gets right, and the log templates responsible.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict

from sentinel.evaluation import ABLATION, POST_SLACK, PRE_SLACK, SPLITS, score_day
from sentinel.pipeline import run_pipeline
from sentinel.simulator import SCENARIOS, simulate
from sentinel.topology import Topology

CORROBORATION_WINDOW = 10


def label(alert, sim, topo) -> str:
    for f in sim.faults:
        blast = {f.root, *topo.downstream(f.root)}
        if alert.node in blast and alert.start <= f.end + POST_SLACK and alert.end >= f.start - PRE_SLACK:
            return "fault"
    for b in sim.benign:
        if alert.node == b.node and alert.start <= b.end + POST_SLACK and alert.end >= b.start - PRE_SLACK:
            return "benign"
    return "false"


def trigger(alert) -> str:
    return alert.description.split(":", 1)[0].split(" vs ")[0]  # "new event type" | "burst" | "surge"


def corroborated(alert, metric_alerts, topo) -> bool:
    for m in metric_alerts:
        dependency = m.node == alert.node or m.node in topo.upstream(alert.node) or alert.node in topo.upstream(m.node)
        near = m.start <= alert.end + CORROBORATION_WINDOW and m.end >= alert.start - CORROBORATION_WINDOW
        if dependency and near:
            return True
    return False


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--split", choices=sorted(SPLITS), default="tune")
    ap.add_argument("--config", choices=list(ABLATION), default="sentinel")
    args = ap.parse_args()
    topo = Topology.default()

    for scenario in SCENARIOS:
        rows: dict[tuple, Counter] = defaultdict(Counter)
        needed: Counter = Counter()
        rates: dict[str, list[float]] = defaultdict(list)
        n_fp_incidents = n_log_only_fp = 0
        for seed in SPLITS[args.split]:
            sim = simulate(topo, seed=seed, **SCENARIOS[scenario])
            cache: dict = {}
            full = run_pipeline(sim, topo, ABLATION[args.config], cache=cache)
            metric_only = run_pipeline(sim, topo, ABLATION["+forecast"], cache=cache)
            logs = [a for a in full.alerts if a.detector == "log_template"]
            metrics = [a for a in full.alerts if a.detector != "log_template"]
            for a in logs:
                text = full.miner.templates[a.signal[4:]].text
                key = (text, trigger(a), a.severity)
                lab = label(a, sim, topo)
                rows[key][lab] += 1
                if lab == "false":
                    rows[key]["false_corroborated"] += corroborated(a, metrics, topo)
                    if "baseline" in a.description:
                        rates[text].append(float(a.description.split("baseline ")[1].split("/")[0]))
            day = score_day(sim, full, topo)
            matched = {o.incident_id for o in day.faults if o.detected}
            for inc in full.incidents:
                is_fp = inc.incident_id not in matched and not any(
                    label(a, sim, topo) != "false" for a in inc.alerts
                )
                n_fp_incidents += is_fp
                n_log_only_fp += is_fp and all(a.detector == "log_template" for a in inc.alerts)
            base = {o.fault_id: o for o in score_day(sim, metric_only, topo).faults}
            by_id = {i.incident_id: i for i in full.incidents}
            for o in day.faults:
                b = base[o.fault_id]
                gain = "detection" if o.detected and not b.detected else "runbook" if o.cls_ok and not b.cls_ok else None
                if gain:
                    inc = by_id[o.incident_id]
                    tpls = sorted({full.miner.templates[a.signal[4:]].text for a in inc.alerts if a.detector == "log_template" and a.node == o.root})
                    needed[(gain, o.kind, " | ".join(tpls) or "(none on root)")] += 1

        print(f"\n=== {args.config}, {args.split} split, {scenario} scenario")
        print(f"False-positive incidents with no fault or benign evidence: {n_fp_incidents} ({n_log_only_fp} made only of log alerts)\n")
        print(f"{'fault':>6} {'benign':>6} {'false':>6} {'false+metric':>12}  trigger          sev       template")
        for (text, trig, sev), c in sorted(rows.items(), key=lambda kv: -kv[1]["false"]):
            print(f"{c['fault']:>6} {c['benign']:>6} {c['false']:>6} {c['false_corroborated']:>12}  {trig:<16} {sev:<9} {text[:70]}")
        if rates:
            print("\nBaseline rate (per 5 min) of templates with false bursts:")
            for text, r in rates.items():
                print(f"  {text[:60]:<60} min {min(r):.2f} median {sorted(r)[len(r) // 2]:.2f} max {max(r):.2f} (n={len(r)})")
        print("\nFaults that need log mining (metric-only pipeline misses them or picks the wrong runbook):")
        for (gain, kind, tpls), n in needed.most_common():
            print(f"  {n:>3} {gain:<9} {kind:<20} root templates: {tpls[:110]}")


if __name__ == "__main__":
    main()
