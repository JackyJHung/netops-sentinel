"""Synthetic telemetry generator with labeled fault injection.

Produces one-minute metrics and syslog/app log lines for every node in a
topology, then injects faults whose effects cascade to downstream nodes with
a small lag. Every fault is recorded as ground truth so the detection pipeline
can be scored (precision, recall, time-to-detect, root-cause accuracy).

Optional realism (all off by default, see SCENARIOS):
  * concurrent faults    - a second fault overlapping an existing one, on the
                           same branch of the topology or an unrelated one
  * missing telemetry    - short NaN gaps per series, random whole-node
                           blackouts, and network devices that become
                           unreachable during a hard fault
  * benign events        - config pushes and rolling restarts that cause a
                           short, real spike but are not faults
  * change events        - a change log (deploys, config pushes) the pipeline can
                           read: some changes cause the fault that follows them,
                           most benign events are recorded as changes, and the
                           rest are harmless

Each option draws from its own RNG stream derived from the seed, so turning
one on does not reshuffle the others, and the default output is unchanged.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from .topology import Topology

FAULT_KINDS = ("cpu_saturation", "link_flap", "memory_leak", "latency_degradation")
BENIGN_KINDS = ("config_push", "maintenance")
DEFAULT_START = datetime(2026, 9, 1, 0, 0, 0)

SILENCE_MIN_INTENSITY = 0.6  # only hard faults knock a device off the monitoring network
BENIGN_CLEARANCE = 30  # minutes between a benign event and any fault, so labels are unambiguous

# Independent RNG streams for the optional scenario features.
_STREAM_OVERLAP, _STREAM_BENIGN, _STREAM_MISSING, _STREAM_CHANGES = 1, 2, 3, 4
BENIGN_RECORDED = 0.8  # share of benign events (planned changes) that make it into the change log

SCENARIOS: dict[str, dict] = {
    "clean": {},
    "hard": {
        "overlap_prob": 0.5,  # about half of the faults get a concurrent partner
        "gap_rate": 3.0,  # short NaN gaps per series per day
        "blackout_rate": 1.0,  # random whole-node blackouts per day (not faults)
        "fault_silence_prob": 0.5,  # chance a hard network fault makes the device unreachable
        "benign_rate": 3.0,  # benign config pushes / restarts per day
        "change_rate": 4.0,  # harmless changes per day, at random nodes and times
        "change_fault_prob": 0.4,  # chance a fault was caused by a change on its root 1-10 min earlier
    },
}


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
class BenignEvent:
    """A real but harmless disturbance (planned change). Alerting on it is a false positive."""

    event_id: str
    kind: str  # config_push | maintenance
    node: str
    start: int
    duration: int

    @property
    def end(self) -> int:
        return self.start + self.duration

    def to_dict(self) -> dict:
        return {**asdict(self), "end": self.end}


@dataclass
class ChangeEvent:
    """An entry in the change log: what the pipeline sees is kind, node, and time."""

    change_id: str
    kind: str  # deploy | config_push
    node: str
    t: int  # minute index
    caused: str | None = None  # ground truth only (fault_id or benign event_id); the pipeline never reads it

    def to_dict(self, truth: bool = True) -> dict:
        d = asdict(self)
        if not truth:
            d.pop("caused")
        return d


@dataclass
class Blackout:
    """A window where a node sent no telemetry at all (metrics NaN, no logs)."""

    node: str
    start: int
    end: int
    cause: str | None = None  # fault_id if the fault made the node unreachable, None if random

    def to_dict(self) -> dict:
        return asdict(self)


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
    benign: list[BenignEvent] = field(default_factory=list)
    blackouts: list[Blackout] = field(default_factory=list)
    changes: list[ChangeEvent] = field(default_factory=list)

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


def add_concurrent_faults(rng, topo: Topology, faults: list[Fault], minutes: int, overlap_prob: float) -> list[Fault]:
    """Give each fault, with probability `overlap_prob`, a partner that overlaps it in time.

    The partner starts between 10 min before and 20 min after its primary, on a
    different node that is either on the same branch (a dependency path exists)
    or on an unrelated branch, 50/50. It ends before the next primary begins, so
    at most two faults overlap. Partner ids are the primary's id plus "b".
    """
    extra: list[Fault] = []
    ordered = sorted(faults, key=lambda f: f.start)
    for i, f in enumerate(ordered):
        if rng.random() >= overlap_prob:
            continue
        related = topo.upstream(f.root) | set(topo.downstream(f.root))
        want_related = bool(rng.random() < 0.5)
        kinds = [str(k) for k in rng.permutation(FAULT_KINDS)]
        pick = None
        for same_branch in (want_related, not want_related):
            for kind in kinds:
                roots = [r for r in _valid_roots(topo, kind) if r != f.root and (r in related) == same_branch]
                if roots:
                    pick = (kind, str(rng.choice(roots)))
                    break
            if pick:
                break
        if pick is None:
            continue
        start = f.start + int(rng.integers(-10, min(20, f.duration - 5) + 1))  # always overlaps by >= 5 min
        horizon = ordered[i + 1].start - 15 if i + 1 < len(ordered) else minutes - 30
        duration = min(int(rng.integers(15, 46)), horizon - start)
        intensity = round(float(rng.uniform(0.3, 1.0)), 2)
        if duration >= 10 and start + duration >= f.start + 5:
            extra.append(Fault(f"{f.fault_id}b", pick[0], pick[1], start, duration, intensity))
    return extra


def schedule_benign(rng, topo: Topology, minutes: int, warmup: int, faults: list[Fault], rate: float) -> list[BenignEvent]:
    """Place Poisson(rate) benign events at least BENIGN_CLEARANCE minutes away from every fault."""
    busy = [(f.start - BENIGN_CLEARANCE, f.end + BENIGN_CLEARANCE) for f in faults]
    events: list[BenignEvent] = []
    for _ in range(int(rng.poisson(rate))):
        kind = str(rng.choice(BENIGN_KINDS))
        nodes = [n.name for n in topo if n.is_network == (kind == "config_push")]
        node, duration = str(rng.choice(nodes)), int(rng.integers(3, 9))
        for _attempt in range(50):
            start = int(rng.integers(warmup, minutes - 30))
            if all(start + duration <= lo or start >= hi for lo, hi in busy):
                busy.append((start - BENIGN_CLEARANCE, start + duration + BENIGN_CLEARANCE))
                events.append(BenignEvent("", kind, node, start, duration))
                break
    events.sort(key=lambda b: b.start)
    for k, b in enumerate(events):
        b.event_id = f"B{k + 1:03d}"
    return events


def _apply_benign(rng, data: dict, logs: list[LogLine], b: BenignEvent, minutes: int) -> None:
    s, e = b.start, min(b.end, minutes)
    if b.kind == "config_push":  # config commit pegs the control plane for a few minutes
        data[(b.node, "cpu_pct")][s:e] += rng.uniform(35, 50)
        data[(b.node, "latency_ms")][s:e] *= rng.uniform(1.3, 1.8)
        for t in range(s, e, 2):
            logs.append(LogLine(t, b.node, f"%SYS-5-CONFIG_I: Configured from console by netops on vty0 ({_ip(rng)})"))
    else:  # rolling restart of a service: brief errors and slow requests while instances cycle
        data[(b.node, "error_rate_pct")][s:e] += rng.uniform(3, 6)
        data[(b.node, "latency_ms")][s:e] *= rng.uniform(1.8, 2.5)
        data[(b.node, "cpu_pct")][s:e] += rng.uniform(15, 25)
        logs.append(LogLine(s, b.node, "INFO rolling restart started by deploy-bot"))
        logs.append(LogLine(e - 1, b.node, "INFO rolling restart finished, all instances healthy"))


def _change_kind(topo: Topology, node: str) -> str:
    return "config_push" if topo.nodes[node].is_network else "deploy"


def schedule_changes(
    rng, topo: Topology, minutes: int, warmup: int, faults: list[Fault], benign: list[BenignEvent],
    change_rate: float, change_fault_prob: float,
) -> list[ChangeEvent]:
    """Build the change log: causal changes on fault roots, recorded benign changes, and harmless noise."""
    changes: list[ChangeEvent] = []
    for f in faults:
        if rng.random() < change_fault_prob:
            changes.append(ChangeEvent("", _change_kind(topo, f.root), f.root, f.start - int(rng.integers(1, 11)), f.fault_id))
    for b in benign:  # planned work is usually, but not always, in the change log; timestamps are approximate
        if rng.random() < BENIGN_RECORDED:
            changes.append(ChangeEvent("", _change_kind(topo, b.node), b.node, b.start + int(rng.integers(-2, 3)), b.event_id))
    for _ in range(int(rng.poisson(change_rate))):
        node = str(rng.choice(list(topo.nodes)))
        changes.append(ChangeEvent("", _change_kind(topo, node), node, int(rng.integers(warmup, minutes))))
    changes.sort(key=lambda c: (c.t, c.node))
    for k, c in enumerate(changes):
        c.change_id = f"C{k + 1:03d}"
    return changes


def _apply_missing(
    rng, topo: Topology, data: dict, faults: list[Fault], minutes: int, warmup: int,
    gap_rate: float, blackout_rate: float, fault_silence_prob: float,
) -> list[Blackout]:
    """Blank out telemetry: fault-induced and random node blackouts, then short per-series gaps."""
    blackouts: list[Blackout] = []
    if fault_silence_prob > 0:
        for f in faults:
            eligible = (
                topo.nodes[f.root].is_network
                and f.kind in ("link_flap", "cpu_saturation")
                and f.intensity >= SILENCE_MIN_INTENSITY
            )
            if eligible and rng.random() < fault_silence_prob:
                start = f.start + int(rng.integers(0, 4))
                blackouts.append(Blackout(f.root, start, min(f.end + int(rng.integers(0, 6)), minutes), f.fault_id))
    for _ in range(int(rng.poisson(blackout_rate))):
        node = str(rng.choice(list(topo.nodes)))
        start = int(rng.integers(warmup, minutes - 60))
        blackouts.append(Blackout(node, start, min(start + int(rng.integers(30, 91)), minutes)))
    for b in blackouts:
        for metric in topo.nodes[b.node].metrics:
            data[(b.node, metric)][b.start : b.end] = np.nan
    if gap_rate > 0:
        for arr in data.values():
            for _ in range(int(rng.poisson(gap_rate))):
                length = int(rng.integers(1, 11))
                start = int(rng.integers(0, minutes - length))
                arr[start : start + length] = np.nan
    return sorted(blackouts, key=lambda b: (b.start, b.node))


def simulate(
    topo: Topology | None = None,
    minutes: int = 1440,
    warmup: int = 180,
    seed: int = 42,
    n_faults: int | None = None,
    log_rate: float = 0.4,
    faults: list[Fault] | None = None,
    overlap_prob: float = 0.0,
    gap_rate: float = 0.0,
    blackout_rate: float = 0.0,
    fault_silence_prob: float = 0.0,
    benign_rate: float = 0.0,
    change_rate: float = 0.0,
    change_fault_prob: float = 0.0,
) -> SimulationResult:
    """Simulate one day. The scenario options default to off; `SCENARIOS["hard"]` turns them all on."""
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

    faults = schedule_faults(rng, topo, minutes, warmup, n_faults) if faults is None else list(faults)
    for f in faults:
        _apply_fault(rng, topo, data, logs, f, minutes)

    if overlap_prob > 0:
        orng = np.random.default_rng([seed, _STREAM_OVERLAP])
        extra = add_concurrent_faults(orng, topo, faults, minutes, overlap_prob)
        for f in extra:
            _apply_fault(orng, topo, data, logs, f, minutes)
        faults = sorted(faults + extra, key=lambda f: (f.start, f.fault_id))

    benign: list[BenignEvent] = []
    if benign_rate > 0:
        brng = np.random.default_rng([seed, _STREAM_BENIGN])
        benign = schedule_benign(brng, topo, minutes, warmup, faults, benign_rate)
        for b in benign:
            _apply_benign(brng, data, logs, b, minutes)

    for (node, metric), arr in data.items():
        if metric in ("cpu_pct", "mem_pct"):
            np.clip(arr, 0, 100, out=arr)
        else:
            np.clip(arr, 0, None, out=arr)

    blackouts: list[Blackout] = []
    if gap_rate > 0 or blackout_rate > 0 or fault_silence_prob > 0:
        mrng = np.random.default_rng([seed, _STREAM_MISSING])
        blackouts = _apply_missing(mrng, topo, data, faults, minutes, warmup, gap_rate, blackout_rate, fault_silence_prob)
        logs = [ln for ln in logs if not any(b.node == ln.node and b.start <= ln.t < b.end for b in blackouts)]

    changes: list[ChangeEvent] = []
    if change_rate > 0 or change_fault_prob > 0:
        crng = np.random.default_rng([seed, _STREAM_CHANGES])
        changes = schedule_changes(crng, topo, minutes, warmup, faults, benign, change_rate, change_fault_prob)

    columns = pd.MultiIndex.from_tuples(list(data.keys()), names=["node", "metric"])
    df = pd.DataFrame(np.column_stack(list(data.values())), columns=columns)
    df.index.name = "minute"
    logs.sort(key=lambda x: (x.t, x.node))
    return SimulationResult(df, logs, faults, minutes, warmup, seed, benign=benign, blackouts=blackouts, changes=changes)
