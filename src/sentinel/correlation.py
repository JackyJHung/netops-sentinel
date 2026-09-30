"""Alert correlation: collapse an alert storm into a handful of incidents.

Two alerts belong to the same incident when they overlap in time (within a
window) AND their nodes are topologically related (same node, one depends on
the other, or within a few hops). Groups are the connected components of that
relation, computed with union-find.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .detection import Alert
from .topology import Topology


@dataclass
class Incident:
    incident_id: str
    alerts: list[Alert]
    root_causes: list[dict] = field(default_factory=list)  # ranked candidates
    runbook: dict | None = None
    summary: str = ""

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

    def to_dict(self, include_alerts: bool = True) -> dict:
        d = {
            "incident_id": self.incident_id,
            "start": self.start,
            "end": self.end,
            "severity": self.severity,
            "nodes": self.nodes,
            "n_alerts": len(self.alerts),
            "root_cause": self.root_cause,
            "root_causes": self.root_causes,
            "runbook": self.runbook,
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


def correlate(alerts: list[Alert], topo: Topology, window: int = 10, max_hops: int = 2) -> list[Incident]:
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

    incidents = sorted(groups.values(), key=lambda g: min(a.start for a in g))
    return [Incident(f"INC-{k + 1:04d}", g) for k, g in enumerate(incidents)]
