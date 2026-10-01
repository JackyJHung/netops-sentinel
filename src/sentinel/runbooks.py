"""Match an incident to a runbook and draft a human-readable summary."""

from __future__ import annotations

from pathlib import Path

import yaml

from .correlation import Incident
from .logs import TemplateMiner
from .rca import describe_change, onsets

DEFAULT_RUNBOOKS = Path(__file__).resolve().parent / "data" / "runbooks.yaml"


def load_runbooks(path: str | Path = DEFAULT_RUNBOOKS) -> list[dict]:
    return yaml.safe_load(Path(path).read_text())["runbooks"]


def _evidence(incident: Incident, node: str, miner: TemplateMiner | None) -> tuple[set[str], list[str]]:
    signals, texts = set(), []
    for a in incident.alerts:
        if a.node != node:
            continue
        if a.signal.startswith("log:"):
            tid = a.signal[4:]
            texts.append(miner.templates[tid].text if miner and tid in miner.templates else a.description)
        else:
            signals.add(a.signal)
    return signals, texts


def match_runbook(incident: Incident, runbooks: list[dict], miner: TemplateMiner | None = None, node: str | None = None) -> dict:
    """Score each runbook by metric-signal and log-keyword overlap on a root-cause node (default: the top one)."""
    generic = next(r for r in runbooks if r["id"] == "RB-GENERIC")
    node = node or incident.root_cause
    if not node:
        return {**generic, "match_score": 0}
    signals, texts = _evidence(incident, node, miner)
    blob = " ".join(texts).lower()

    best, best_score = generic, 0.0
    for rb in runbooks:
        if rb["id"] == "RB-GENERIC":
            continue
        score = sum(1.0 for s in rb["signals"] if s in signals)
        score += sum(2.0 for k in rb["keywords"] if k.lower() in blob)  # logs are more specific
        if score > best_score:
            best, best_score = rb, score
    return {**best, "match_score": best_score}


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
    roots = set(incident.roots)
    impacted = [n for n in incident.nodes if n not in roots]
    if impacted:
        parts.append(f"Impacted: {', '.join(impacted)}.")
    return " ".join(parts)
