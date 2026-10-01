import numpy as np

from sentinel.simulator import Fault, simulate


def test_deterministic_for_seed():
    a, b = simulate(seed=7, minutes=600), simulate(seed=7, minutes=600)
    assert np.allclose(a.metrics.to_numpy(), b.metrics.to_numpy())
    assert [f.to_dict() for f in a.faults] == [f.to_dict() for f in b.faults]


def test_warmup_is_fault_free_and_faults_do_not_overlap():
    sim = simulate(seed=3)
    assert sim.faults, "expected some faults in a simulated day"
    assert all(f.start >= sim.warmup for f in sim.faults)
    for prev, nxt in zip(sim.faults, sim.faults[1:]):
        assert nxt.start > prev.end


def test_injected_cpu_fault_is_visible_and_cascades():
    fault = Fault("F001", "cpu_saturation", "access-sw-3", start=300, duration=30, intensity=1.0)
    sim = simulate(seed=1, minutes=600, faults=[fault])
    cpu = sim.series("access-sw-3", "cpu_pct")
    assert cpu[305:325].mean() > cpu[200:290].mean() + 40
    db_lat = sim.series("db-1", "latency_ms")  # downstream, 1 hop
    assert db_lat[305:325].mean() > 1.5 * db_lat[200:290].mean()
    assert any("CPUHOG" in ln.message for ln in sim.logs if ln.node == "access-sw-3")


def test_metric_bounds():
    sim = simulate(seed=5)
    for (node, metric) in sim.metrics.columns:
        x = sim.series(node, metric)
        assert x.min() >= 0
        if metric.endswith("_pct") and metric in ("cpu_pct", "mem_pct"):
            assert x.max() <= 100
