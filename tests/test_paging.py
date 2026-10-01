"""Change-aware paging: hold a page after a recorded change, drop it if the anomaly clears."""

from types import SimpleNamespace

from sentinel.correlation import Incident
from sentinel.detection import Alert
from sentinel.evaluation import score_day
from sentinel.paging import apply_change_hold
from sentinel.simulator import ChangeEvent, Fault


def _inc(iid, node, start, end):
    return Incident(iid, [Alert(f"A-{iid}", node, "cpu_pct", "robust_z+ewma", start, end, 3.0, "critical")])


def test_short_anomaly_right_after_a_change_is_not_paged():
    inc = _inc("INC-1", "dist-sw-2", 100, 106)  # a config push spike that clears in 6 min
    apply_change_hold([inc], [ChangeEvent("C1", "config_push", "dist-sw-2", 99)], window=15, grace=10)
    assert inc.held and inc.suppressed and inc.paged_at is None


def test_persistent_anomaly_after_a_change_pages_late():
    inc = _inc("INC-1", "db-1", 100, 140)  # a bad deploy: still broken when the hold expires
    apply_change_hold([inc], [ChangeEvent("C1", "deploy", "db-1", 95)], window=15, grace=10)
    assert inc.held and not inc.suppressed and inc.paged_at == 110


def test_no_recent_change_pages_immediately():
    incs = [_inc("INC-1", "db-1", 100, 104), _inc("INC-2", "db-1", 200, 204)]
    changes = [ChangeEvent("C1", "deploy", "db-1", 60), ChangeEvent("C2", "deploy", "web-1", 199)]  # too old / other node
    apply_change_hold(incs, changes, window=15, grace=10)
    assert all(not i.held and not i.suppressed and i.paged_at == i.start for i in incs)


def test_hold_disabled_with_zero_grace():
    inc = _inc("INC-1", "dist-sw-2", 100, 106)
    apply_change_hold([inc], [ChangeEvent("C1", "config_push", "dist-sw-2", 99)], window=15, grace=0)
    assert not inc.held and inc.paged_at == 100


def test_evaluation_ignores_suppressed_and_delays_held_pages():
    fault = Fault("F001", "cpu_saturation", "db-1", 100, 40)
    real = _inc("INC-1", "db-1", 101, 140)
    real.held, real.paged_at = True, 111
    noise = _inc("INC-2", "dist-sw-2", 300, 305)
    noise.held, noise.suppressed, noise.paged_at = True, True, None
    sim = SimpleNamespace(faults=[fault], benign=[], blackouts=[], changes=[], seed=0)
    day = score_day(sim, SimpleNamespace(incidents=[real, noise], alerts=real.alerts + noise.alerts))
    assert day.n_incidents == 1 and day.n_matched_incidents == 1  # the suppressed incident never paged
    assert day.faults[0].ttd == 11.0  # detection counts from the page, not the first alert
    assert day.n_suppressed == 1 and day.n_suppressed_real == 0


# ---------------------------------------------------------------- hold per root cause
def _multi_root_incident():
    """access-sw-2 (config pushed at 98) cascades to cache-1; web-1 has its own CPU fault from 104 (declared root)."""
    alerts = [
        Alert("A1", "access-sw-2", "packet_loss_pct", "robust_z+ewma", 100, 140, 3.0, "critical"),
        Alert("A2", "cache-1", "latency_ms", "robust_z+ewma", 101, 140, 2.0, "warning"),
        Alert("A3", "web-1", "cpu_pct", "robust_z+ewma", 104, 140, 3.0, "critical"),
    ]
    inc = Incident("INC-1", alerts)
    inc.root_causes = [{"node": "access-sw-2", "declared": True}, {"node": "web-1", "declared": True}, {"node": "cache-1", "declared": False}]
    return inc


def test_unrelated_root_pages_without_waiting_for_its_partners_change():
    change = [ChangeEvent("C1", "config_push", "access-sw-2", 98)]
    per_incident, per_root = _multi_root_incident(), _multi_root_incident()
    apply_change_hold([per_incident], change, window=15, grace=10, per_root=False)
    apply_change_hold([per_root], change, window=15, grace=10, per_root=True)
    assert per_incident.paged_at == 110  # the whole incident waited for the hold
    assert per_root.held and not per_root.suppressed and per_root.paged_at == 104  # web-1's group paged at once


def test_incident_is_suppressed_only_if_every_root_group_clears():
    inc = _multi_root_incident()
    for a in inc.alerts:
        a.end = a.start + 4  # everything clears fast
    apply_change_hold([inc], [ChangeEvent("C1", "config_push", "access-sw-2", 98)], window=15, grace=10, per_root=True)
    assert not inc.suppressed and inc.paged_at == 104  # web-1 had no change, so it still pages
    inc = _multi_root_incident()
    for a in inc.alerts:
        a.end = a.start + 4
    changes = [ChangeEvent("C1", "config_push", "access-sw-2", 98), ChangeEvent("C2", "deploy", "web-1", 103)]
    apply_change_hold([inc], changes, window=15, grace=10, per_root=True)
    assert inc.suppressed and inc.paged_at is None
