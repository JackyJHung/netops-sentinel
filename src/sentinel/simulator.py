"""Synthetic telemetry generator with labeled fault injection.

Produces one-minute metrics and syslog/app log lines for every node in a
topology, then injects faults whose effects cascade to downstream nodes with
a small lag. Every fault is recorded as ground truth so the detection pipeline
can be scored (precision, recall, time-to-detect, root-cause accuracy).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from .topology import Topology

FAULT_KINDS = ("cpu_saturation", "link_flap", "memory_leak", "latency_degradation")
DEFAULT_START = datetime(2026, 9, 1, 0, 0, 0)


@dataclass
class Fault:
    fault_id: str
    kind: str
    root: str
    start: int  # minute index
    duration: int
    intensity: float = 1.0  # 1.0 = hard failure, ~0.3 = subtle "gray" failure

    @property
    def end(self) -> int:
        return self.start + self.duration

    def to_dict(self) -> dict:
        return {**asdict(self), "end": self.end}


@dataclass
class LogLine:
    t: int  # minute index
    node: str
    message: str

    def render(self, start: datetime = DEFAULT_START) -> str:
        ts = (start + timedelta(minutes=self.t)).strftime("%Y-%m-%dT%H:%M:%S")
        return f"{ts} {self.node} {self.message}"


@dataclass
class SimulationResult:
    metrics: pd.DataFrame  # index: minute, columns: MultiIndex (node, metric)
    logs: list[LogLine]
    faults: list[Fault]
    minutes: int
    warmup: int
    seed: int
    start_time: datetime = field(default=DEFAULT_START)

    def series(self, node: str, metric: str) -> np.ndarray:
        return self.metrics[(node, metric)].to_numpy()

    def timestamp(self, t: int) -> datetime:
        return self.start_time + timedelta(minutes=int(t))


# --------------------------------------------------------------------------
# Baseline behaviour
# --------------------------------------------------------------------------
_BASE_LATENCY = {"router": 2.0, "switch": 1.0, "service": 40.0}


def _baseline(rng: np.random.Generator, node, metric: str, minutes: int) -> np.ndarray:
    t = np.arange(minutes)
    diurnal = np.sin(2 * np.pi * (t - 6 * 60) / 1440)  # peaks mid-afternoon
    if metric == "cpu_pct":
        base = rng.uniform(20, 35)
        x = base + 8 * diurnal + rng.normal(0, 2.0, minutes)
    elif metric == "mem_pct":
        base = rng.uniform(40, 60)
        x = base + 1.5 * diurnal + rng.normal(0, 0.6, minutes)
    elif metric == "latency_ms":
        base = _BASE_LATENCY[node.kind] * rng.uniform(0.8, 1.2)
        x = base * (1 + 0.15 * diurnal) * rng.lognormal(0, 0.08, minutes)
    elif metric == "packet_loss_pct":
        x = np.abs(rng.normal(0.02, 0.02, minutes))
    elif metric == "error_rate_pct":
        x = np.abs(rng.normal(0.3, 0.12, minutes))
    else:  # pragma: no cover
        raise ValueError(metric)
    return x


# --------------------------------------------------------------------------
# Log templates
# --------------------------------------------------------------------------
def _ip(rng) -> str:
    return ".".join(str(rng.integers(1, 255)) for _ in range(4))


def _normal_log(rng, node) -> str:
    if node.is_network:
        choice = rng.integers(0, 10)
        if choice < 6:
            return (
                f"%SEC-6-IPACCESSLOGP: list ACL-IN permitted tcp {_ip(rng)}({rng.integers(1024, 65535)})"
                f" -> {_ip(rng)}({rng.choice([22, 80, 443, 8443])}), 1 packet"
            )
        if choice < 9:
            return f"%NTP-6-PEERSYNC: NTP synced to peer {_ip(rng)}"
        return f"%SYS-5-CONFIG_I: Configured from console by admin on vty0 ({_ip(rng)})"
    choice = rng.integers(0, 10)
    if choice < 6:
        res = rng.choice(["users", "orders", "items", "sessions"])
        return f"INFO GET /api/v1/{res} 200 {rng.integers(5, 90)}ms"
    if choice < 9:
        return f"INFO session refreshed id={rng.integers(0, 16**8):08x}"
    return f"WARN retrying connection to metrics-exporter attempt={rng.integers(1, 3)}"


# --------------------------------------------------------------------------
# Fault effects
# --------------------------------------------------------------------------
def _apply_fault(rng, topo: Topology, data: dict, logs: list[LogLine], f: Fault, minutes: int):
    root = topo.nodes[f.root]
    s, e = f.start, min(f.end, minutes)
    span = np.arange(s, e)
    down = topo.downstream(f.root)

    k = f.intensity

    def bump(node: str, metric: str, lo: int, hi: int, add=None, mult=None):
        lo, hi = max(lo, 0), min(hi, minutes)
        if lo >= hi or (node, metric) not in data:
            return
        arr = data[(node, metric)]
        if add is not None:
            add = np.asarray(add, dtype=float) * k
        if mult is not None:
            mult = 1 + (np.asarray(mult, dtype=float) - 1) * k
        if mult is not None:
            arr[lo:hi] *= mult if mult.ndim == 0 else mult[: hi - lo]
        if add is not None:
            arr[lo:hi] += add if add.ndim == 0 else add[: hi - lo]

    def log(t: int, node: str, msg: str):
        # subtle faults leave sparser log evidence
        if 0 <= t < minutes and rng.random() < max(k, 0.25):
            logs.append(LogLine(int(t), node, msg))

    if f.kind == "cpu_saturation":
        bump(f.root, "cpu_pct", s, e, add=rng.uniform(50, 60))
        bump(f.root, "latency_ms", s, e, mult=3.0)
        for t in span[::2]:
            if root.is_network:
                log(t, f.root, f"%SYS-3-CPUHOG: Task is running for ({rng.integers(2000, 9000)})msecs, process = IP Input")
            else:
                log(t, f.root, f"WARN worker pool saturated active={rng.integers(190, 256)} queued={rng.integers(50, 400)}")
        for node, hops in down.items():
            bump(node, "latency_ms", s + hops, e + hops, mult=1 + 1.5 / hops)

    elif f.kind == "link_flap":
        pattern = ((np.arange(e - s) // 2) % 2 == 0).astype(float)  # 2 min down / 2 min up
        bump(f.root, "packet_loss_pct", s, e, add=rng.uniform(6, 12) * pattern)
        bump(f.root, "latency_ms", s, e, mult=1 + 2 * pattern)
        iface = f"GigabitEthernet{rng.integers(0, 2)}/{rng.integers(1, 48)}"
        for i, t in enumerate(span[::2]):
            state = "down" if i % 2 == 0 else "up"
            log(t, f.root, f"%LINK-3-UPDOWN: Interface {iface}, changed state to {state}")
            log(t, f.root, f"%LINEPROTO-5-UPDOWN: Line protocol on Interface {iface}, changed state to {state}")
        for node, hops in down.items():
            n = topo.nodes[node]
            lag = hops
            if n.is_network:
                bump(node, "packet_loss_pct", s + lag, e + lag, add=4.0 / hops * pattern)
            else:
                bump(node, "error_rate_pct", s + lag, e + lag, add=6.0 / hops * pattern)
                bump(node, "latency_ms", s + lag, e + lag, mult=1 + 1.0 / hops * pattern)
                for t in span[::4]:
                    log(t + lag, node, f"ERROR connection reset by peer {_ip(rng)}:{rng.choice([443, 5432, 6379])}")

    elif f.kind == "memory_leak":
        cur = data[(f.root, "mem_pct")][s]
        ramp = np.linspace(0, 97 - cur, e - s)
        bump(f.root, "mem_pct", s, e, add=ramp)
        tail = s + int(0.7 * (e - s))
        bump(f.root, "latency_ms", tail, e, mult=2.5)
        bump(f.root, "error_rate_pct", tail, e, add=rng.uniform(4, 8))
        for t in span[::3]:
            if t >= s + (e - s) // 3:
                log(t, f.root, f"WARN GC pause {rng.integers(400, 3000)}ms heap={rng.integers(85, 99)}%")
        if k > 0.7:
            for t in range(tail, e, 2):
                log(t, f.root, "ERROR java.lang.OutOfMemoryError: Java heap space")
        for node, hops in down.items():
            bump(node, "error_rate_pct", tail + hops, e + hops, add=3.0 / hops)

    elif f.kind == "latency_degradation":
        bump(f.root, "latency_ms", s, e, mult=rng.uniform(5, 8))
        for t in span[::2]:
            log(t, f.root, f"WARN slow query took {rng.integers(800, 5000)}ms table={rng.choice(['orders', 'users', 'items'])}")
        for node, hops in down.items():
            bump(node, "latency_ms", s + hops, e + hops, mult=1 + 3.0 / hops)
            bump(node, "error_rate_pct", s + hops, e + hops, add=3.0 / hops)
            for t in span[::3]:
                log(t + hops, node, f"ERROR upstream timeout calling {f.root} after {rng.integers(1000, 5000)}ms")
    else:  # pragma: no cover
        raise ValueError(f.kind)


def _valid_roots(topo: Topology, kind: str) -> list[str]:
    if kind == "link_flap":
        return [n.name for n in topo if n.is_network]
    if kind in ("memory_leak", "latency_degradation"):
        return [n.name for n in topo if not n.is_network]
    return [n.name for n in topo]


def schedule_faults(rng, topo: Topology, minutes: int, warmup: int, n_faults: int | None = None) -> list[Fault]:
    """Place non-overlapping faults after the warm-up window, with quiet gaps between them."""
    faults: list[Fault] = []
    t = warmup + int(rng.integers(30, 90))
    while t < minutes - 60:
        if n_faults is not None and len(faults) >= n_faults:
            break
        kind = str(rng.choice(FAULT_KINDS))
        root = str(rng.choice(_valid_roots(topo, kind)))
        duration = int(rng.integers(15, 46))
        intensity = round(float(rng.uniform(0.3, 1.0)), 2)
        faults.append(Fault(f"F{len(faults) + 1:03d}", kind, root, t, duration, intensity))
        t += duration + int(rng.integers(60, 150))
    return faults


def simulate(
    topo: Topology | None = None,
    minutes: int = 1440,
    warmup: int = 180,
    seed: int = 42,
    n_faults: int | None = None,
    log_rate: float = 0.4,
    faults: list[Fault] | None = None,
) -> SimulationResult:
    topo = topo or Topology.default()
    rng = np.random.default_rng(seed)

    data: dict[tuple[str, str], np.ndarray] = {}
    for node in topo:
        for metric in node.metrics:
            data[(node.name, metric)] = _baseline(rng, node, metric, minutes)

    logs: list[LogLine] = []
    for node in topo:
        counts = rng.poisson(log_rate, minutes)
        for t in np.nonzero(counts)[0]:
            for _ in range(counts[t]):
                logs.append(LogLine(int(t), node.name, _normal_log(rng, node)))

    if faults is None:
        faults = schedule_faults(rng, topo, minutes, warmup, n_faults)
    for f in faults:
        _apply_fault(rng, topo, data, logs, f, minutes)

    for (node, metric), arr in data.items():
        if metric in ("cpu_pct", "mem_pct"):
            np.clip(arr, 0, 100, out=arr)
        else:
            np.clip(arr, 0, None, out=arr)

    columns = pd.MultiIndex.from_tuples(list(data.keys()), names=["node", "metric"])
    df = pd.DataFrame(np.column_stack(list(data.values())), columns=columns)
    df.index.name = "minute"
    logs.sort(key=lambda x: (x.t, x.node))
    return SimulationResult(df, logs, faults, minutes, warmup, seed)
