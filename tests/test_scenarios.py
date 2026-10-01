import numpy as np
import pytest

from sentinel.detection import detect_metric_anomalies
from sentinel.evaluation import score_day
from sentinel.pipeline import run_pipeline
from sentinel.simulator import SCENARIOS, Fault, simulate
from sentinel.topology import Topology

TOPO = Topology.default()


def _overlaps(a, b) -> bool:
    return a.start < b.end and b.start < a.end


def _related(a: str, b: str) -> bool:
    return a in TOPO.upstream(b) or b in TOPO.upstream(a)


# ---------------------------------------------------------------- overlap scheduling
def test_default_is_unchanged_and_hard_keeps_primary_faults():
    clean = simulate(seed=5)
    assert clean.benign == [] and clean.blackouts == []
    assert not np.isnan(clean.metrics.to_numpy()).any()
    hard = simulate(seed=5, **SCENARIOS["hard"])
    primary = [f for f in hard.faults if not f.fault_id.endswith("b")]
    assert [f.to_dict() for f in primary] == [f.to_dict() for f in clean.faults]


def test_overlap_prob_adds_concurrent_faults_on_both_branch_types():
    pairs = {"same": 0, "unrelated": 0}
    for seed in range(4):
        sim = simulate(seed=seed, minutes=1440, overlap_prob=1.0)
        extra = [f for f in sim.faults if f.fault_id.endswith("b")]
        assert extra, "overlap_prob=1 should add concurrent faults"
        for f in extra:
            partner = next(p for p in sim.faults if p.fault_id == f.fault_id[:-1])
            assert _overlaps(f, partner) and f.root != partner.root
            assert partner.start - 10 <= f.start <= partner.start + 20
            pairs["same" if _related(f.root, partner.root) else "unrelated"] += 1
            others = [g for g in sim.faults if g not in (f, partner)]
            assert not any(_overlaps(f, g) for g in others), "at most two faults overlap"
    assert pairs["same"] and pairs["unrelated"]


def test_overlap_is_deterministic_per_seed():
    a, b = simulate(seed=9, overlap_prob=0.5), simulate(seed=9, overlap_prob=0.5)
    assert [f.to_dict() for f in a.faults] == [f.to_dict() for f in b.faults]
    assert np.allclose(a.metrics.to_numpy(), b.metrics.to_numpy(), equal_nan=True)


# ---------------------------------------------------------------- missing telemetry
def test_short_gaps_are_1_to_10_minutes():
    sim = simulate(seed=2, faults=[], gap_rate=3.0)
    x = sim.series("api-1", "latency_ms")
    assert np.isnan(x).any()
    runs = np.diff(np.flatnonzero(np.diff(np.r_[0, np.isnan(x).astype(int), 0])).reshape(-1, 2), axis=1).ravel()
    assert runs.max() <= 20  # two gaps may touch, but no long outage from gaps alone


def test_random_blackout_silences_metrics_and_logs():
    sim = simulate(seed=4, faults=[], blackout_rate=3.0)
    assert sim.blackouts and all(b.cause is None and b.end - b.start >= 30 for b in sim.blackouts)
    b = sim.blackouts[0]
    for m in TOPO.nodes[b.node].metrics:
        assert np.isnan(sim.series(b.node, m)[b.start : b.end]).all()
    assert not [ln for ln in sim.logs if ln.node == b.node and b.start <= ln.t < b.end]


def test_hard_fault_can_make_network_root_go_silent():
    f = Fault("F001", "link_flap", "dist-sw-1", start=400, duration=40, intensity=0.9)
    sim = simulate(seed=1, minutes=720, faults=[f], fault_silence_prob=1.0)
    (b,) = sim.blackouts
    assert b.node == "dist-sw-1" and b.cause == "F001" and b.start <= 404
    assert np.isnan(sim.series("dist-sw-1", "packet_loss_pct")[b.start : b.end]).all()
    # the cascade is still visible downstream
    assert np.nanmean(sim.series("access-sw-1", "packet_loss_pct")[405:440]) > 0.5


def test_detectors_do_not_fire_on_gaps_or_blackouts():
    sim = simulate(seed=6, faults=[], gap_rate=6.0, blackout_rate=3.0)
    alerts = detect_metric_anomalies(sim.metrics, warmup=sim.warmup)
    assert len(alerts) <= 2, [a.description for a in alerts]


# ---------------------------------------------------------------- benign events
def test_benign_events_are_real_spikes_away_from_faults():
    sim = simulate(seed=8, benign_rate=4.0)
    assert sim.benign
    for b in sim.benign:
        assert all(b.end + 30 <= f.start or b.start >= f.end + 30 for f in sim.faults)
        metric = "cpu_pct" if b.kind == "config_push" else "error_rate_pct"
        x = sim.series(b.node, metric)
        assert np.nanmean(x[b.start : b.end]) > np.nanmean(x[b.start - 60 : b.start - 5]) + 2


def test_alerting_on_benign_event_is_a_false_positive():
    sim = simulate(seed=8, faults=[], benign_rate=4.0)
    day = score_day(sim, run_pipeline(sim))
    assert day.n_benign == len(sim.benign)
    assert day.n_benign_paged >= 1
    assert day.n_matched_incidents == 0  # no faults, so nothing can be a true positive


@pytest.mark.parametrize("seed", [5])
def test_hard_scenario_runs_end_to_end(seed):
    sim = simulate(seed=seed, **SCENARIOS["hard"])
    assert np.isnan(sim.metrics.to_numpy()).any() and sim.benign
    res = run_pipeline(sim)
    day = score_day(sim, res)
    assert len(day.faults) == len(sim.faults)
    assert res.incidents


# ---------------------------------------------------------------- milestone 5: change events
def test_changes_cause_some_faults_and_some_are_harmless():
    sim = simulate(seed=4, **SCENARIOS["hard"])
    assert sim.changes
    faults = {f.fault_id: f for f in sim.faults}
    causal = [c for c in sim.changes if c.caused in faults]
    harmless = [c for c in sim.changes if c.caused is None]
    assert causal and harmless
    for c in causal:
        f = faults[c.caused]
        assert c.node == f.root and 1 <= f.start - c.t <= 10
        assert c.kind == ("config_push" if TOPO.nodes[c.node].is_network else "deploy")
    assert [c.t for c in sim.changes] == sorted(c.t for c in sim.changes)


def test_most_benign_events_have_a_change_record():
    recorded = total = 0
    for seed in range(6):
        sim = simulate(seed=seed, **SCENARIOS["hard"])
        for b in sim.benign:
            total += 1
            recorded += any(c.caused == b.event_id and c.node == b.node and abs(c.t - b.start) <= 2 for c in sim.changes)
    assert total and 0.6 <= recorded / total < 1.0  # recorded most of the time, not always


def test_changes_do_not_perturb_faults_or_clean_scenario():
    assert simulate(seed=5).changes == []
    no_changes = {k: v for k, v in SCENARIOS["hard"].items() if not k.startswith("change")}
    a, b = simulate(seed=5, **SCENARIOS["hard"]), simulate(seed=5, **no_changes)
    assert [f.to_dict() for f in a.faults] == [f.to_dict() for f in b.faults]
    assert np.allclose(a.metrics.to_numpy(), b.metrics.to_numpy(), equal_nan=True)
