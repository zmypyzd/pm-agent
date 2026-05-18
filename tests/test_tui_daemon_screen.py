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
