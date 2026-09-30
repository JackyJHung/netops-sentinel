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

RCA top-1 misses (fault detected, but its root is not ranked first) are
attributed to:
  merged          the incident also holds another fault's evidence
  root_silent     the fault knocked its root off the network, so the root never alerts
  root_absent     the root has no alert in the incident for another reason
  outranked       the root alerted but another node scored higher
"""

from __future__ import annotations

import argparse
from collections import Counter

from sentinel.evaluation import ABLATION, PRE_SLACK, SPLITS, _detects, _hits_benign, _matches
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
    others = [g for g in sim.faults if g is not f and _detects(main, g, topo, silenced)]
    if others:
        return "merged"
    if f.fault_id in silenced:
        return "root_silent"
    if f.root not in main.nodes:
        return "root_absent"
    return "outranked"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--split", choices=sorted(SPLITS), default="tune")
    ap.add_argument("--scenario", choices=list(SCENARIOS), default="hard")
    ap.add_argument("--config", choices=list(ABLATION), default="sentinel")
    ap.add_argument("-v", "--verbose", action="store_true", help="print every false-positive incident")
    args = ap.parse_args()

    topo = Topology.default()
    fp, miss, templates = Counter(), Counter(), Counter()
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
        for f in sim.faults:
            hits = [i for i in res.incidents if _detects(i, f, topo, silenced)]
            if not hits:
                continue
            n_det += 1
            main_inc = max(hits, key=lambda i: len(i.alerts))
            if not main_inc.root_causes or main_inc.root_causes[0]["node"] != f.root:
                miss[rca_miss_cause(f, main_inc, sim, topo, silenced)] += 1

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


if __name__ == "__main__":
    main()
