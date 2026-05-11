"""Shared pytest fixtures for the Beta loop test suite."""
from __future__ import annotations

import pytest
from pathlib import Path

# Module-level Path.home() expansion in pm_agent.tui (ARTIFACTS_ROOT) is
# evaluated at import time. tmp_pm_agent_home monkeypatches both the env
# var (for code that reads HOME lazily) AND the module constant directly.


@pytest.fixture
def tmp_pm_agent_home(tmp_path, monkeypatch):
    """Redirect ~/.pm-agent to a tmp dir so tests don't pollute real state.

    Also monkeypatches pm_agent.tui.ARTIFACTS_ROOT in case the TUI module is
    already imported and the constant is cached.
    """
    home = tmp_path / "pm-agent-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    # Patch the cached constant if pm_agent.tui has been imported.
    try:
        import pm_agent.tui  # noqa: PLC0415
        monkeypatch.setattr(
            pm_agent.tui, "ARTIFACTS_ROOT",
            home / ".pm-agent" / "runs",
            raising=False,
        )
    except ImportError:
        pass
    yield home
