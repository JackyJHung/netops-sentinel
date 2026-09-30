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
