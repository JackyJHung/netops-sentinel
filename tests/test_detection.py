import numpy as np

from sentinel.detection import EWMAControl, RobustZScore, detect_metric_anomalies, intervals
from sentinel.simulator import Fault, simulate


def _series(seed=0, n=600):
    rng = np.random.default_rng(seed)
    return 30 + rng.normal(0, 2, n)


def test_intervals_bridges_gaps_and_drops_blips():
    m = np.zeros(30, bool)
    m[[2]] = True  # single blip
    m[10:13] = True
    m[14:17] = True  # gap of 1 -> bridged
    assert intervals(m, min_len=3, max_gap=2) == [(10, 17)]


def test_robust_z_flags_spike_not_noise():
    x = _series()
    x[400:420] += 40
    z = RobustZScore("cpu_pct").score(x)
    assert (z[400:420] > 5).all()
    assert (z[150:390] > 5).sum() == 0


def test_ewma_baseline_freezes_during_incident():
    x = _series()
    x[300:500] += 40  # long incident
    z = EWMAControl("cpu_pct").score(x)
    assert (z[480:500] > 6).all(), "long incident should not be absorbed into the baseline"


def test_detects_injected_fault_with_few_false_alerts():
    fault = Fault("F001", "cpu_saturation", "dist-sw-1", start=400, duration=30)
    sim = simulate(seed=11, minutes=720, faults=[fault])
    alerts = detect_metric_anomalies(sim.metrics, warmup=sim.warmup)
    on_root = [a for a in alerts if a.node == "dist-sw-1" and a.signal == "cpu_pct"]
    assert on_root and abs(on_root[0].start - 400) <= 5
    outside = [a for a in alerts if a.end < 390 or a.start > 470]
    assert len(outside) <= 2


def test_forecast_catches_slow_leak_before_static_threshold():
    from sentinel.detection import SaturationForecast

    rng = np.random.default_rng(0)
    x = 50 + rng.normal(0, 0.6, 600)
    x[300:400] += np.linspace(0, 30, 100)  # slow leak: +0.3%/min, never reaches 90%
    score = SaturationForecast().score(x)
    fired = np.flatnonzero(score[200:] > 1.0) + 200
    assert fired.size and fired[0] < 330, "should warn within ~30 min of the leak starting"
    assert (score[:290] > 1.0).sum() == 0


def test_univariate_detectors_handle_nan_gaps():
    from sentinel.detection import StaticThreshold

    x = _series(n=600)
    x[250:258] = np.nan
    x[330:333] = np.nan
    x[400:420] += 40
    z = RobustZScore("cpu_pct").score(x)
    e = EWMAControl("cpu_pct").score(x)
    s = StaticThreshold("cpu_pct").score(x)
    for score in (z, e, s):
        assert score.shape == x.shape
        assert not (np.nan_to_num(score[240:340]) > 5).any(), "a gap must not look anomalous"
    assert (z[400:420] > 5).all() and (e[405:420] > 6).all()


def test_forecast_tolerates_gaps_in_a_leak():
    from sentinel.detection import SaturationForecast

    rng = np.random.default_rng(0)
    x = 50 + rng.normal(0, 0.6, 600)
    x[300:400] += np.linspace(0, 30, 100)
    x[310:316] = np.nan
    x[150:160] = np.nan
    score = SaturationForecast().score(x)
    fired = np.flatnonzero(score[200:] > 1.0) + 200
    assert fired.size and fired[0] < 335
    assert (score[:290] > 1.0).sum() == 0


def test_iforest_ignores_blackout():
    import pandas as pd

    from sentinel.detection import NodeIsolationForest

    rng = np.random.default_rng(1)
    frame = pd.DataFrame({m: 30 + rng.normal(0, 2, 600) for m in ("cpu_pct", "mem_pct", "latency_ms", "error_rate_pct")})
    frame.iloc[300:360] = np.nan
    s = NodeIsolationForest(warmup=180).score(frame)
    assert (s[300:360] <= 0).all()
