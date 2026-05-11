"""Shared pytest fixtures for the Beta loop test suite."""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture
def tmp_pm_agent_home(tmp_path, monkeypatch):
    """Redirect ~/.pm-agent to a tmp dir so tests don't pollute real state."""
    home = tmp_path / "pm-agent-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    yield home


@pytest.fixture
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()
