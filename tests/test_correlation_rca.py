from sentinel.correlation import correlate
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
