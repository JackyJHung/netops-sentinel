"""End-to-end AIOps pipeline: telemetry -> alerts -> incidents -> RCA -> runbook."""

from __future__ import annotations

from dataclasses import dataclass, field

from .correlation import Incident, correlate
from .detection import Alert, detect_metric_anomalies
from .logs import TemplateMiner, detect_log_anomalies, parse_logs
from .rca import rank_root_causes
from .runbooks import load_runbooks, match_runbook, summarize
from .simulator import SimulationResult
from .topology import Topology


@dataclass
class PipelineConfig:
    detectors: tuple[str, ...] = ("robust_z", "ewma", "iforest", "forecast")
    use_logs: bool = True
    correlation_window: int = 10
    max_hops: int = 2
    min_alert_len: int = 3

    @classmethod
    def baseline(cls) -> "PipelineConfig":
        """Static thresholds, no log mining: roughly what a team has before AIOps."""
        return cls(detectors=("static",), use_logs=False)


@dataclass
class PipelineResult:
    alerts: list[Alert]
    incidents: list[Incident]
    miner: TemplateMiner | None = None
    config: PipelineConfig = field(default_factory=PipelineConfig)

    def incident(self, incident_id: str) -> Incident | None:
        return next((i for i in self.incidents if i.incident_id == incident_id), None)


def run_pipeline(
    sim: SimulationResult,
    topo: Topology | None = None,
    config: PipelineConfig | None = None,
    cache: dict | None = None,
) -> PipelineResult:
    """`cache` is an optional per-simulation dict for detector scores shared across configs."""
    topo = topo or Topology.default()
    config = config or PipelineConfig()

    alerts = detect_metric_anomalies(sim.metrics, config.detectors, sim.warmup, config.min_alert_len, cache)
    miner = None
    if config.use_logs:
        parsed, miner = parse_logs(sim.logs)
        alerts += detect_log_anomalies(parsed, miner, sim.minutes, sim.warmup)

    incidents = correlate(alerts, topo, config.correlation_window, config.max_hops)
    runbooks = load_runbooks()
    fmt = lambda t: sim.timestamp(t).strftime("%H:%M")  # noqa: E731
    for inc in incidents:
        inc.root_causes = rank_root_causes(inc, topo)
        inc.runbook = match_runbook(inc, runbooks, miner)
        inc.summary = summarize(inc, fmt)
    return PipelineResult(alerts, incidents, miner, config)
