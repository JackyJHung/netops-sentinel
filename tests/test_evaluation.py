import json

import numpy as np
import pytest

from sentinel.cli import main
from sentinel.evaluation import (
    SPLITS,
    DayScore,
    FaultOutcome,
    intensity_bucket,
    score_day,
    summarize,
    to_markdown,
)
from sentinel.pipeline import run_pipeline
from sentinel.simulator import Fault, simulate


def _fault(kind="cpu_saturation", intensity=0.9, detected=True, ttd=2.0, top1=True):
    return FaultOutcome("F001", kind, "db-1", intensity, detected, ttd if detected else None, top1, top1, top1)


def test_splits_are_disjoint_and_fixed():
    assert SPLITS["tune"] == tuple(range(0, 10))
    assert SPLITS["test"] == tuple(range(100, 130))
    assert not set(SPLITS["tune"]) & set(SPLITS["test"])


def test_intensity_buckets():
    assert intensity_bucket(0.3) == intensity_bucket(0.49) == "subtle"
    assert intensity_bucket(0.5) == intensity_bucket(1.0) == "hard"


def test_metrics_are_pooled_over_days_not_averaged():
    # day 1: 1 of 2 incidents matched; day 2: 8 of 8. Pooled precision is 9/10, a per-day mean would be 0.75.
    days = [
        DayScore(0, n_alerts=4, n_incidents=2, n_matched_incidents=1, faults=[_fault()]),
        DayScore(1, n_alerts=16, n_incidents=8, n_matched_incidents=8, faults=[_fault(), _fault(detected=False)]),
    ]
    overall = summarize({"x": days})["configs"]["x"]["overall"]
    assert overall["precision"]["mean"] == pytest.approx(0.9)
    assert overall["recall"]["mean"] == pytest.approx(2 / 3, abs=1e-3)
    assert overall["alert_compression"]["mean"] == pytest.approx(2.0)
    assert overall["n_incidents"]["mean"] == pytest.approx(5.0)


def test_bootstrap_ci_brackets_estimate_and_is_deterministic():
    rng = np.random.default_rng(0)
    days = [
        DayScore(s, 10, 5, int(rng.integers(3, 6)), [_fault(detected=bool(rng.random() < 0.8)) for _ in range(5)])
        for s in range(20)
    ]
    a = summarize({"x": days})["configs"]["x"]["overall"]
    b = summarize({"x": days})["configs"]["x"]["overall"]
    assert a == b
    for m in ("precision", "recall", "f1"):
        assert a[m]["lo"] <= a[m]["mean"] <= a[m]["hi"]
        assert a[m]["lo"] < a[m]["hi"], "a noisy metric should have a non-degenerate interval"


def test_constant_days_give_zero_width_interval():
    days = [DayScore(s, 10, 5, 5, [_fault(), _fault()]) for s in range(10)]
    r = summarize({"x": days})["configs"]["x"]["overall"]["precision"]
    assert r["lo"] == r["mean"] == r["hi"] == 1.0


def test_breakdown_by_kind_and_intensity():
    days = [
        DayScore(0, 10, 3, 3, [_fault("cpu_saturation", 0.3), _fault("link_flap", 0.9, detected=False), _fault("link_flap", 0.6)]),
    ]
    cfg = summarize({"x": days})["configs"]["x"]
    assert cfg["by_kind"]["link_flap"]["n_faults"] == 2
    assert cfg["by_kind"]["link_flap"]["recall"]["mean"] == pytest.approx(0.5)
    assert cfg["by_kind"]["memory_leak"]["n_faults"] == 0
    assert cfg["by_kind"]["memory_leak"]["recall"]["mean"] is None  # undefined, not 0 or 1
    assert cfg["by_intensity"]["subtle"]["n_faults"] == 1
    assert cfg["by_intensity"]["hard"]["recall"]["mean"] == pytest.approx(0.5)


def test_score_day_records_every_fault():
    faults = [
        Fault("F001", "cpu_saturation", "dist-sw-1", start=300, duration=30, intensity=1.0),
        Fault("F002", "latency_degradation", "db-1", start=500, duration=30, intensity=0.4),
    ]
    sim = simulate(seed=1, minutes=720, faults=faults)
    day = score_day(sim, run_pipeline(sim))
    assert [f.fault_id for f in day.faults] == ["F001", "F002"]
    assert day.faults[0].detected and day.faults[0].ttd is not None
    assert day.n_matched_incidents <= day.n_incidents


def test_markdown_has_ci_and_breakdowns():
    days = [DayScore(0, 10, 3, 3, [_fault()]), DayScore(1, 10, 3, 2, [_fault("memory_leak", 0.4)])]
    md = to_markdown(summarize({"sentinel": days}, split="tune", seeds=[0, 1], minutes=1440))
    assert "tune" in md and "[" in md
    assert "memory_leak" in md and "subtle" in md


def test_cli_eval_writes_split_reports(tmp_path):
    main(["eval", "--split", "tune", "--seeds", "1", "--minutes", "600", "--jobs", "1", "--out", str(tmp_path)])
    report = json.loads((tmp_path / "benchmark-tune.json").read_text())
    assert report["split"] == "tune" and report["seeds"] == [0]
    assert set(report["scenarios"]) == {"clean", "hard"}
    sentinel = report["scenarios"]["hard"]["configs"]["sentinel"]
    assert {"mean", "lo", "hi"} <= set(sentinel["overall"]["recall"])
    assert set(sentinel["by_kind"]) == {"cpu_saturation", "link_flap", "memory_leak", "latency_degradation"}
    assert report["scenarios"]["clean"]["configs"]["sentinel"]["overall"]["benign_paged"]["mean"] is None
    md = (tmp_path / "benchmark-tune.md").read_text()
    assert "Scenario: clean" in md and "Scenario: hard" in md
