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


# ---------------------------------------------------------------- milestone 3: per-fault RCA and merges
def _inc(iid, alerts, ranked, runbooks=None):
    from sentinel.correlation import Incident

    inc = Incident(iid, alerts)
    inc.root_causes = [{"node": n, "declared": i == 0} for i, n in enumerate(ranked)]
    inc.runbooks = runbooks or {}
    inc.runbook = next(iter(inc.runbooks.values()), None)
    return inc


def _alert(i, node, start, end):
    from sentinel.detection import Alert

    return Alert(f"A{i}", node, "latency_ms", "robust_z+ewma", start, end, 2.0, "warning")


def _fake_day(faults, incidents):
    from types import SimpleNamespace

    sim = SimpleNamespace(faults=faults, benign=[], blackouts=[], seed=0)
    res = SimpleNamespace(incidents=incidents, alerts=[a for i in incidents for a in i.alerts])
    return score_day(sim, res)


def test_rca_rank_is_per_fault_and_filtered():
    fa = Fault("F001", "cpu_saturation", "access-sw-3", 100, 30)
    fb = Fault("F001b", "cpu_saturation", "cache-1", 105, 30)
    alerts = [_alert(1, "access-sw-3", 100, 130), _alert(2, "cache-1", 105, 135), _alert(3, "api-1", 106, 135)]
    # both true roots ranked first and second: each fault is a top-1 hit (the other true root is filtered out)
    day = _fake_day([fa, fb], [_inc("INC-1", alerts, ["access-sw-3", "cache-1", "api-1"])])
    assert [f.top1 for f in day.faults] == [True, True]
    # a wrong node between them pushes the second fault to rank 2
    day = _fake_day([fa, fb], [_inc("INC-1", alerts, ["access-sw-3", "api-1", "cache-1"])])
    assert [f.top1 for f in day.faults] == [True, False] and day.faults[1].top3


def test_wrong_merge_counts_unrelated_concurrent_pairs():
    fa = Fault("F001", "cpu_saturation", "access-sw-3", 100, 30)
    fb = Fault("F001b", "cpu_saturation", "cache-1", 105, 30)  # no dependency path to access-sw-3
    a1, a2 = _alert(1, "access-sw-3", 100, 130), _alert(2, "cache-1", 105, 135)
    merged = _fake_day([fa, fb], [_inc("INC-1", [a1, a2], ["access-sw-3", "cache-1"])])
    assert (merged.pairs_unrelated, merged.merged_unrelated) == (1, 1)
    split = _fake_day([fa, fb], [_inc("INC-1", [a1], ["access-sw-3"]), _inc("INC-2", [a2], ["cache-1"])])
    assert (split.pairs_unrelated, split.merged_unrelated) == (1, 0)
    r = summarize({"x": [merged, split]})["configs"]["x"]["overall"]
    assert r["wrong_merge"]["mean"] == pytest.approx(0.5)


def test_runbook_is_judged_for_the_faults_own_root():
    fa = Fault("F001", "link_flap", "access-sw-3", 100, 30)
    fb = Fault("F001b", "memory_leak", "cache-1", 105, 30)
    alerts = [_alert(1, "access-sw-3", 100, 130), _alert(2, "cache-1", 105, 135)]
    runbooks = {"access-sw-3": {"fault_kind": "link_flap"}, "cache-1": {"fault_kind": "memory_leak"}}
    day = _fake_day([fa, fb], [_inc("INC-1", alerts, ["access-sw-3", "cache-1"], runbooks)])
    assert [f.cls_ok for f in day.faults] == [True, True]


def test_rca_is_broken_down_by_change_cause():
    days = [DayScore(0, 10, 3, 3, [_fault(), FaultOutcome("F002", "link_flap", "db-1", 0.9, True, 1.0, False, True, True, after_change=True)])]
    by_cause = summarize({"x": days})["configs"]["x"]["by_cause"]
    assert by_cause["after a change"]["n_faults"] == 1 and by_cause["after a change"]["rca_top1"]["mean"] == 0.0
    assert by_cause["no change"]["rca_top1"]["mean"] == 1.0
