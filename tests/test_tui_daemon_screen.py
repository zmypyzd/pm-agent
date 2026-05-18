"""Tests for GoalScreen and DaemonScreen via App.run_test()."""
from __future__ import annotations

import pytest


def test_goal_screen_class_exists():
    from pm_agent.tui import GoalScreen
    from textual.screen import Screen
    assert issubclass(GoalScreen, Screen)


def test_existing_pmagenttui_alias_still_exists():
    """Regression: pm_agent.tui.PMAgentTUI is still importable for tests."""
    from pm_agent.tui import PMAgentTUI
    assert PMAgentTUI is not None


def test_artifacts_root_stays_module_level():
    """tests/conftest.py monkey-patches ARTIFACTS_ROOT — must stay at tui.py top."""
    import pm_agent.tui as tui_mod
    assert hasattr(tui_mod, "ARTIFACTS_ROOT")


import asyncio
import pytest


@pytest.mark.asyncio
async def test_daemon_screen_hard_check_names_match_preflight():
    """HARD_CHECK_NAMES strings MUST equal preflight CheckResult.name strings.
    This is the critical regression gate from Spec §B1 (round-2 audit)."""
    from pm_agent import preflight as pf
    from pm_agent.tui import HARD_CHECK_NAMES
    from pathlib import Path

    db = Path("/tmp/pm-agent-test-state.db.never-exists")
    # We don't care about ok values; only the .name strings each check
    # returns. Run against a non-existent repo so checks return cleanly.
    results, _ = pf.run_preflight(db, Path("/nonexistent"))
    actual_names = {r.name for r in results}
    missing = HARD_CHECK_NAMES - actual_names
    assert not missing, (
        f"HARD_CHECK_NAMES contains strings not produced by preflight.py: "
        f"{missing}. Actual preflight names: {actual_names}"
    )
