"""Dependency topology: which devices and services depend on which.

Edges point upstream (child -> parent). A fault on a node can propagate to
everything *downstream* of it, which is what root-cause analysis exploits.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import yaml

NETWORK_METRICS = ("cpu_pct", "mem_pct", "latency_ms", "packet_loss_pct")
SERVICE_METRICS = ("cpu_pct", "mem_pct", "latency_ms", "error_rate_pct")

DEFAULT_TOPOLOGY = Path(__file__).resolve().parent / "data" / "topology.yaml"


@dataclass(frozen=True)
class Node:
    name: str
    kind: str  # router | switch | service
    depends_on: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_network(self) -> bool:
        return self.kind in ("router", "switch")

    @property
    def metrics(self) -> tuple[str, ...]:
        return NETWORK_METRICS if self.is_network else SERVICE_METRICS


class Topology:
    def __init__(self, nodes: list[Node]):
        self.nodes: dict[str, Node] = {n.name: n for n in nodes}
        for n in nodes:
            for parent in n.depends_on:
                if parent not in self.nodes:
                    raise ValueError(f"{n.name} depends on unknown node {parent}")
        self._children: dict[str, list[str]] = {name: [] for name in self.nodes}
        for n in nodes:
            for parent in n.depends_on:
                self._children[parent].append(n.name)
        self._check_acyclic()

    # ---------- construction ----------
    @classmethod
    def from_yaml(cls, path: str | Path = DEFAULT_TOPOLOGY) -> "Topology":
        data = yaml.safe_load(Path(path).read_text())
        return cls([Node(d["name"], d["kind"], tuple(d.get("depends_on", []))) for d in data["nodes"]])

    @classmethod
    def default(cls) -> "Topology":
        return cls.from_yaml(DEFAULT_TOPOLOGY)

    def _check_acyclic(self) -> None:
        for name in self.nodes:
            if name in self.upstream(name):
                raise ValueError(f"dependency cycle through {name}")

    # ---------- graph queries ----------
    def __iter__(self):
        return iter(self.nodes.values())

    def __len__(self) -> int:
        return len(self.nodes)

    def upstream(self, name: str) -> set[str]:
        """Everything `name` transitively depends on."""
        seen: set[str] = set()
        stack = list(self.nodes[name].depends_on)
        while stack:
            cur = stack.pop()
            if cur not in seen:
                seen.add(cur)
                stack.extend(self.nodes[cur].depends_on)
        return seen

    def downstream(self, name: str) -> dict[str, int]:
        """Everything that transitively depends on `name`, mapped to hop distance."""
        hops: dict[str, int] = {}
        queue = deque([(c, 1) for c in self._children[name]])
        while queue:
            cur, d = queue.popleft()
            if cur not in hops or d < hops[cur]:
                hops[cur] = d
                queue.extend((c, d + 1) for c in self._children[cur])
        return hops

    def distance(self, a: str, b: str) -> int:
        """Undirected hop distance (large number if disconnected)."""
        if a == b:
            return 0
        queue = deque([(a, 0)])
        seen = {a}
        while queue:
            cur, d = queue.popleft()
            neighbours = list(self.nodes[cur].depends_on) + self._children[cur]
            for nb in neighbours:
                if nb == b:
                    return d + 1
                if nb not in seen:
                    seen.add(nb)
                    queue.append((nb, d + 1))
        return 10**6

    def related(self, a: str, b: str, max_hops: int = 2) -> bool:
        """True if two nodes could plausibly be part of the same incident."""
        return (
            a == b
            or b in self.upstream(a)
            or a in self.upstream(b)
            or self.distance(a, b) <= max_hops
        )

    def to_dict(self) -> dict:
        return {
            "nodes": [
                {"name": n.name, "kind": n.kind, "depends_on": list(n.depends_on)} for n in self
            ]
        }
