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
