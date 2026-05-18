"""Tests for pm_agent.persistence_queries — shared SQL for dashboard + TUI."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pm_agent import persistence, persistence_queries as queries
from pm_agent.scanner import Finding


def _f(bug_id: str = "ab12cd34", title: str = "t") -> Finding:
    return Finding(
        bug_id=bug_id, title=title, severity="Low",
        paths=["a.py"], acceptance=["ok"], evidence="e", kind="bug",
    )


def test_live_cycle_idle_no_history(tmp_path):
    persistence.init_db(tmp_path / "state.db")
    payload = queries.live_cycle()
    assert payload.status == "idle"
    assert payload.cycle is None
    assert payload.findings == []
    assert payload.last_finished is None


def test_live_cycle_idle_with_history(tmp_path):
    persistence.init_db(tmp_path / "state.db")
    cid = persistence.start_cycle()
    persistence.record_cost(cid, "scanner", 0.10)
    persistence.finish_cycle(cid, "done", 0.10)
    payload = queries.live_cycle()
    assert payload.status == "idle"
    assert payload.cycle is None
    assert payload.last_finished is not None
    assert payload.last_finished.id == cid
    assert payload.last_finished.status == "done"
    assert payload.last_finished.cost_usd == pytest.approx(0.10)


def test_live_cycle_running_counts_total_and_pr_opened(tmp_path):
    persistence.init_db(tmp_path / "state.db")
    cid = persistence.start_cycle()  # leaves it 'running'
    f1_id = persistence.record_finding(cid, _f("aaa11111", "t1"))
    f2_id = persistence.record_finding(cid, _f("bbb22222", "t2"))
    f3_id = persistence.record_finding(cid, _f("ccc33333", "t3"))
    persistence.update_finding(f1_id, "done")  # PR opened
    persistence.update_finding(f2_id, "fixing-code")
    # f3 stays 'discovered'
    persistence.record_cost(cid, "scanner", 0.05)
    persistence.record_cost(cid, "coder-1", 0.03)

    payload = queries.live_cycle()
    assert payload.status == "running"
    assert payload.cycle.id == cid
    assert payload.cycle.findings_total == 3
    assert payload.cycle.findings_pr_opened == 1
    assert payload.cycle.cost_usd == pytest.approx(0.08)
    assert len(payload.findings) == 3
    # ORDER BY id DESC: newest first
    assert payload.findings[0].bug_id == "ccc33333"


def test_live_cycle_findings_limit_50(tmp_path):
    persistence.init_db(tmp_path / "state.db")
    cid = persistence.start_cycle()
    for i in range(60):
        persistence.record_finding(cid, _f(f"hex{i:05d}", f"t{i}"))
    payload = queries.live_cycle()
    assert payload.cycle.findings_total == 60
    assert len(payload.findings) == 50


def test_recent_prs_24h_empty(tmp_path):
    persistence.init_db(tmp_path / "state.db")
    assert queries.recent_prs_24h() == []


def test_recent_prs_24h_returns_recent(tmp_path):
    persistence.init_db(tmp_path / "state.db")
    cid = persistence.start_cycle()
    fid = persistence.record_finding(cid, _f("xx111111", "fix"))
    persistence.record_pr(fid, 42, "https://example.com/pr/42", "open", "opened")
    rows = queries.recent_prs_24h()
    assert len(rows) == 1
    assert rows[0].github_number == 42
    assert rows[0].bug_id == "xx111111"
    assert rows[0].state == "open"


def test_recent_prs_24h_ignores_older_than_24h(tmp_path, monkeypatch):
    """A PR created 25 hours ago must NOT appear."""
    persistence.init_db(tmp_path / "state.db")
    cid = persistence.start_cycle()
    fid = persistence.record_finding(cid, _f("yy222222", "old"))
    # Patch persistence._now once to seed an "old" PR row
    old_ts = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    monkeypatch.setattr(persistence, "_now", lambda: old_ts)
    persistence.record_pr(fid, 1, "u", "open", "opened")
    monkeypatch.undo()
    # And a fresh PR
    persistence.record_pr(fid, 2, "u", "open", "opened")

    rows = queries.recent_prs_24h()
    nums = [r.github_number for r in rows]
    assert 1 not in nums
    assert 2 in nums


def test_recent_prs_24h_order_desc_by_id(tmp_path):
    persistence.init_db(tmp_path / "state.db")
    cid = persistence.start_cycle()
    fid = persistence.record_finding(cid, _f("zz333333", "x"))
    persistence.record_pr(fid, 1, "u", "open", "opened")
    persistence.record_pr(fid, 2, "u", "open", "opened")
    persistence.record_pr(fid, 3, "u", "open", "opened")
    rows = queries.recent_prs_24h()
    assert [r.github_number for r in rows] == [3, 2, 1]
