"""End-to-end AIOps pipeline: telemetry -> alerts -> incidents -> RCA -> runbook."""

from __future__ import annotations

from dataclasses import dataclass, field

from .correlation import Incident, Silence, correlate, detect_silences
from .detection import Alert, detect_metric_anomalies
from .logs import TemplateMiner, detect_log_anomalies, parse_logs
from .rca import rank_root_causes
from .runbooks import load_runbooks, match_runbook, summarize
from .simulator import SimulationResult
from .topology import Topology


# Milestone 2 correlation and RCA: one group per connected component, alerting nodes only, one root.
SINGLE_ROOT_CORRELATION = {"split_incidents": False, "silence_evidence": False, "multi_root": False}


@dataclass
class PipelineConfig:
    detectors: tuple[str, ...] = ("robust_z", "ewma", "iforest", "forecast")
    use_logs: bool = True
    correlation_window: int = 10
    max_hops: int = 2
    min_alert_len: int = 3
    split_incidents: bool = True  # split groups whose origins are on unrelated branches with separate onsets
    split_gap: int = 1  # origins starting within this many minutes stay together (tuned on the tune split)
    silence_evidence: bool = True  # nodes that stop reporting become RCA candidates and splitting origins
    silence_min: int = 5  # minutes of every metric missing before a node counts as silent
    multi_root: bool = True  # declare more than one root per incident when the evidence says so

    @classmethod
    def baseline(cls) -> PipelineConfig:
        """Static thresholds, no log mining, plain correlation: roughly what a team has before AIOps."""
        return cls(detectors=("static",), use_logs=False, **SINGLE_ROOT_CORRELATION)


@dataclass
class PipelineResult:
    alerts: list[Alert]
    incidents: list[Incident]
    miner: TemplateMiner | None = None
    config: PipelineConfig = field(default_factory=PipelineConfig)
    silences: list[Silence] = field(default_factory=list)

    def incident(self, incident_id: str) -> Incident | None:
        return next((i for i in self.incidents if i.incident_id == incident_id), None)


def run_pipeline(
    sim: SimulationResult,
    topo: Topology | None = None,
    config: PipelineConfig | None = None,
    cache: dict | None = None,
) -> PipelineResult:
    """`cache` is an optional per-simulation dict for detector outputs shared across configs."""
    topo = topo or Topology.default()
    config = config or PipelineConfig()
    cache = {} if cache is None else cache

    key = ("metric_alerts", config.detectors, config.min_alert_len)
    if key not in cache:
        cache[key] = detect_metric_anomalies(sim.metrics, config.detectors, sim.warmup, config.min_alert_len, cache)
    alerts = list(cache[key])
    miner = None
    if config.use_logs:
        if "logs" not in cache:
            parsed, log_miner = parse_logs(sim.logs)
            cache["logs"] = (log_miner, detect_log_anomalies(parsed, log_miner, sim.minutes, sim.warmup))
        miner, log_alerts = cache["logs"]
        alerts += log_alerts

    silences = detect_silences(sim.metrics, config.silence_min) if config.silence_evidence else []
    incidents = correlate(
        alerts, topo, config.correlation_window, config.max_hops,
        split=config.split_incidents, split_gap=config.split_gap, silences=silences,
    )
    runbooks = load_runbooks()
    fmt = lambda t: sim.timestamp(t).strftime("%H:%M")  # noqa: E731
    for inc in incidents:
        inc.root_causes = rank_root_causes(inc, topo, multi_root=config.multi_root)
        inc.runbooks = {node: match_runbook(inc, runbooks, miner, node) for node in inc.roots}
        inc.runbook = inc.runbooks.get(inc.root_cause) or match_runbook(inc, runbooks, miner)
        inc.summary = summarize(inc, fmt)
    return PipelineResult(alerts, incidents, miner, config, silences)
