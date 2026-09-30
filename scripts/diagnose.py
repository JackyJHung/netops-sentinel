"""Where do false positives and RCA misses come from?

    python scripts/diagnose.py                      # tuning split, hard scenario
    python scripts/diagnose.py --scenario clean
    python scripts/diagnose.py --split test         # reporting only: never tune on this

False-positive incidents (no fault explains them) are attributed to the first
matching cause:
  benign          overlaps a benign event on one of its nodes
  after_blackout  on a node whose telemetry just came back from a blackout
  log_only        every alert is a log alert
  metric_noise    anything else

RCA top-1 misses (fault detected, but its root is not first once other true
roots in the same incident are set aside) are attributed to:
  merged          the incident also holds another fault's evidence
  root_silent     the fault knocked its root off the network and it was not recovered as a candidate
  root_absent     the root is not a candidate in the incident for another reason
  outranked       the root is a candidate but another node scored higher

Wrong merges (two concurrent faults on unrelated branches judged on the same
incident) are listed with their onset gap.
"""

from __future__ import annotations

import argparse
from collections import Counter

from sentinel.evaluation import ABLATION, PRE_SLACK, SPLITS, _detects, _hits_benign, _matches, score_day
from sentinel.pipeline import run_pipeline
from sentinel.simulator import SCENARIOS, simulate
from sentinel.topology import Topology


def fp_cause(inc, sim) -> str:
    if any(_hits_benign(inc, b) for b in sim.benign):
        return "benign"
    if any(b.node in inc.nodes and b.end - PRE_SLACK <= inc.start <= b.end + 30 for b in sim.blackouts):
        return "after_blackout"
    if all(a.detector == "log_template" for a in inc.alerts):
        return "log_only"
    return "metric_noise"


def rca_miss_cause(f, main, sim, topo, silenced) -> str:
    cands = [c["node"] for c in main.root_causes]
    if f.fault_id in silenced and f.root not in cands:
        return "root_silent"
    if f.root not in cands:
        return "root_absent"
    others = [g for g in sim.faults if g is not f and _detects(main, g, topo, silenced)]
    return "merged" if others else "outranked"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--split", choices=sorted(SPLITS), default="tune")
    ap.add_argument("--scenario", choices=list(SCENARIOS), default="hard")
    ap.add_argument("--config", choices=list(ABLATION), default="sentinel")
    ap.add_argument("-v", "--verbose", action="store_true", help="print every false-positive incident")
    args = ap.parse_args()

    topo = Topology.default()
    fp, miss, templates, merges = Counter(), Counter(), Counter(), []
    n_inc = n_fp = n_det = 0
    for seed in SPLITS[args.split]:
        sim = simulate(topo, seed=seed, **SCENARIOS[args.scenario])
        res = run_pipeline(sim, topo, ABLATION[args.config])
        silenced = {b.cause for b in sim.blackouts if b.cause}
        n_inc += len(res.incidents)
        for inc in res.incidents:
            if any(_matches(inc, f, topo) for f in sim.faults):
                continue
            n_fp += 1
            cause = fp_cause(inc, sim)
            fp[cause] += 1
            for a in inc.alerts:
                if a.detector == "log_template":
                    templates[(cause, a.description.split(": ", 1)[-1].rsplit(" x", 1)[0])] += 1
            if args.verbose:
                sigs = ", ".join(sorted({f"{a.node}:{a.signal}" for a in inc.alerts}))
                print(f"  seed {seed} {inc.incident_id} t={inc.start}-{inc.end} [{cause}] {sigs}")
        # Per-fault outcomes come from the evaluation itself, so this script never disagrees with the benchmark.
        day = score_day(sim, res, topo)
        by_id = {i.incident_id: i for i in res.incidents}
        faults = {f.fault_id: f for f in sim.faults}
        for o in day.faults:
            if not o.detected:
                continue
            n_det += 1
            if not o.top1:
                f, main_inc = faults[o.fault_id], by_id[o.incident_id]
                cause = rca_miss_cause(f, main_inc, sim, topo, silenced)
                miss[cause] += 1
                if args.verbose:
                    ranked = [c["node"] for c in main_inc.root_causes]
                    print(f"  seed {seed} {f.fault_id} {f.kind} root={f.root} [{cause}] rank={o.rank} ranked={ranked[:4]}")
        home = {o.fault_id: o.incident_id for o in day.faults if o.detected}
        for i, f in enumerate(sim.faults):
            for g in sim.faults[i + 1 :]:
                related = f.root in topo.upstream(g.root) or g.root in topo.upstream(f.root)
                concurrent = f.start < g.end and g.start < f.end
                if concurrent and not related and f.fault_id in home and home[f.fault_id] == home.get(g.fault_id):
                    merges.append((seed, f, g, by_id[home[f.fault_id]]))

    print(f"{args.config} on {args.split} split, {args.scenario} scenario")
    print(f"\nFalse-positive incidents: {n_fp} of {n_inc} ({n_fp / max(n_inc, 1):.0%})")
    for cause, n in fp.most_common():
        print(f"  {cause:<16} {n:>4}  ({n / max(n_fp, 1):.0%} of false positives)")
    if templates:
        print("\nLog templates in false-positive incidents:")
        for (cause, text), n in templates.most_common(10):
            print(f"  {n:>4}  [{cause}] {text}")
    n_miss = sum(miss.values())
    print(f"\nRCA top-1 misses: {n_miss} of {n_det} detected faults ({n_miss / max(n_det, 1):.0%})")
    for cause, n in miss.most_common():
        print(f"  {cause:<16} {n:>4}  ({n / max(n_miss, 1):.0%} of misses)")
    print(f"\nWrong merges (unrelated concurrent faults in one incident): {len(merges)}")
    for seed, f, g, inc in merges:
        first = {n: min(a.start for a in inc.alerts if a.node == n) for n in inc.nodes}
        print(
            f"  seed {seed} {f.fault_id} {f.kind}@{f.root} (first alert {first.get(f.root, '-')}) + "
            f"{g.fault_id} {g.kind}@{g.root} (first alert {first.get(g.root, '-')}); fault starts {f.start}/{g.start}"
        )


if __name__ == "__main__":
    main()
