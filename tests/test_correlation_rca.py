from sentinel.correlation import Silence, correlate, detect_silences
from sentinel.detection import Alert
from sentinel.rca import rank_root_causes
from sentinel.runbooks import load_runbooks, match_runbook
from sentinel.topology import Topology


def _a(i, node, start, end=None, signal="latency_ms", sev="warning", det="robust_z+ewma"):
    return Alert(f"A{i}", node, signal, det, start, end or start + 10, 2.0, sev)


def test_related_alerts_group_and_unrelated_split():
    topo = Topology.default()
    alerts = [
        _a(1, "access-sw-3", 100, signal="packet_loss_pct"),
        _a(2, "db-1", 101),
        _a(3, "api-1", 103),
        _a(4, "web-1", 400),  # much later -> separate incident
    ]
    incs = correlate(alerts, topo)
    assert len(incs) == 2
    assert set(incs[0].nodes) == {"access-sw-3", "db-1", "api-1"}


def test_rca_prefers_upstream_early_node():
    topo = Topology.default()
    alerts = [
        _a(1, "access-sw-3", 100, signal="packet_loss_pct", sev="critical"),
        _a(2, "db-1", 101),
        _a(3, "api-1", 102),
        _a(4, "web-1", 103),
    ]
    inc = correlate(alerts, topo)[0]
    ranked = rank_root_causes(inc, topo)
    assert ranked[0]["node"] == "access-sw-3"
    assert "upstream of 3" in ranked[0]["reason"]


def test_runbook_matches_signal():
    topo = Topology.default()
    inc = correlate([_a(1, "cache-1", 100, signal="mem_pct"), _a(2, "api-1", 104, signal="error_rate_pct")], topo)[0]
    inc.root_causes = rank_root_causes(inc, topo)
    rb = match_runbook(inc, load_runbooks())
    assert inc.root_cause == "cache-1"
    assert rb["id"] == "RB-MEM"


def test_empty_alerts():
    assert correlate([], Topology.default()) == []


# ---------------------------------------------------------------- milestone 3: concurrent faults
def _two_branch_alerts(offset=10):
    """access-sw-3 fault cascades to db-1 -> api-1 -> web-1; `offset` min later an unrelated cache-1 fault
    reaches api-1 and web-1 too. access-sw-3 and cache-1 share no dependency path."""
    return [
        _a(1, "access-sw-3", 100, 140, signal="packet_loss_pct"),
        _a(2, "db-1", 101, 140),
        _a(3, "api-1", 102, 140),
        _a(4, "web-1", 103, 140),
        _a(5, "cache-1", 100 + offset, 150, signal="cpu_pct"),
        _a(6, "api-1", 101 + offset, 150, signal="error_rate_pct"),
        _a(7, "web-1", 102 + offset, 150, signal="error_rate_pct"),
    ]


def test_unrelated_concurrent_faults_are_split():
    topo = Topology.default()
    merged = correlate(_two_branch_alerts(), topo, split=False)
    assert len(merged) == 1
    incs = correlate(_two_branch_alerts(), topo)
    assert len(incs) == 2
    first, second = incs
    assert "access-sw-3" in first.nodes and "cache-1" not in first.nodes
    assert "cache-1" in second.nodes and "access-sw-3" not in second.nodes
    # shared downstream alerts go to the fault whose onset most recently preceded them
    assert {a.alert_id for a in second.alerts} == {"A5", "A6", "A7"}


def test_simultaneous_unrelated_origins_stay_together():
    # same first-alert minute: could be one unseen upstream cause, so don't split
    incs = correlate(_two_branch_alerts(offset=0), Topology.default())
    assert len(incs) == 1


def test_silent_upstream_node_holds_its_downstream_together():
    topo = Topology.default()
    alerts = [_a(1, "access-sw-1", 100, 130, signal="packet_loss_pct"), _a(2, "access-sw-2", 107, 130, signal="packet_loss_pct")]
    assert len(correlate(alerts, topo)) == 2  # siblings with separate onsets look like two faults
    silence = Silence("dist-sw-1", 99, 135)
    (inc,) = correlate(alerts, topo, silences=[silence])
    assert [s.node for s in inc.silences] == ["dist-sw-1"]
    ranked = rank_root_causes(inc, topo)
    assert ranked[0]["node"] == "dist-sw-1"
    assert "stopped reporting" in ranked[0]["reason"]


def test_detect_silences_needs_every_metric_missing():
    import numpy as np
    import pandas as pd

    cols = pd.MultiIndex.from_tuples([("n1", "cpu_pct"), ("n1", "mem_pct"), ("n2", "cpu_pct")])
    data = np.ones((100, 3))
    data[20:30, 0] = np.nan  # one metric only: a gap, not silence
    data[50:60, :2] = np.nan  # whole node for 10 min
    data[70:73, 2] = np.nan  # whole node, but too short
    sil = detect_silences(pd.DataFrame(data, columns=cols), min_len=5)
    assert [(s.node, s.start, s.end) for s in sil] == [("n1", 50, 60)]


def test_rca_declares_second_root_with_local_evidence():
    topo = Topology.default()
    alerts = [
        _a(1, "access-sw-2", 100, 140, signal="packet_loss_pct", sev="critical"),
        _a(2, "api-1", 101, 140),
        _a(3, "web-1", 102, 140),
        _a(4, "cache-1", 106, 140, signal="cpu_pct"),  # downstream of access-sw-2, but CPU does not cascade
    ]
    (inc,) = correlate(alerts, topo)
    ranked = rank_root_causes(inc, topo)
    assert ranked[0]["node"] == "access-sw-2"
    assert [c["node"] for c in ranked if c["declared"]] == ["access-sw-2", "cache-1"]


def test_rca_single_root_when_everything_is_explained():
    topo = Topology.default()
    alerts = [_a(1, "access-sw-3", 100, signal="packet_loss_pct"), _a(2, "db-1", 101), _a(3, "api-1", 102)]
    (inc,) = correlate(alerts, topo)
    assert [c["node"] for c in rank_root_causes(inc, topo) if c["declared"]] == ["access-sw-3"]


# ---------------------------------------------------------------- milestone 5: change events
def test_recent_change_promotes_the_changed_node():
    from sentinel.rca import attach_changes
    from sentinel.simulator import ChangeEvent

    topo = Topology.default()
    # access-sw-2 link fault cascades to cache-1, api-1, web-1. web-1 alerts a little later with only cascading
    # symptoms, so it looks like part of the cascade, but web-1 was deployed 2 min before its first alert.
    alerts = [
        _a(1, "access-sw-2", 100, 140, signal="packet_loss_pct", sev="critical"),
        _a(2, "cache-1", 101, 140),
        _a(3, "api-1", 102, 140),
        _a(4, "web-1", 105, 140),
    ]
    (inc,) = correlate(alerts, topo)
    without = [c["node"] for c in rank_root_causes(inc, topo)]
    attach_changes([inc], [ChangeEvent("C001", "deploy", "web-1", 103)], lookback=15)
    ranked = rank_root_causes(inc, topo)
    assert without.index("web-1") > 1
    assert [c["node"] for c in ranked][:2] == ["access-sw-2", "web-1"]
    assert ranked[1]["declared"] and "deploy to web-1" in ranked[1]["reason"]


def test_changes_outside_lookback_or_on_other_nodes_are_ignored():
    from sentinel.rca import attach_changes
    from sentinel.simulator import ChangeEvent

    topo = Topology.default()
    (inc,) = correlate([_a(1, "access-sw-3", 100, signal="packet_loss_pct"), _a(2, "db-1", 101)], topo)
    changes = [ChangeEvent("C001", "deploy", "db-1", 60), ChangeEvent("C002", "deploy", "web-1", 99)]
    attach_changes([inc], changes, lookback=15)
    assert inc.changes == []


def test_summary_names_the_change():
    from sentinel.rca import attach_changes
    from sentinel.runbooks import summarize
    from sentinel.simulator import ChangeEvent

    topo = Topology.default()
    (inc,) = correlate([_a(1, "dist-sw-2", 845, signal="cpu_pct", sev="critical"), _a(2, "access-sw-3", 846)], topo)
    attach_changes([inc], [ChangeEvent("C001", "config_push", "dist-sw-2", 842)], lookback=15)
    inc.root_causes = rank_root_causes(inc, topo)
    text = summarize(inc, lambda t: f"{t // 60:02d}:{t % 60:02d}")
    assert "config push to dist-sw-2 at 14:02, 3 min before first alert" in text


# ---------------------------------------------------------------- local evidence declares a root (DESIGN D21)
def _router_cpu_with_concurrent_leak():
    """core-rtr-1 CPU fault cascades latency to both distribution switches; 13 min later cache-1 starts
    leaking memory. cache-1 explains nothing below it, so it scores low, but a memory leak cannot come from
    the router's CPU."""
    return [
        _a(1, "core-rtr-1", 788, 791, signal="cpu_pct", sev="critical"),
        _a(2, "core-rtr-1", 788, 791, signal="latency_ms"),
        _a(3, "dist-sw-1", 789, 817),
        _a(4, "dist-sw-2", 789, 834),
        _a(5, "cache-1", 801, 818, signal="mem_pct", det="forecast"),
        _a(6, "cache-1", 811, 818),
    ]


def test_local_evidence_declares_a_root_below_the_score_floor():
    from sentinel.rca import MIN_ROOT_SCORE

    topo = Topology.default()
    (inc,) = correlate(_router_cpu_with_concurrent_leak(), topo)
    old = rank_root_causes(inc, topo, local_root_floor=MIN_ROOT_SCORE)
    cache = next(c for c in old if c["node"] == "cache-1")
    assert cache["score"] < MIN_ROOT_SCORE * old[0]["score"] and not cache["declared"]
    new = rank_root_causes(inc, topo)
    assert [c["node"] for c in new if c["declared"]] == ["core-rtr-1", "cache-1"]


def test_no_local_evidence_still_needs_the_floor():
    topo = Topology.default()
    alerts = [a for a in _router_cpu_with_concurrent_leak() if a.signal != "mem_pct"]  # cache-1 only has latency now
    (inc,) = correlate(alerts, topo)
    assert [c["node"] for c in rank_root_causes(inc, topo) if c["declared"]] == ["core-rtr-1"]


# ---------------------------------------------------------------- runbook for a silent root (inferred from dependents)
def _silent_root_incident(child_alerts):
    topo = Topology.default()
    (inc,) = correlate(child_alerts, topo, silences=[Silence("dist-sw-1", 99, 135)])
    inc.root_causes = rank_root_causes(inc, topo)
    assert inc.root_cause == "dist-sw-1"
    return inc, topo


def test_silent_root_with_lossy_dependents_gets_the_link_runbook():
    inc, topo = _silent_root_incident([
        _a(1, "access-sw-1", 100, 130, signal="packet_loss_pct"),
        _a(2, "access-sw-2", 101, 130, signal="packet_loss_pct"),
        _a(3, "cache-1", 102, 130, signal="error_rate_pct"),
    ])
    rb = match_runbook(inc, load_runbooks(), topo=topo)
    assert rb["id"] == "RB-LINK" and rb["matched_on"] == "dependents"


def test_silent_root_with_slow_dependents_gets_the_cpu_runbook():
    inc, topo = _silent_root_incident([_a(1, "access-sw-1", 100, 130), _a(2, "access-sw-2", 101, 130)])  # latency only
    rb = match_runbook(inc, load_runbooks(), topo=topo)
    assert rb["id"] == "RB-CPU" and rb["matched_on"] == "dependents"


def test_own_evidence_beats_dependents():
    topo = Topology.default()
    alerts = [
        _a(1, "dist-sw-1", 99, 130, signal="cpu_pct", sev="critical"),
        _a(2, "access-sw-1", 100, 130, signal="packet_loss_pct"),
        _a(3, "access-sw-2", 101, 130, signal="packet_loss_pct"),
    ]
    (inc,) = correlate(alerts, topo)
    inc.root_causes = rank_root_causes(inc, topo)
    rb = match_runbook(inc, load_runbooks(), topo=topo)
    assert rb["id"] == "RB-CPU" and rb["matched_on"] == "root"


def test_dependents_never_pick_a_runbook_for_the_wrong_device_type():
    # a silent switch whose dependents show latency and errors (the latency-degradation signature) must not
    # get the service-only slow-dependency runbook
    inc, topo = _silent_root_incident([
        _a(1, "web-1", 100, 130),
        _a(2, "web-1", 101, 130, signal="error_rate_pct"),
    ])
    rb = match_runbook(inc, load_runbooks(), topo=topo)
    assert rb["id"] != "RB-LAT"
