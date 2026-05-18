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
