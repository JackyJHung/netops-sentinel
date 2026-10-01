"""End-to-end AIOps pipeline: telemetry -> alerts -> incidents -> RCA -> runbook."""

from __future__ import annotations

from dataclasses import dataclass, field

from .correlation import Incident, Silence, correlate, detect_silences
from .detection import Alert, detect_metric_anomalies
from .logs import TemplateMiner, detect_log_anomalies, parse_logs
from .paging import apply_change_hold
from .rca import attach_changes, rank_root_causes
from .runbooks import load_runbooks, match_runbook, summarize
from .simulator import SimulationResult
from .topology import Topology


# Milestone 2 correlation and RCA: one group per connected component, alerting nodes only, one root.
SINGLE_ROOT_CORRELATION = {
    "split_incidents": False, "silence_evidence": False, "multi_root": False, "change_evidence": False, "change_hold": 0,
}


@dataclass
class PipelineConfig:
    detectors: tuple[str, ...] = ("robust_z", "ewma", "iforest", "forecast")
    use_logs: bool = True
    log_false_bursts_per_day: float | None = 0.01  # burst false-alarm budget per (node, template); None = fixed floor only
    correlation_window: int = 10
    max_hops: int = 2
    min_alert_len: int = 3
    split_incidents: bool = True  # split groups whose origins are on unrelated branches with separate onsets
    split_gap: int = 1  # origins starting within this many minutes stay together (tuned on the tune split)
    silence_evidence: bool = True  # nodes that stop reporting become RCA candidates and splitting origins
    silence_min: int = 5  # minutes of every metric missing before a node counts as silent
    multi_root: bool = True  # declare more than one root per incident when the evidence says so
    change_evidence: bool = True  # a deploy/config push on a node shortly before it alerts is an RCA signal
    change_lookback: int = 15  # minutes before a node's first alert that a change still counts
    change_weight: float = 0.25  # RCA score weight of "changed shortly before alerting"
    change_hold: int = 8  # minutes to hold a page after a recorded change; drop it if it clears (0 = off; tuned on tune)
    hold_per_root: bool = True  # decide the hold per declared root cause, not for the whole incident

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
        if "parsed_logs" not in cache:
            cache["parsed_logs"] = parse_logs(sim.logs)
        parsed, miner = cache["parsed_logs"]
        key = ("log_alerts", config.log_false_bursts_per_day)
        if key not in cache:
            cache[key] = detect_log_anomalies(
                parsed, miner, sim.minutes, sim.warmup, false_bursts_per_day=config.log_false_bursts_per_day
            )
        log_alerts = cache[key]
        alerts += log_alerts

    silences = detect_silences(sim.metrics, config.silence_min) if config.silence_evidence else []
    incidents = correlate(
        alerts, topo, config.correlation_window, config.max_hops,
        split=config.split_incidents, split_gap=config.split_gap, silences=silences,
    )
    if config.change_evidence:
        attach_changes(incidents, sim.changes, config.change_lookback)
    # paging needs the declared roots, so it runs after RCA (below)
    runbooks = load_runbooks()
    fmt = lambda t: sim.timestamp(t).strftime("%H:%M")  # noqa: E731
    for inc in incidents:
        inc.root_causes = rank_root_causes(inc, topo, weights={"change": config.change_weight}, multi_root=config.multi_root)
        inc.runbooks = {node: match_runbook(inc, runbooks, miner, node) for node in inc.roots}
        inc.runbook = inc.runbooks.get(inc.root_cause) or match_runbook(inc, runbooks, miner)
    apply_change_hold(incidents, sim.changes, config.change_lookback, config.change_hold, config.hold_per_root, topo)
    for inc in incidents:
        inc.summary = summarize(inc, fmt)
    return PipelineResult(alerts, incidents, miner, config, silences)
