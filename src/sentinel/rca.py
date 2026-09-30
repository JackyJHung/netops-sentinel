"""Root-cause ranking for an incident.

Each alerted node is scored on three signals an on-call engineer would use:

  explain   - what fraction of the *other* alerted nodes sit downstream of it
              (a failing switch explains its services' errors, not vice versa)
  earliness - how soon it alerted relative to the incident start
  intensity - how loud it is: alert count, critical alerts, strong log evidence

The weighted sum gives a ranked list with a human-readable explanation.
"""

from __future__ import annotations

from .correlation import Incident
from .topology import Topology

WEIGHTS = {"explain": 0.45, "earliness": 0.35, "intensity": 0.20}


def rank_root_causes(incident: Incident, topo: Topology, weights: dict | None = None, top_k: int = 3) -> list[dict]:
    w = weights or WEIGHTS
    nodes = incident.nodes
    start, span = incident.start, max(incident.end - incident.start, 1)
    first_alert = {n: min(a.start for a in incident.alerts if a.node == n) for n in nodes}

    raw_intensity = {}
    for n in nodes:
        mine = [a for a in incident.alerts if a.node == n]
        crit = sum(a.severity == "critical" for a in mine)
        logs = sum(a.detector == "log_template" for a in mine)
        raw_intensity[n] = len(mine) + crit + 0.5 * logs
    max_int = max(raw_intensity.values()) or 1.0

    ranked = []
    for n in nodes:
        others = [m for m in nodes if m != n]
        downstream = topo.downstream(n)
        explained = [m for m in others if m in downstream]
        explain = len(explained) / len(others) if others else 1.0
        # A node that is itself downstream of another alerted node is less likely to be root.
        upstream_alerted = [m for m in others if m in topo.upstream(n) and first_alert[m] <= first_alert[n]]
        if upstream_alerted:
            explain *= 0.5
        earliness = 1.0 - (first_alert[n] - start) / span
        intensity = raw_intensity[n] / max_int
        score = w["explain"] * explain + w["earliness"] * earliness + w["intensity"] * intensity

        reasons = [f"first alert at +{first_alert[n] - start} min"]
        if explained:
            reasons.append(f"upstream of {len(explained)} other alerting node(s): {', '.join(sorted(explained))}")
        if upstream_alerted:
            reasons.append(f"depends on alerting node(s) {', '.join(sorted(upstream_alerted))}")
        ranked.append(
            {
                "node": n,
                "score": round(score, 3),
                "explain": round(explain, 3),
                "earliness": round(earliness, 3),
                "intensity": round(intensity, 3),
                "reason": "; ".join(reasons),
            }
        )
    ranked.sort(key=lambda r: (-r["score"], r["node"]))
    return ranked[:top_k]
