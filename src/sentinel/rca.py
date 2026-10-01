"""Root-cause ranking for an incident.

Each candidate node is scored on three signals an on-call engineer would use:

  explain   - what fraction of the *other* alerted nodes sit downstream of it
              (a failing switch explains its services' errors, not vice versa)
  earliness - how soon it alerted (or went silent) relative to the incident start
  intensity - how loud it is: alert count, critical alerts, strong log evidence

Candidates are the alerting nodes plus nodes that went silent (stopped
sending telemetry) around the incident: a switch that goes dark while
everything below it alerts is evidence, not an absence of evidence.

A fourth signal, change, rewards a node that had a deploy or config push
shortly before it started alerting (see `attach_changes`).

The weighted sum gives a ranked list with a human-readable explanation. With
concurrent faults one incident can have more than one root, so candidates are
also *declared* as roots when the roots already declared cannot explain them
(see `_declare_roots`).
"""

from __future__ import annotations

from .correlation import ORIGIN_TOL, Incident
from .topology import Topology

WEIGHTS = {"explain": 0.45, "earliness": 0.35, "intensity": 0.20, "change": 0.25}
LOCAL_SIGNALS = {"cpu_pct", "mem_pct"}  # resource saturation stays on the box; it does not cascade to dependents
MIN_ROOT_SCORE = 0.4  # a second root must score at least this fraction of the top candidate...
LOCAL_ROOT_SCORE = 0.1  # ...unless it has local evidence (CPU/memory symptoms or a recent change); see DESIGN D29
MAX_ROOTS = 3


def onsets(incident: Incident) -> dict[str, int]:
    """First alert (or silence) per node in the incident."""
    first: dict[str, int] = {}
    for a in incident.alerts:
        first[a.node] = min(first.get(a.node, a.start), a.start)
    for s in incident.silences:
        first[s.node] = min(first.get(s.node, s.start), s.start)
    return first


def attach_changes(incidents: list[Incident], changes: list, lookback: int = 15) -> None:
    """Attach the changes made to an incident's nodes in the `lookback` minutes before each node's first
    alert (or silence). A change logged up to ORIGIN_TOL after the onset still counts: change logs and
    monitoring clocks are not perfectly aligned."""
    for inc in incidents:
        onset = onsets(inc)
        inc.changes = [c for c in changes if c.node in onset and onset[c.node] - lookback <= c.t <= onset[c.node] + ORIGIN_TOL]


def describe_change(c, onset: int) -> tuple[str, str]:
    """("config push to dist-sw-2", "3 min before first alert")"""
    gap = onset - c.t
    when = f"{gap} min before first alert" if gap > 0 else "at the first alert" if gap == 0 else f"{-gap} min after first alert"
    return f"{c.kind.replace('_', ' ')} to {c.node}", when


def rank_root_causes(
    incident: Incident,
    topo: Topology,
    weights: dict | None = None,
    top_k: int = 5,
    multi_root: bool = True,
    local_root_floor: float = LOCAL_ROOT_SCORE,
) -> list[dict]:
    w = {**WEIGHTS, **(weights or {})}
    first = onsets(incident)
    went_silent: dict[str, tuple[int, int]] = {}
    for s in incident.silences:
        went_silent.setdefault(s.node, (s.start, s.end))
    changed = {}
    for c in sorted(incident.changes, key=lambda c: c.t):
        changed[c.node] = c  # the most recent change wins
    nodes = sorted(first)
    start = min(first.values())
    span = max(incident.end - start, 1)

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
        # A node that is itself downstream of another alerting (or silent) node is less likely to be root.
        # ORIGIN_TOL allows for detection and polling jitter, as in correlation.
        upstream_alerted = [m for m in others if m in topo.upstream(n) and first[m] <= first[n] + ORIGIN_TOL]
        if upstream_alerted:
            explain *= 0.5
        earliness = 1.0 - (first[n] - start) / span
        intensity = raw_intensity[n] / max_int
        change = 1.0 if n in changed else 0.0
        score = w["explain"] * explain + w["earliness"] * earliness + w["intensity"] * intensity + w["change"] * change

        reasons = []
        if n in changed:
            what, when = describe_change(changed[n], first[n])
            reasons.append(f"{what} {when}")
        if n in went_silent:
            s, e = went_silent[n]
            reasons.append(f"stopped reporting at +{s - start} min (no telemetry for {e - s} min)")
        if raw_intensity[n]:
            reasons.append(f"first alert at +{min(a.start for a in incident.alerts if a.node == n) - start} min")
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
                "silent": n in went_silent,
                "change": changed[n].change_id if n in changed else None,
                "declared": False,
                "reason": "; ".join(reasons),
            }
        )
    ranked.sort(key=lambda r: (-r["score"], r["node"]))
    _declare_roots(ranked, incident, topo, multi_root, local_root_floor)
    ranked.sort(key=lambda r: (not r["declared"], -r["score"], r["node"]))  # the declared roots are the answer
    return ranked[:top_k]


def _declare_roots(
    ranked: list[dict], incident: Incident, topo: Topology, multi_root: bool, local_root_floor: float = LOCAL_ROOT_SCORE
) -> None:
    """Mark the top candidate as a root, plus (if `multi_root`) any later candidate the declared roots cannot
    explain: one on an unrelated branch, or one downstream but with local evidence that does not propagate
    (CPU or memory saturation, or a change made to it just before it alerted).

    A candidate with local evidence only has to clear `local_root_floor` x top score: its evidence is about
    the node itself, so it should not need downstream alerts to score high enough (DESIGN D21)."""
    if not ranked:
        return
    ranked[0]["declared"] = True
    if not multi_root:
        return
    local = {a.node for a in incident.alerts if a.signal in LOCAL_SIGNALS} | {c["node"] for c in ranked if c.get("change")}
    declared = [ranked[0]["node"]]
    floor, local_floor = MIN_ROOT_SCORE * ranked[0]["score"], local_root_floor * ranked[0]["score"]
    for c in ranked[1:]:
        if len(declared) >= MAX_ROOTS:
            break
        n = c["node"]
        if c["score"] < (local_floor if n in local else floor):
            continue
        if any(d in topo.downstream(n) for d in declared):
            continue  # upstream of a declared root: it would explain that root, and it ranked lower
        explained = any(n in topo.downstream(d) for d in declared)
        if not explained or n in local:
            c["declared"] = True
            c["reason"] += "; not explained by the other root cause(s)" if not explained else "; local evidence (CPU/memory or a recent change) does not cascade"
            declared.append(n)
