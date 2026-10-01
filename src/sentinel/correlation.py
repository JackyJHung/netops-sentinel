"""Alert correlation: collapse an alert storm into a handful of incidents.

Two alerts belong to the same incident when they overlap in time (within a
window) AND their nodes are topologically related (same node, one depends on
the other, or within a few hops). Groups are the connected components of that
relation, computed with union-find.

That relation is transitive, so two concurrent faults on unrelated branches
get chained together through the services they both feed. Each group is
therefore split again (`split=True`):

  1. Origins are the nodes with no upstream node in the group that alerted
     (or went silent) before them: the places a fault could have started.
  2. Origins stay together if one depends on the other, or if they started
     within `split_gap` minutes of each other (possibly one unseen cause).
     Otherwise they are separate faults.
  3. Every other alert goes to the origin cluster upstream of it whose onset
     most recently preceded the alert.

Silences (a node whose telemetry stopped entirely) are not alerts, but they
are evidence: a switch that stops reporting just before everything below it
alerts is the likely cause. Relevant silences are attached to incidents and
act as origins when splitting.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

import pandas as pd

from .detection import Alert, intervals
from .topology import Topology

ORIGIN_TOL = 2  # minutes of detection jitter allowed when deciding who alerted first
SILENCE_LEAD = 15  # a silence can start up to this long before a downstream alert and still explain it
SILENCE_LAG = 3  # ... or this long after it


@dataclass(frozen=True)
class Silence:
    """A window where every metric of a node was missing."""

    node: str
    start: int
    end: int

    def to_dict(self) -> dict:
        return asdict(self)


def detect_silences(metrics: pd.DataFrame, min_len: int = 5) -> list[Silence]:
    """Runs of at least `min_len` minutes where a node reported no metric at all.
    One missing series is a gap; every series missing at once means the node went dark."""
    out: list[Silence] = []
    for node in dict.fromkeys(metrics.columns.get_level_values(0)):
        dark = metrics[node].isna().to_numpy().all(axis=1)
        out += [Silence(node, a, b) for a, b in intervals(dark, min_len=min_len, max_gap=0)]
    return sorted(out, key=lambda s: (s.start, s.node))


@dataclass
class Incident:
    incident_id: str
    alerts: list[Alert]
    root_causes: list[dict] = field(default_factory=list)  # ranked candidates; "declared" marks the roots
    runbook: dict | None = None  # runbook for the top root cause
    runbooks: dict[str, dict] = field(default_factory=dict)  # runbook per declared root
    summary: str = ""
    silences: list[Silence] = field(default_factory=list)
    changes: list = field(default_factory=list)  # ChangeEvents on incident nodes shortly before they alerted
    # paging decision (see paging.py); by default an incident pages as soon as it opens
    held: bool = False
    suppressed: bool = False
    paged_at: int | None = None
    hold_change: object | None = None  # the change that triggered the hold

    @property
    def start(self) -> int:
        return min(a.start for a in self.alerts)

    @property
    def end(self) -> int:
        return max(a.end for a in self.alerts)

    @property
    def nodes(self) -> list[str]:
        return sorted({a.node for a in self.alerts})

    @property
    def severity(self) -> str:
        return "critical" if any(a.severity == "critical" for a in self.alerts) else "warning"

    @property
    def root_cause(self) -> str | None:
        return self.root_causes[0]["node"] if self.root_causes else None

    @property
    def roots(self) -> list[str]:
        """Every declared root cause (more than one when concurrent faults share the incident)."""
        return [c["node"] for c in self.root_causes if c.get("declared")] or ([self.root_cause] if self.root_cause else [])

    def to_dict(self, include_alerts: bool = True) -> dict:
        d = {
            "incident_id": self.incident_id,
            "start": self.start,
            "end": self.end,
            "severity": self.severity,
            "nodes": self.nodes,
            "n_alerts": len(self.alerts),
            "root_cause": self.root_cause,
            "roots": self.roots,
            "root_causes": self.root_causes,
            "runbook": self.runbook,
            "runbooks": self.runbooks,
            "silences": [s.to_dict() for s in self.silences],
            "changes": [c.to_dict(truth=False) for c in self.changes],
            "held": self.held,
            "suppressed": self.suppressed,
            "paged_at": self.paged_at if self.paged_at is not None or self.suppressed else self.start,
            "summary": self.summary,
        }
        if include_alerts:
            d["alerts"] = [a.to_dict() for a in sorted(self.alerts, key=lambda a: a.start)]
        return d


class _UnionFind:
    def __init__(self, n: int):
        self.parent = list(range(n))

    def find(self, i: int) -> int:
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def _relevant_silences(group: list[Alert], silences: list[Silence], topo: Topology) -> list[Silence]:
    """Silences that could explain this group: the node itself alerted around then, or it sits upstream of
    an alerting node and went dark shortly before that node started alerting."""
    out = []
    for s in silences:
        for a in group:
            upstream_or_self = s.node == a.node or s.node in topo.upstream(a.node)
            if upstream_or_self and a.start - SILENCE_LEAD <= s.start <= a.start + SILENCE_LAG:
                out.append(s)
                break
    return out


def _split(group: list[Alert], silences: list[Silence], topo: Topology, gap: int) -> list[tuple[list[Alert], list[Silence]]]:
    onset: dict[str, int] = {}
    for a in group:
        onset[a.node] = min(onset.get(a.node, a.start), a.start)
    for s in silences:
        onset[s.node] = min(onset.get(s.node, s.start), s.start)
    up = {n: topo.upstream(n) for n in onset}
    origins = [n for n in onset if not any(m in up[n] and onset[m] <= onset[n] + ORIGIN_TOL for m in onset if m != n)]

    uf = _UnionFind(len(origins))
    for i, a in enumerate(origins):
        for j in range(i + 1, len(origins)):
            b = origins[j]
            if a in up[b] or b in up[a] or abs(onset[a] - onset[b]) <= gap:
                uf.union(i, j)
    clusters: dict[int, list[str]] = {}
    for i, o in enumerate(origins):
        clusters.setdefault(uf.find(i), []).append(o)
    if len(clusters) <= 1:
        return [(group, silences)]

    members = list(clusters.values())
    cluster_onset = [min(onset[o] for o in m) for m in members]

    def home(node: str, t: int) -> int:
        cands = [k for k, m in enumerate(members) if any(o == node or o in up[node] for o in m)] or list(range(len(members)))
        before = [k for k in cands if cluster_onset[k] <= t + ORIGIN_TOL]
        return max(before, key=lambda k: cluster_onset[k]) if before else min(cands, key=lambda k: cluster_onset[k])

    parts: list[tuple[list[Alert], list[Silence]]] = [([], []) for _ in members]
    for a in group:
        parts[home(a.node, a.start)][0].append(a)
    for s in silences:
        parts[home(s.node, s.start)][1].append(s)
    return [p for p in parts if p[0]]


def correlate(
    alerts: list[Alert],
    topo: Topology,
    window: int = 10,
    max_hops: int = 2,
    split: bool = True,
    split_gap: int = 1,
    silences: list[Silence] | None = None,
) -> list[Incident]:
    if not alerts:
        return []
    alerts = sorted(alerts, key=lambda a: a.start)
    uf = _UnionFind(len(alerts))
    related_cache: dict[tuple[str, str], bool] = {}

    for i, a in enumerate(alerts):
        for j in range(i + 1, len(alerts)):
            b = alerts[j]
            if b.start > a.end + window:
                break  # sorted by start: nothing later can overlap `a`
            key = (a.node, b.node)
            if key not in related_cache:
                related_cache[key] = topo.related(a.node, b.node, max_hops)
            if related_cache[key]:
                uf.union(i, j)

    groups: dict[int, list[Alert]] = {}
    for i, a in enumerate(alerts):
        groups.setdefault(uf.find(i), []).append(a)

    parts: list[tuple[list[Alert], list[Silence]]] = []
    for g in groups.values():
        sil = _relevant_silences(g, silences or [], topo)
        parts += _split(g, sil, topo, split_gap) if split else [(g, sil)]

    parts.sort(key=lambda p: min(a.start for a in p[0]))
    return [Incident(f"INC-{k + 1:04d}", g, silences=sil) for k, (g, sil) in enumerate(parts)]
