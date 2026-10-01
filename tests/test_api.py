import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from sentinel.api import app  # noqa: E402

client = TestClient(app)


def test_health():
    assert client.get("/health").json() == {"status": "ok"}


def test_simulate_and_query():
    r = client.post("/simulate", json={"seed": 3, "minutes": 720})
    assert r.status_code == 200 and r.json()["incidents"] >= 1
    incs = client.get("/incidents").json()
    detail = client.get(f"/incidents/{incs[0]['incident_id']}").json()
    assert detail["alerts"] and detail["runbook"]["id"].startswith("RB-")
    assert client.get("/incidents/INC-9999").status_code == 404
    ev = client.get("/evaluation").json()
    assert 0 <= ev["precision"] <= 1
    s = client.get(f"/series/{detail['root_cause']}/cpu_pct").json()
    assert len(s["values"]) == 720


def test_dashboard_served():
    r = client.get("/")
    assert r.status_code == 200 and "NetOps Sentinel" in r.text


def test_hard_scenario_ground_truth_and_gaps():
    r = client.post("/simulate", json={"seed": 5, "minutes": 1440, "scenario": "hard"})
    assert r.status_code == 200 and r.json()["scenario"] == "hard"
    truth = client.get("/ground-truth").json()
    assert truth["faults"] and truth["benign"] and truth["blackouts"]
    b = truth["blackouts"][0]
    node_kind = {n["name"]: n["kind"] for n in client.get("/topology").json()["nodes"]}[b["node"]]
    metric = "error_rate_pct" if node_kind == "service" else "packet_loss_pct"
    s = client.get(f"/series/{b['node']}/{metric}").json()  # NaN must serialize as null, not crash
    assert s["values"][b["start"]] is None
    assert "benign_paged" in client.get("/evaluation").json()


def test_rejects_unknown_scenario():
    assert client.post("/simulate", json={"scenario": "chaos"}).status_code == 422


def test_incidents_expose_multiple_roots_and_silences():
    client.post("/simulate", json={"seed": 1, "minutes": 1440, "scenario": "hard"})
    incs = client.get("/incidents").json()
    for i in incs:
        assert i["root_cause"] == i["roots"][0]
        assert set(i["runbooks"]) == set(i["roots"])
    silent = [i for i in incs if i["silences"]]
    assert silent, "the hard scenario should produce an incident with a silent node"
    detail = client.get(f"/incidents/{silent[0]['incident_id']}").json()
    assert {"node", "start", "end"} <= set(detail["silences"][0])
    assert any(c["silent"] for c in detail["root_causes"])


def test_changes_in_incidents_and_ground_truth():
    client.post("/simulate", json={"seed": 4, "minutes": 1440, "scenario": "hard"})
    truth = client.get("/ground-truth").json()
    assert truth["changes"] and "caused" in truth["changes"][0]
    incs = [client.get(f"/incidents/{i['incident_id']}").json() for i in client.get("/incidents").json()]
    with_change = [i for i in incs if i["changes"]]
    assert with_change, "some incident should follow a recorded change"
    change = with_change[0]["changes"][0]
    assert "caused" not in change  # the pipeline's view has no ground truth
    assert "Recent change:" in with_change[0]["summary"]
