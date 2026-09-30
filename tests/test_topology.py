import pytest

from sentinel.topology import Node, Topology


def test_default_topology_loads():
    topo = Topology.default()
    assert len(topo) == 10
    assert topo.nodes["core-rtr-1"].is_network
    assert "error_rate_pct" in topo.nodes["db-1"].metrics


def test_upstream_and_downstream():
    topo = Topology.default()
    assert topo.upstream("web-1") >= {"access-sw-1", "api-1", "db-1", "core-rtr-1"}
    down = topo.downstream("access-sw-3")
    assert down["db-1"] == 1
    assert down["api-1"] == 2
    assert down["web-1"] == 3
    assert "core-rtr-1" not in down


def test_related():
    topo = Topology.default()
    assert topo.related("db-1", "web-1")  # dependency chain
    assert topo.related("dist-sw-1", "dist-sw-2")  # siblings, 2 hops
    assert not topo.related("cache-1", "access-sw-3", max_hops=1)


def test_cycle_rejected():
    with pytest.raises(ValueError):
        Topology([Node("a", "switch", ("b",)), Node("b", "switch", ("a",))])


def test_unknown_parent_rejected():
    with pytest.raises(ValueError):
        Topology([Node("a", "switch", ("ghost",))])
