"""Unit tests for pm_agent.dashboard — FastAPI + HTMX dashboard."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from pm_agent.dashboard.server import create_app
from pm_agent.persistence import (
    init_db, get_conn, start_cycle, finish_cycle,
    record_finding, record_cost,
)
from pm_agent.scanner import Finding


@pytest.fixture
def seeded_db(tmp_path):
    """Initialize a DB with one finished cycle + 1 finding + cost."""
    init_db(tmp_path / "state.db")
    cid = start_cycle()
    f = Finding(
        bug_id="x1", title="x", severity="Low",
        paths=["a.py"], acceptance=["ok"], evidence="e", kind="bug",
    )
    record_finding(cid, f)
    record_cost(cid, "scanner", 0.05)
    finish_cycle(cid, "done", 0.05)
    return tmp_path / "state.db"


def test_index_renders_non_empty(seeded_db):
    """GET / returns 200 with HTML mentioning Live Cycle and pm-agent."""
    client = TestClient(create_app())
    r = client.get("/")
    assert r.status_code == 200
    assert "Live Cycle" in r.text or "pm-agent" in r.text


def test_api_live_idle_returns_200(tmp_path):
    """GET /api/live with no running cycle returns 200 + status='idle', NOT 404."""
    init_db(tmp_path / "state.db")
    client = TestClient(create_app())
    r = client.get("/api/live")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "idle"
    assert data["cycle"] is None


def test_api_trend_returns_points(seeded_db):
    """GET /api/trend returns 200 with 'points' key in response."""
    client = TestClient(create_app())
    r = client.get("/api/trend")
    assert r.status_code == 200
    assert "points" in r.json()


def test_api_live_running_cycle_returns_findings(tmp_path):
    """A running cycle with findings should be returned in /api/live."""
    init_db(tmp_path / "state.db")
    cid = start_cycle()  # leaves status='running' by default
    f = Finding(
        bug_id="run1", title="active fix", severity="High",
        paths=["b.py"], acceptance=["ok"], evidence="e", kind="bug",
    )
    record_finding(cid, f)
    client = TestClient(create_app())
    r = client.get("/api/live")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "running"
    assert data["cycle"] is not None
    assert data["cycle"]["id"] == cid
    assert len(data["findings"]) == 1
    assert data["findings"][0]["bug_id"] == "run1"


def test_api_trend_cumulative_cost_accumulates(tmp_path):
    """Multiple cycles → cumulative_cost monotonically increases per point."""
    init_db(tmp_path / "state.db")
    for usd in (0.10, 0.20, 0.30):
        cid = start_cycle()
        record_cost(cid, "scanner", usd)
        finish_cycle(cid, "done", usd)
    client = TestClient(create_app())
    r = client.get("/api/trend")
    points = r.json()["points"]
    assert len(points) == 3
    costs = [p["cumulative_cost_usd"] for p in points]
    assert costs == sorted(costs)  # monotonically non-decreasing
    assert abs(costs[-1] - 0.60) < 1e-6  # final cumulative


def test_api_live_running_includes_findings_pr_opened(tmp_path):
    """LiveCycleResponse must include the new `findings_pr_opened` field."""
    init_db(tmp_path / "state.db")
    cid = start_cycle()
    f = Finding(
        bug_id="aa1", title="t", severity="Low",
        paths=["a.py"], acceptance=["ok"], evidence="e", kind="bug",
    )
    fid = record_finding(cid, f)
    from pm_agent.persistence import update_finding
    update_finding(fid, "done")
    client = TestClient(create_app())
    r = client.get("/api/live")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "running"
    assert data["cycle"]["findings_pr_opened"] == 1
    assert data["cycle"]["findings_total"] == 1


def test_api_live_includes_last_finished_when_idle(tmp_path):
    init_db(tmp_path / "state.db")
    cid = start_cycle()
    finish_cycle(cid, "done", 0.0)
    client = TestClient(create_app())
    r = client.get("/api/live")
    data = r.json()
    assert data["status"] == "idle"
    assert data["last_finished"]["id"] == cid
