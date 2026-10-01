"""Match an incident to a runbook and draft a human-readable summary."""

from __future__ import annotations

from pathlib import Path

import yaml

from .correlation import Incident
from .logs import TemplateMiner
from .rca import describe_change, onsets
from .topology import Topology

DEFAULT_RUNBOOKS = Path(__file__).resolve().parent / "data" / "runbooks.yaml"


def load_runbooks(path: str | Path = DEFAULT_RUNBOOKS) -> list[dict]:
    return yaml.safe_load(Path(path).read_text())["runbooks"]


def _evidence(incident: Incident, nodes: set[str], miner: TemplateMiner | None) -> tuple[set[str], str]:
    """Alerting metric signals and lowercased log-template text on `nodes`."""
    signals, texts = set(), []
    for a in incident.alerts:
        if a.node not in nodes:
            continue
        if a.signal.startswith("log:"):
            tid = a.signal[4:]
            texts.append(miner.templates[tid].text if miner and tid in miner.templates else a.description)
        else:
            signals.add(a.signal)
    return signals, " ".join(texts).lower()


def _applies(rb: dict, topo: Topology, node: str) -> bool:
    kind = rb.get("applies_to", "any")
    return kind == "any" or node not in topo.nodes or (kind == "network") == topo.nodes[node].is_network


def match_runbook(
    incident: Incident,
    runbooks: list[dict],
    miner: TemplateMiner | None = None,
    node: str | None = None,
    topo: Topology | None = None,
    signatures: bool = True,
) -> dict:
    """Score each runbook by metric-signal and log-keyword overlap on a root-cause node (default: the top one).

    Only runbooks for the node's device type are considered. If the node has no evidence of its own (it went
    silent), match the runbooks' `dependents` signatures against the alerts on the nodes that depend on it.
    `matched_on` says which: "root", "dependents", or "none" (generic)."""
    topo = topo or Topology.default()
    generic = next(r for r in runbooks if r["id"] == "RB-GENERIC")
    node = node or incident.root_cause
    if not node:
        return {**generic, "match_score": 0, "matched_on": "none"}
    candidates = [rb for rb in runbooks if rb["id"] != "RB-GENERIC" and (not signatures or _applies(rb, topo, node))]

    signals, blob = _evidence(incident, {node}, miner)
    best, best_score = generic, 0.0
    for rb in candidates:
        score = sum(1.0 for s in rb["signals"] if s in signals)
        score += sum(2.0 for k in rb["keywords"] if k.lower() in blob)  # logs are more specific
        if score > best_score:
            best, best_score = rb, score
    if best_score > 0 or not signatures:
        return {**best, "match_score": best_score, "matched_on": "root" if best_score > 0 else "none"}

    dependents = set(topo.downstream(node)) if node in topo.nodes else set()
    signals, blob = _evidence(incident, dependents, miner)
    for rb in candidates:
        sig = rb.get("dependents", {})
        score = sum(w for s, w in sig.get("signals", {}).items() if s in signals)
        score += sum(2.0 for k in sig.get("keywords", []) if k.lower() in blob)
        if score > best_score:
            best, best_score = rb, score
    return {**best, "match_score": best_score, "matched_on": "dependents" if best_score > 0 else "none"}


def summarize(incident: Incident, fmt_time=lambda t: f"t+{t}m") -> str:
    rc = incident.root_causes[0] if incident.root_causes else None
    rb = incident.runbook or {}
    parts = [
        f"{incident.severity.upper()} incident {incident.incident_id}: {len(incident.alerts)} alerts across "
        f"{len(incident.nodes)} node(s) from {fmt_time(incident.start)} to {fmt_time(incident.end)}."
    ]
    if rc:
        parts.append(f"Most likely root cause: {rc['node']} (score {rc['score']}; {rc['reason']}).")
    if rb:
        parts.append(f"Suggested runbook: {rb.get('id')} ({rb.get('title')}).")
    for c in incident.root_causes[1:]:
        if c.get("declared"):
            other = incident.runbooks.get(c["node"], {})
            parts.append(f"Concurrent root cause: {c['node']} (score {c['score']}; {c['reason']}); runbook {other.get('id', 'RB-GENERIC')}.")
    for s in incident.silences:
        parts.append(f"{s.node} stopped sending telemetry at {fmt_time(s.start)} for {s.end - s.start} min.")
    onset = onsets(incident)
    for c in incident.changes:
        what, when = describe_change(c, onset[c.node])
        parts.append(f"Recent change: {what} at {fmt_time(c.t)}, {when}.")
    if incident.held and incident.hold_change is not None:
        c = incident.hold_change
        kind = c.kind.replace("_", " ")
        if incident.suppressed:
            parts.append(f"Not paged: started right after a {kind} to {c.node} at {fmt_time(c.t)} and cleared during the hold.")
        elif incident.paged_at > incident.start:
            parts.append(f"Paged at {fmt_time(incident.paged_at)} after a hold: started right after a {kind} to {c.node} and did not clear.")
        else:
            parts.append(f"Part of this incident followed a {kind} to {c.node}; another root cause paged it immediately.")
    roots = set(incident.roots)
    impacted = [n for n in incident.nodes if n not in roots]
    if impacted:
        parts.append(f"Impacted: {', '.join(impacted)}.")
    return " ".join(parts)
