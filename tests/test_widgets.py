"""Tests for new TUI widgets (PreflightBar, CycleSummaryRow)."""
from __future__ import annotations

import pytest

from pm_agent.preflight import CheckResult


def _hard_ok():
    return [
        CheckResult("state.db dir writable", True, "/x"),
        CheckResult("state.db clean",        True, "fresh"),
        CheckResult("claude CLI on PATH",    True, "/usr/local/bin/claude"),
        CheckResult("gh CLI authenticated",  True, "ok"),
        CheckResult("repo clean",            True, "/x"),
        CheckResult("repo on main",          True, "main", warn_only=True),
        CheckResult("/tmp writable",         True, "/tmp"),
    ]


def test_preflight_bar_renders_seven_cells_when_all_pass():
    from pm_agent.tui import PreflightBar
    bar = PreflightBar()
    bar.update_from(_hard_ok())
    text = str(bar.renderable)
    assert text.count("│") == 6
    assert "db dir" in text or "db dir writable" in text
    assert "gh" in text
    assert "/tmp" in text


def test_preflight_bar_renders_failure_in_red():
    from pm_agent.tui import PreflightBar
    results = _hard_ok()
    results[3] = CheckResult("gh CLI authenticated", False, "not logged in")
    bar = PreflightBar()
    bar.update_from(results)
    text = str(bar.renderable)
    assert "✗" in text
    assert "red" in text


def test_preflight_bar_warn_only_is_yellow():
    from pm_agent.tui import PreflightBar
    results = _hard_ok()
    results[5] = CheckResult("repo on main", False, "feature/x", warn_only=True)
    bar = PreflightBar()
    bar.update_from(results)
    text = str(bar.renderable)
    assert "⚠" in text
    assert "yellow" in text


def test_cycle_summary_row_idle_no_history():
    from pm_agent.tui import CycleSummaryRow
    from pm_agent.persistence_queries import LiveCyclePayload
    row = CycleSummaryRow()
    row.update_from(LiveCyclePayload(status="idle", cycle=None,
                                     findings=[], last_finished=None))
    text = str(row.renderable)
    assert "idle" in text
    assert "press 's'" in text


def test_cycle_summary_row_idle_with_history():
    from pm_agent.tui import CycleSummaryRow
    from pm_agent.persistence_queries import (
        LiveCyclePayload, FinishedCycleSummary)
    row = CycleSummaryRow()
    lf = FinishedCycleSummary(
        id=7, status="done", started_at="2026-05-18T12:00:00+00:00",
        finished_at="2026-05-18T12:05:00+00:00", cost_usd=1.23,
    )
    row.update_from(LiveCyclePayload(status="idle", cycle=None,
                                     findings=[], last_finished=lf))
    text = str(row.renderable)
    assert "last cycle" in text
    assert "#7" in text
    assert "1.23" in text


def test_cycle_summary_row_running():
    from pm_agent.tui import CycleSummaryRow
    from pm_agent.persistence_queries import (
        LiveCyclePayload, CycleSummary)
    row = CycleSummaryRow()
    cs = CycleSummary(
        id=42, status="running",
        started_at="2026-05-18T12:00:00+00:00",
        finished_at=None, cost_usd=0.5,
        findings_total=5, findings_pr_opened=2,
    )
    row.update_from(LiveCyclePayload(status="running", cycle=cs,
                                     findings=[], last_finished=None))
    text = str(row.renderable)
    assert "#42" in text
    assert "PR opened" in text
    assert "2" in text
    assert "5" in text


def test_cycle_summary_row_shows_last_error_when_present():
    from pm_agent.tui import CycleSummaryRow
    from pm_agent.persistence_queries import LiveCyclePayload
    row = CycleSummaryRow()
    row.set_last_error(RuntimeError("gh auth lost"))
    row.update_from(LiveCyclePayload(status="idle", cycle=None,
                                     findings=[], last_finished=None))
    text = str(row.renderable)
    assert "crashed" in text
    assert "gh auth lost" in text
