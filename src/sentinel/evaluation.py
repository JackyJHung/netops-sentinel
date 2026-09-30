"""Score pipeline runs against the simulator's ground-truth faults.

Metrics reported (the ones an AIOps team actually tracks):
  precision / recall / F1   - incident-level, vs injected faults
  MTTD                      - mean minutes from fault start to first matching incident
  RCA top-1 / top-3         - per fault: is its root the top (or a top-3) candidate of the incident that
                              carries its evidence? Roots of *other* faults in the same incident are
                              removed from the ranking first ("filtered rank"), so an incident that
                              ranks both of two concurrent roots first and second scores both as top-1.
  classification accuracy   - did the runbook chosen for the fault's root have the right fault_kind?
  wrong merge               - share of concurrent faults on unrelated branches (no dependency path)
                              that ended up in the same incident
  extra-root precision      - of the second and third roots an incident declares, the share that
                              really are roots of faults active at the time
  alert compression         - raw alerts per incident (how much noise was removed)
  benign paged              - share of benign events (config pushes, restarts) that
                              produced an incident; those incidents are false positives

Benchmark protocol (see docs/DESIGN.md):
  * Seeds are split. `tune` (0-9) is for development and threshold tuning;
    `test` (100-129) is held out and only used for the numbers we report.
  * Metrics are pooled over every day in the split (recall = all detected
    faults / all faults), not averaged per day.
  * 95% confidence intervals come from a day-level bootstrap: resample whole
    simulated days with replacement and recompute the pooled metric. The day
    is the independent unit; faults on the same day share telemetry.
  * Fault-level metrics are broken down by fault kind and intensity bucket.
  * Every split is run on two scenarios: `clean` (one fault at a time, perfect
    telemetry) and `hard` (concurrent faults, missing telemetry, benign events).
"""

from __future__ import annotations

import os
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass

import numpy as np

from .correlation import Incident
from .pipeline import SINGLE_ROOT_CORRELATION, PipelineConfig, PipelineResult, run_pipeline
from .simulator import FAULT_KINDS, SCENARIOS, BenignEvent, Fault, SimulationResult, simulate
from .topology import Topology

PRE_SLACK = 5    # minutes before fault start an incident may begin and still count
POST_SLACK = 15  # minutes after fault end

SPLITS: dict[str, tuple[int, ...]] = {"tune": tuple(range(0, 10)), "test": tuple(range(100, 130))}
SUBTLE_BELOW = 0.5  # intensity < 0.5 is a subtle ("gray") fault, >= 0.5 is hard
INTENSITY_BUCKETS = ("subtle", "hard")
N_BOOT = 2000
CI_LEVEL = 0.95

# Ablation ladder: each row adds one stage. The README results table is this ladder on the test split.
_OFF = SINGLE_ROOT_CORRELATION
ABLATION: dict[str, PipelineConfig] = {
    "static_baseline": PipelineConfig.baseline(),
    "robust_z+ewma": PipelineConfig(detectors=("robust_z", "ewma"), use_logs=False, **_OFF),
    "+iforest": PipelineConfig(detectors=("robust_z", "ewma", "iforest"), use_logs=False, **_OFF),
    "+forecast": PipelineConfig(detectors=("robust_z", "ewma", "iforest", "forecast"), use_logs=False, **_OFF),
    "+log mining": PipelineConfig(**_OFF),
    "+incident splitting": PipelineConfig(silence_evidence=False, multi_root=False),
    "+silent nodes": PipelineConfig(multi_root=False),
    "sentinel": PipelineConfig(),  # + multi-root RCA
}

OVERALL_METRICS = (
    "precision", "recall", "f1", "mttd_min", "rca_top1", "rca_top3",
    "classification_acc", "alert_compression", "n_alerts", "n_incidents", "benign_paged",
    "wrong_merge", "same_branch_merge", "extra_root_precision",
)
FAULT_METRICS = ("recall", "mttd_min", "rca_top1", "rca_top3", "classification_acc")


def intensity_bucket(intensity: float) -> str:
    return "subtle" if intensity < SUBTLE_BELOW else "hard"


# --------------------------------------------------------------------------
# Scoring one simulated day
# --------------------------------------------------------------------------
@dataclass
class FaultOutcome:
    fault_id: str
    kind: str
    root: str
    intensity: float
    detected: bool
    ttd: float | None  # minutes from fault start to first matching incident
    top1: bool
    top3: bool
    cls_ok: bool  # runbook fault_kind == true kind
    incident_id: str | None = None  # the incident this fault was judged on
    rank: int | None = None  # filtered rank of the root in that incident (1 = top)

    @property
    def bucket(self) -> str:
        return intensity_bucket(self.intensity)


@dataclass
class DayScore:
    seed: int
    n_alerts: int
    n_incidents: int
    n_matched_incidents: int
    faults: list[FaultOutcome]
    n_benign: int = 0
    n_benign_paged: int = 0  # benign events that produced an unmatched (false-positive) incident
    # concurrent fault pairs (both detected), and how many ended up in the same incident
    pairs_unrelated: int = 0
    merged_unrelated: int = 0
    pairs_same: int = 0
    merged_same: int = 0
    extra_roots: int = 0  # declared roots beyond the first, over all incidents
    extra_roots_correct: int = 0


@dataclass
class EvalReport:
    n_faults: int
    n_incidents: int
    n_alerts: int
    precision: float
    recall: float
    f1: float
    mttd_min: float | None
    rca_top1: float
    rca_top3: float
    classification_acc: float
    alert_compression: float

    def to_dict(self) -> dict:
        return {k: (round(v, 3) if isinstance(v, float) else v) for k, v in asdict(self).items()}


def _overlaps(inc: Incident, f: Fault) -> bool:
    return inc.start <= f.end + POST_SLACK and inc.end >= f.start - PRE_SLACK


def _matches(inc: Incident, f: Fault, topo: Topology) -> bool:
    """Precision: is this page explained by the fault (any node in its blast radius, in its time window)?"""
    blast = {f.root, *topo.downstream(f.root)}
    return _overlaps(inc, f) and any(n in blast for n in inc.nodes)


def _detects(inc: Incident, f: Fault, topo: Topology, silenced: set[str]) -> bool:
    """Recall: does this incident carry evidence of *this* fault? An alert on its root in the window, or,
    if the fault knocked the root off the network, any alert in its blast radius. Without this, a
    concurrent fault would get credit for its neighbour's incident through shared downstream nodes."""
    if not _overlaps(inc, f):
        return False
    if any(a.node == f.root and a.start <= f.end + POST_SLACK and a.end >= f.start - PRE_SLACK for a in inc.alerts):
        return True
    return f.fault_id in silenced and _matches(inc, f, topo)


def _hits_benign(inc: Incident, b: BenignEvent) -> bool:
    return b.node in inc.nodes and inc.start <= b.end + POST_SLACK and inc.end >= b.start - PRE_SLACK


def score_day(sim: SimulationResult, result: PipelineResult, topo: Topology | None = None) -> DayScore:
    topo = topo or Topology.default()
    incidents = result.incidents
    silenced = {b.cause for b in sim.blackouts if b.cause}
    matched_incidents = {i.incident_id for i in incidents if any(_matches(i, f, topo) for f in sim.faults)}
    outcomes: list[FaultOutcome] = []
    home: dict[str, str] = {}  # fault_id -> incident judged for it
    for f in sim.faults:
        hits = [i for i in incidents if _detects(i, f, topo, silenced)]
        if not hits:
            outcomes.append(FaultOutcome(f.fault_id, f.kind, f.root, f.intensity, False, None, False, False, False))
            continue
        # Time to the first page consistent with this fault: an alert in its blast radius that is active in
        # its window. Not the incident start, which may belong to a concurrent fault that paged earlier.
        blast = {f.root, *topo.downstream(f.root)}
        first = min(max(a.start, f.start) for i in hits for a in i.alerts if a.node in blast and a.end >= f.start - PRE_SLACK)
        ttd = float(first - f.start)
        # Judge RCA on the incident carrying the most evidence of this fault (the one on-call would work
        # for it): root evidence first, then alerts in its blast radius during its window.
        def evidence(i: Incident, f=f, blast=blast) -> tuple:
            on_root = any(a.node == f.root for a in i.alerts) or any(s.node == f.root for s in getattr(i, "silences", []))
            in_blast = sum(a.node in blast and a.start <= f.end + POST_SLACK and a.end >= f.start - PRE_SLACK for a in i.alerts)
            return on_root, in_blast, len(i.alerts)

        main = max(hits, key=evidence)
        home[f.fault_id] = main.incident_id
        other_roots = {g.root for g in sim.faults if g.root != f.root and _detects(main, g, topo, silenced)}
        cands = [c["node"] for c in main.root_causes if c["node"] not in other_roots]
        rank = cands.index(f.root) + 1 if f.root in cands else None
        runbook = (getattr(main, "runbooks", None) or {}).get(f.root) or main.runbook or {}
        outcomes.append(
            FaultOutcome(
                f.fault_id, f.kind, f.root, f.intensity, True, ttd,
                top1=rank == 1,
                top3=rank is not None and rank <= 3,
                cls_ok=runbook.get("fault_kind") == f.kind,
                incident_id=main.incident_id,
                rank=rank,
            )
        )

    pairs = {"unrelated": [0, 0], "same": [0, 0]}
    for i, f in enumerate(sim.faults):
        for g in sim.faults[i + 1 :]:
            if f.start < g.end and g.start < f.end and f.fault_id in home and g.fault_id in home:
                related = f.root in topo.upstream(g.root) or g.root in topo.upstream(f.root)
                pair = pairs["same" if related else "unrelated"]
                pair[0] += 1
                pair[1] += home[f.fault_id] == home[g.fault_id]

    extra = correct = 0
    for inc in incidents:
        declared = [c["node"] for c in inc.root_causes if c.get("declared")][1:]
        active = {f.root for f in sim.faults if _overlaps(inc, f)}
        extra += len(declared)
        correct += sum(n in active for n in declared)

    false_pos = [i for i in incidents if i.incident_id not in matched_incidents]
    paged = sum(any(_hits_benign(i, b) for i in false_pos) for b in sim.benign)
    return DayScore(
        sim.seed, len(result.alerts), len(incidents), len(matched_incidents), outcomes, len(sim.benign), paged,
        pairs_unrelated=pairs["unrelated"][0], merged_unrelated=pairs["unrelated"][1],
        pairs_same=pairs["same"][0], merged_same=pairs["same"][1],
        extra_roots=extra, extra_roots_correct=correct,
    )


# --------------------------------------------------------------------------
# Pooled metrics
# --------------------------------------------------------------------------
def _ratio(a, b):
    """a / b with NaN where b == 0. Works on scalars and bootstrap arrays."""
    a, b = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(b > 0, a / np.where(b > 0, b, 1.0), np.nan)


def _day_counts(day: DayScore, keep=None) -> dict[str, float]:
    faults = [f for f in day.faults if keep is None or keep(f)]
    det = [f for f in faults if f.detected]
    return {
        "days": 1.0,
        "faults": len(faults),
        "detected": len(det),
        "ttd_sum": sum(f.ttd for f in det),
        "top1": sum(f.top1 for f in det),
        "top3": sum(f.top3 for f in det),
        "cls": sum(f.cls_ok for f in det),
        "incidents": day.n_incidents,
        "matched": day.n_matched_incidents,
        "alerts": day.n_alerts,
        "benign": day.n_benign,
        "benign_paged": day.n_benign_paged,
        "pairs_unrelated": day.pairs_unrelated,
        "merged_unrelated": day.merged_unrelated,
        "pairs_same": day.pairs_same,
        "merged_same": day.merged_same,
        "extra_roots": day.extra_roots,
        "extra_roots_correct": day.extra_roots_correct,
    }


_COUNT_KEYS = ("faults", "detected", "incidents", "matched", "alerts", "benign", "pairs_unrelated", "pairs_same", "extra_roots")


def _metrics(c: dict) -> dict:
    """Pooled metrics from summed counts. NaN marks an undefined metric (e.g. recall with no faults)."""
    p = _ratio(c["matched"], c["incidents"])
    r = _ratio(c["detected"], c["faults"])
    return {
        "precision": p,
        "recall": r,
        "f1": _ratio(2 * p * r, p + r),
        "mttd_min": _ratio(c["ttd_sum"], c["detected"]),
        "rca_top1": _ratio(c["top1"], c["detected"]),
        "rca_top3": _ratio(c["top3"], c["detected"]),
        "classification_acc": _ratio(c["cls"], c["detected"]),
        "alert_compression": _ratio(c["alerts"], c["incidents"]),
        "n_alerts": _ratio(c["alerts"], c["days"]),
        "n_incidents": _ratio(c["incidents"], c["days"]),
        "benign_paged": _ratio(c["benign_paged"], c["benign"]),
        "wrong_merge": _ratio(c["merged_unrelated"], c["pairs_unrelated"]),
        "same_branch_merge": _ratio(c["merged_same"], c["pairs_same"]),
        "extra_root_precision": _ratio(c["extra_roots_correct"], c["extra_roots"]),
    }


def _num(x) -> float | None:
    x = float(x)
    return round(x, 3) if np.isfinite(x) else None


def evaluate(sim: SimulationResult, result: PipelineResult, topo: Topology | None = None) -> EvalReport:
    """Score a single simulated day (used by `sentinel run`, the API, and tests)."""
    day = score_day(sim, result, topo)
    m = {k: float(v) for k, v in _metrics(_day_counts(day)).items()}

    def fill(v: float, empty: float) -> float:
        return v if np.isfinite(v) else empty

    return EvalReport(
        n_faults=len(day.faults),
        n_incidents=day.n_incidents,
        n_alerts=day.n_alerts,
        precision=fill(m["precision"], 0.0),
        recall=fill(m["recall"], 1.0),
        f1=fill(m["f1"], 0.0),
        mttd_min=m["mttd_min"] if np.isfinite(m["mttd_min"]) else None,
        rca_top1=fill(m["rca_top1"], 0.0),
        rca_top3=fill(m["rca_top3"], 0.0),
        classification_acc=fill(m["classification_acc"], 0.0),
        alert_compression=fill(m["alert_compression"], 0.0),
    )


# --------------------------------------------------------------------------
# Day-level bootstrap
# --------------------------------------------------------------------------
def _bootstrap_weights(n_days: int, n_boot: int = N_BOOT, seed: int = 0) -> np.ndarray:
    """(n_boot, n_days) resample counts: row b says how often each day appears in resample b."""
    rng = np.random.default_rng(seed)
    return rng.multinomial(n_days, np.full(n_days, 1.0 / n_days), size=n_boot)


def _with_ci(days: list[DayScore], weights: np.ndarray, metrics: tuple[str, ...], keep=None) -> dict[str, dict]:
    rows = [_day_counts(d, keep) for d in days]
    per_day = {k: np.array([r[k] for r in rows], dtype=float) for k in rows[0]}
    point = _metrics({k: v.sum() for k, v in per_day.items()})
    boot = _metrics({k: weights @ v for k, v in per_day.items()})
    tail = 100 * (1 - CI_LEVEL) / 2
    out = {}
    for name in metrics:
        b = boot[name][np.isfinite(boot[name])]
        lo, hi = np.percentile(b, [tail, 100 - tail]) if b.size else (np.nan, np.nan)
        out[name] = {"mean": _num(point[name]), "lo": _num(lo), "hi": _num(hi)}
    return out


def summarize_scenarios(
    days: dict[str, dict[str, list[DayScore]]], split: str | None = None, seeds=None, minutes: int | None = None
) -> dict:
    """`days[scenario][config]` -> one report covering every scenario."""
    per = {name: summarize(d) for name, d in days.items()}
    first = next(iter(per.values()))
    return {
        "split": split,
        "seeds": list(seeds) if seeds is not None else None,
        "n_days": first["n_days"],
        "minutes": minutes,
        "ci": first["ci"],
        "scenarios": {
            name: {"settings": SCENARIOS.get(name, {}), "faults_per_day": _faults_per_day(days[name]), "configs": r["configs"]}
            for name, r in per.items()
        },
    }


def _faults_per_day(days_by_config: dict[str, list[DayScore]]) -> float:
    days = next(iter(days_by_config.values()))
    return round(sum(len(d.faults) for d in days) / max(len(days), 1), 2)


def summarize(days_by_config: dict[str, list[DayScore]], split: str | None = None, seeds=None, minutes: int | None = None) -> dict:
    """Pooled metrics with 95% CIs, overall and per fault kind / intensity bucket, for each config."""
    n_days = len(next(iter(days_by_config.values())))
    weights = _bootstrap_weights(n_days)
    def fault_slice(days: list[DayScore], keep) -> dict:
        n = sum(keep(f) for d in days for f in d.faults)
        return {"n_faults": n, **_with_ci(days, weights, FAULT_METRICS, keep)}

    configs = {}
    for name, days in days_by_config.items():
        totals = {k: int(sum(_day_counts(d)[k] for d in days)) for k in _COUNT_KEYS}
        configs[name] = {
            "counts": totals,  # sample sizes behind the rates
            "overall": _with_ci(days, weights, OVERALL_METRICS),
            "by_kind": {k: fault_slice(days, lambda f, k=k: f.kind == k) for k in FAULT_KINDS},
            "by_intensity": {b: fault_slice(days, lambda f, b=b: f.bucket == b) for b in INTENSITY_BUCKETS},
        }
    return {
        "split": split,
        "seeds": list(seeds) if seeds is not None else None,
        "n_days": n_days,
        "minutes": minutes,
        "ci": f"{CI_LEVEL:.0%} CI, day-level bootstrap ({N_BOOT} resamples)",
        "configs": configs,
    }


# --------------------------------------------------------------------------
# Benchmark runner
# --------------------------------------------------------------------------
def _run_seed(task: tuple[int, int, dict[str, PipelineConfig], str]) -> dict[str, DayScore]:
    seed, minutes, configs, scenario = task
    topo = Topology.default()
    sim = simulate(topo, minutes=minutes, seed=seed, **SCENARIOS[scenario])
    cache: dict = {}  # detector scores shared by every config on this simulated day
    return {name: score_day(sim, run_pipeline(sim, topo, cfg, cache=cache), topo) for name, cfg in configs.items()}


def run_benchmark(
    seeds,
    minutes: int = 1440,
    configs: dict[str, PipelineConfig] | None = None,
    jobs: int | None = None,
    scenario: str = "clean",
) -> dict[str, list[DayScore]]:
    """Simulate each seed once under `scenario`, run every config on it, and return per-day scores per config."""
    configs = configs or ABLATION
    tasks = [(int(s), minutes, configs, scenario) for s in seeds]
    jobs = min(jobs or os.cpu_count() or 1, len(tasks))
    if jobs > 1:
        with ProcessPoolExecutor(max_workers=jobs) as pool:
            per_seed = list(pool.map(_run_seed, tasks))
    else:
        per_seed = [_run_seed(t) for t in tasks]
    return {name: [r[name] for r in per_seed] for name in configs}


# --------------------------------------------------------------------------
# Markdown
# --------------------------------------------------------------------------
_LABELS = {
    "precision": "precision", "recall": "recall", "f1": "F1", "mttd_min": "MTTD (min)",
    "rca_top1": "RCA top-1", "rca_top3": "RCA top-3", "classification_acc": "runbook match",
    "alert_compression": "alerts/incident", "n_alerts": "alerts/day", "n_incidents": "incidents/day",
    "benign_paged": "benign paged", "wrong_merge": "wrong merge", "same_branch_merge": "same-branch merge",
    "extra_root_precision": "extra-root precision",
}


def _cell(m: dict, digits: int = 2) -> str:
    if m["mean"] is None:
        return "n/a"
    ci = f" [{m['lo']:.{digits}f}, {m['hi']:.{digits}f}]" if m["lo"] is not None else ""
    return f"{m['mean']:.{digits}f}{ci}"


def _digits(metric: str) -> int:
    return 1 if metric in ("mttd_min", "alert_compression", "n_alerts", "n_incidents") else 2


def _table(rows: dict[str, dict], metrics: tuple[str, ...], first_col: str, with_n: bool = False) -> list[str]:
    head = [first_col] + (["faults"] if with_n else []) + [_LABELS[m] for m in metrics]
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for name, r in rows.items():
        cells = [name] + ([str(r["n_faults"])] if with_n else []) + [_cell(r[m], _digits(m)) for m in metrics]
        lines.append("| " + " | ".join(cells) + " |")
    return lines


def _config_sections(configs: dict, level: str) -> list[str]:
    lines = [f"{level} Overall", "", *_table({n: c["overall"] for n, c in configs.items()}, OVERALL_METRICS, "config")]
    for name in ("static_baseline", "sentinel"):
        if name not in configs:
            continue
        cfg = configs[name]
        lines += ["", f"{level} {name}: by fault kind", "", *_table(cfg["by_kind"], FAULT_METRICS, "fault kind", with_n=True)]
        lines += ["", f"{level} {name}: by intensity (subtle < {SUBTLE_BELOW}, hard >= {SUBTLE_BELOW})", ""]
        lines += _table(cfg["by_intensity"], FAULT_METRICS, "intensity", with_n=True)
    return lines


def to_markdown(report: dict) -> str:
    split = report.get("split") or "custom"
    seeds = report.get("seeds") or []
    seed_txt = f"seeds {seeds[0]}-{seeds[-1]}" if seeds else "custom seeds"
    held_out = " (held out)" if split == "test" else " (development only)" if split == "tune" else ""
    lines = [
        f"# Benchmark: {split} split{held_out}",
        "",
        f"{report['n_days']} simulated days ({seed_txt}), {report.get('minutes') or '?'} min each. "
        f"Values are pooled over all days; brackets are the {report['ci']}.",
        "",
    ]
    if "scenarios" not in report:
        return "\n".join(lines + _config_sections(report["configs"], "##")) + "\n"
    for name, sc in report["scenarios"].items():
        settings = ", ".join(f"{k}={v}" for k, v in sc["settings"].items()) or "one fault at a time, complete telemetry"
        lines += [f"## Scenario: {name}", "", f"Settings: {settings}. Faults per day: {sc['faults_per_day']}.", ""]
        lines += _config_sections(sc["configs"], "###") + [""]
    return "\n".join(lines).rstrip() + "\n"
