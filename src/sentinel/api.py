"""FastAPI service exposing incidents, RCA, and telemetry, plus a small dashboard."""

from __future__ import annotations

from pathlib import Path
from threading import Lock

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from .evaluation import evaluate, score_day
from .pipeline import PipelineConfig, PipelineResult, run_pipeline
from .simulator import SCENARIOS, SimulationResult, simulate
from .topology import Topology

app = FastAPI(title="NetOps Sentinel", version="0.1.0", description="AIOps anomaly detection, alert correlation, and root-cause analysis")

_topo = Topology.default()
_lock = Lock()
_state: dict[str, SimulationResult | PipelineResult | None] = {"sim": None, "result": None}


class SimulateRequest(BaseModel):
    seed: int = 42
    minutes: int = Field(1440, ge=300, le=10080)
    config: str = Field("sentinel", pattern="^(sentinel|static)$")
    scenario: str = Field("clean", pattern="^(" + "|".join(SCENARIOS) + ")$")


def _run(req: SimulateRequest) -> None:
    sim = simulate(_topo, minutes=req.minutes, seed=req.seed, **SCENARIOS[req.scenario])
    cfg = PipelineConfig.baseline() if req.config == "static" else PipelineConfig()
    _state["sim"], _state["result"] = sim, run_pipeline(sim, _topo, cfg)


def _current() -> tuple[SimulationResult, PipelineResult]:
    with _lock:
        if _state["result"] is None:
            _run(SimulateRequest())
        return _state["sim"], _state["result"]  # type: ignore[return-value]


def _with_times(sim: SimulationResult, d: dict) -> dict:
    d["start_time"] = sim.timestamp(d["start"]).isoformat()
    d["end_time"] = sim.timestamp(d["end"]).isoformat()
    return d


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/simulate")
def simulate_endpoint(req: SimulateRequest) -> dict:
    with _lock:
        _run(req)
    sim, res = _state["sim"], _state["result"]
    return {"scenario": req.scenario, "faults": len(sim.faults), "alerts": len(res.alerts), "incidents": len(res.incidents)}


@app.get("/topology")
def topology() -> dict:
    return _topo.to_dict()


@app.get("/incidents")
def incidents() -> list[dict]:
    sim, res = _current()
    return [_with_times(sim, i.to_dict(include_alerts=False)) for i in res.incidents]


@app.get("/incidents/{incident_id}")
def incident(incident_id: str) -> dict:
    sim, res = _current()
    inc = res.incident(incident_id)
    if inc is None:
        raise HTTPException(404, f"no incident {incident_id}")
    return _with_times(sim, inc.to_dict())


@app.get("/faults")
def faults() -> list[dict]:
    sim, _ = _current()
    return [_with_times(sim, f.to_dict()) for f in sim.faults]


@app.get("/ground-truth")
def ground_truth() -> dict:
    """Injected faults plus the things that are *not* faults (benign events, telemetry blackouts), and the
    change log with its hidden ground truth (`caused`: the fault or benign event a change led to)."""
    sim, _ = _current()
    return {
        "faults": [_with_times(sim, f.to_dict()) for f in sim.faults],
        "benign": [_with_times(sim, b.to_dict()) for b in sim.benign],
        "blackouts": [_with_times(sim, b.to_dict()) for b in sim.blackouts],
        "changes": [{**c.to_dict(), "time": sim.timestamp(c.t).isoformat()} for c in sim.changes],
    }


@app.get("/evaluation")
def evaluation() -> dict:
    sim, res = _current()
    day = score_day(sim, res, _topo)
    return {**evaluate(sim, res, _topo).to_dict(), "benign_events": day.n_benign, "benign_paged": day.n_benign_paged}


@app.get("/series/{node}/{metric}")
def series(node: str, metric: str) -> dict:
    sim, _ = _current()
    if (node, metric) not in sim.metrics.columns:
        raise HTTPException(404, f"no series {node}/{metric}")
    return {
        "node": node,
        "metric": metric,
        "t": [sim.timestamp(t).strftime("%H:%M") for t in sim.metrics.index],
        "values": [round(float(v), 3) if v == v else None for v in sim.series(node, metric)],  # NaN = missing
    }


@app.get("/", response_class=HTMLResponse)
def dashboard() -> str:
    return (Path(__file__).parent / "static" / "index.html").read_text()
