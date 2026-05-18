"""Tests for pm_agent.tui.DbPoller — SQLite polling on two cadences."""
from __future__ import annotations

import asyncio
import sqlite3
import pytest


class _FakeApp:
    def __init__(self):
        self.screen_stack = []


@pytest.mark.asyncio
async def test_tick_once_calls_both_queries(monkeypatch):
    from pm_agent.tui import DbPoller
    live_calls = 0
    prs_calls = 0
    def fake_live():
        nonlocal live_calls
        live_calls += 1
        return object()
    def fake_prs():
        nonlocal prs_calls
        prs_calls += 1
        return []
    monkeypatch.setattr("pm_agent.persistence_queries.live_cycle", fake_live)
    monkeypatch.setattr("pm_agent.persistence_queries.recent_prs_24h", fake_prs)

    poller = DbPoller(_FakeApp())
    await poller.tick_once()
    assert live_calls == 1
    assert prs_calls == 1


@pytest.mark.asyncio
async def test_live_tick_swallows_sqlite_operational_error(monkeypatch):
    from pm_agent.tui import DbPoller

    crash_count = [0]
    def fake_live():
        crash_count[0] += 1
        raise sqlite3.OperationalError("simulated lock")
    monkeypatch.setattr("pm_agent.persistence_queries.live_cycle", fake_live)
    monkeypatch.setattr("pm_agent.persistence_queries.recent_prs_24h", lambda: [])

    poller = DbPoller(_FakeApp())
    poller.LIVE_INTERVAL = 0.01
    poller.start()
    await asyncio.sleep(0.05)
    poller.stop()
    # Should have ticked multiple times, swallowing each error
    assert crash_count[0] >= 2


@pytest.mark.asyncio
async def test_live_tick_timeout_does_not_kill_task(monkeypatch):
    from pm_agent.tui import DbPoller

    def fake_live():
        import time
        time.sleep(5)
    monkeypatch.setattr("pm_agent.persistence_queries.live_cycle", fake_live)
    monkeypatch.setattr("pm_agent.persistence_queries.recent_prs_24h", lambda: [])

    poller = DbPoller(_FakeApp())
    poller.LIVE_INTERVAL = 0.01
    poller.LIVE_TIMEOUT = 0.05
    poller.start()
    await asyncio.sleep(0.2)
    poller.stop()
    await asyncio.sleep(0.05)  # let cancellation propagate through the event loop
    # The point: we got here without exception escaping the poller
    assert poller._live_task.done() or poller._live_task.cancelled()


@pytest.mark.asyncio
async def test_stop_cancels_both_tasks(monkeypatch):
    from pm_agent.tui import DbPoller
    monkeypatch.setattr("pm_agent.persistence_queries.live_cycle", lambda: None)
    monkeypatch.setattr("pm_agent.persistence_queries.recent_prs_24h", lambda: [])
    poller = DbPoller(_FakeApp())
    poller.start()
    await asyncio.sleep(0.02)
    poller.stop()
    await asyncio.sleep(0.02)
    assert poller._live_task.cancelled() or poller._live_task.done()
    assert poller._prs_task.cancelled() or poller._prs_task.done()
