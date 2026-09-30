"""Score a pipeline run against the simulator's ground-truth faults.

Metrics reported (the ones an AIOps team actually tracks):
  precision / recall / F1   - incident-level, vs injected faults
  MTTD                      - mean minutes from fault start to first matching incident
  RCA top-1 / top-3         - is the true root node the top (or a top-3) candidate?
  classification accuracy   - did the matched runbook's fault_kind equal the true kind?
  alert compression         - raw alerts per incident (how much noise was removed)
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np

from .correlation import Incident
from .pipeline import PipelineConfig, PipelineResult, run_pipeline
from .simulator import Fault, SimulationResult, simulate
from .topology import Topology

PRE_SLACK = 5    # minutes before fault start an incident may begin and still count
POST_SLACK = 15  # minutes after fault end


@dataclass
class EvalReport:
    n_faults: int
    n_incidents: int
    n_alerts: int
    precision: float
    recall: float
    f1: float
    mttd_min: float
    rca_top1: float
    rca_top3: float
    classification_acc: float
    alert_compression: float

    def to_dict(self) -> dict:
        return {k: (round(v, 3) if isinstance(v, float) else v) for k, v in asdict(self).items()}


def _matches(inc: Incident, f: Fault, topo: Topology) -> bool:
    overlaps = inc.start <= f.end + POST_SLACK and inc.end >= f.start - PRE_SLACK
    blast = {f.root, *topo.downstream(f.root)}
    return overlaps and any(n in blast for n in inc.nodes)


def evaluate(sim: SimulationResult, result: PipelineResult, topo: Topology | None = None) -> EvalReport:
    topo = topo or Topology.default()
    incidents, faults = result.incidents, sim.faults

    matched_incidents: set[str] = set()
    detected, ttd, top1, top3, cls = 0, [], 0, 0, 0
    for f in faults:
        hits = [i for i in incidents if _matches(i, f, topo)]
        matched_incidents.update(i.incident_id for i in hits)
        if not hits:
            continue
        detected += 1
        ttd.append(max(0, min(i.start for i in hits) - f.start))
        # judge RCA on the biggest matching incident (the one on-call would work)
        main = max(hits, key=lambda i: len(i.alerts))
        cands = [c["node"] for c in main.root_causes]
        top1 += bool(cands) and cands[0] == f.root
        top3 += f.root in cands[:3]
        cls += (main.runbook or {}).get("fault_kind") == f.kind

    n_inc, n_f = len(incidents), len(faults)
    precision = len(matched_incidents) / n_inc if n_inc else 0.0
    recall = detected / n_f if n_f else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return EvalReport(
        n_faults=n_f,
        n_incidents=n_inc,
        n_alerts=len(result.alerts),
        precision=precision,
        recall=recall,
        f1=f1,
        mttd_min=float(np.mean(ttd)) if ttd else float("nan"),
        rca_top1=top1 / detected if detected else 0.0,
        rca_top3=top3 / detected if detected else 0.0,
        classification_acc=cls / detected if detected else 0.0,
        alert_compression=len(result.alerts) / n_inc if n_inc else 0.0,
    )


def benchmark(seeds=range(5), minutes: int = 1440, configs: dict[str, PipelineConfig] | None = None) -> dict[str, dict]:
    """Average each config's metrics across several simulated days."""
    topo = Topology.default()
    configs = configs or {"static_baseline": PipelineConfig.baseline(), "sentinel": PipelineConfig()}
    out: dict[str, dict] = {}
    for name, cfg in configs.items():
        reports = []
        for seed in seeds:
            sim = simulate(topo, minutes=minutes, seed=seed)
            reports.append(evaluate(sim, run_pipeline(sim, topo, cfg), topo).to_dict())
        keys = reports[0].keys()
        out[name] = {k: round(float(np.nanmean([r[k] for r in reports])), 3) for k in keys}
    return out


def to_markdown(results: dict[str, dict]) -> str:
    cols = ["precision", "recall", "f1", "mttd_min", "rca_top1", "rca_top3", "classification_acc", "alert_compression", "n_alerts", "n_incidents"]
    lines = ["| config | " + " | ".join(cols) + " |", "|---|" + "---|" * len(cols)]
    for name, r in results.items():
        lines.append(f"| {name} | " + " | ".join(str(r[c]) for c in cols) + " |")
    return "\n".join(lines)
