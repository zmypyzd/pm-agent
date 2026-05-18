"""Tests for the PMAgentTUI App shell."""
from __future__ import annotations

import asyncio
import pytest


@pytest.mark.asyncio
async def test_app_starts_in_goal_mode_by_default(tmp_path):
    from pm_agent.tui import PMAgentTUI, GoalScreen
    async with PMAgentTUI(repo=tmp_path).run_test() as pilot:
        await pilot.pause()
        assert isinstance(pilot.app.screen_stack[-1], GoalScreen)


@pytest.mark.asyncio
async def test_app_opens_daemon_screen_with_flag(tmp_path):
    from pm_agent.tui import PMAgentTUI, DaemonScreen
    async with PMAgentTUI(repo=tmp_path, open_daemon=True).run_test() as pilot:
        await pilot.pause()
        assert isinstance(pilot.app.screen_stack[-1], DaemonScreen)


@pytest.mark.asyncio
async def test_app_d_key_switches_to_daemon(tmp_path):
    from pm_agent.tui import PMAgentTUI, DaemonScreen
    async with PMAgentTUI(repo=tmp_path).run_test() as pilot:
        await pilot.pause()
        await pilot.press("d")
        await pilot.pause()
        assert isinstance(pilot.app.screen_stack[-1], DaemonScreen)


@pytest.mark.asyncio
async def test_is_daemon_active_when_starting(tmp_path):
    from pm_agent.tui import PMAgentTUI
    async with PMAgentTUI(repo=tmp_path).run_test() as pilot:
        assert pilot.app.is_daemon_active is False
        pilot.app.daemon_starting = True
        assert pilot.app.is_daemon_active is True
        pilot.app.daemon_starting = False
        assert pilot.app.is_daemon_active is False


@pytest.mark.asyncio
async def test_on_daemon_done_resets_task_field(tmp_path):
    from pm_agent.tui import PMAgentTUI
    async with PMAgentTUI(repo=tmp_path).run_test() as pilot:
        async def finished():
            return
        t = asyncio.create_task(finished())
        await t
        pilot.app.daemon_task = t
        pilot.app._on_daemon_done(t)
        assert pilot.app.daemon_task is None
        assert pilot.app.daemon_last_error is None


@pytest.mark.asyncio
async def test_on_daemon_done_captures_exception(tmp_path):
    from pm_agent.tui import PMAgentTUI
    async with PMAgentTUI(repo=tmp_path).run_test() as pilot:
        async def crashed():
            raise RuntimeError("boom")
        t = asyncio.create_task(crashed())
        with pytest.raises(RuntimeError):
            await t
        pilot.app.daemon_task = t
        pilot.app._on_daemon_done(t)
        assert pilot.app.daemon_task is None
        assert isinstance(pilot.app.daemon_last_error, RuntimeError)


@pytest.mark.asyncio
async def test_daemon_crash_during_goal_mode_surfaces_on_resume(tmp_path):
    """If daemon dies while user is on GoalScreen, the crashed banner must
    appear when they switch back to DaemonScreen. See final-review finding."""
    from pm_agent.tui import PMAgentTUI, DaemonScreen, CycleSummaryRow
    async with PMAgentTUI(repo=tmp_path).run_test() as pilot:
        await pilot.pause()
        # Start on GoalScreen, simulate daemon crash via _on_daemon_done
        async def crashed():
            raise RuntimeError("simulated daemon crash")
        t = asyncio.create_task(crashed())
        with pytest.raises(RuntimeError):
            await t
        pilot.app.daemon_task = t
        pilot.app._on_daemon_done(t)
        assert isinstance(pilot.app.daemon_last_error, RuntimeError)

        # Switch to DaemonScreen — on_screen_resume should now mirror the
        # crash onto CycleSummaryRow.
        await pilot.press("d")
        await pilot.pause()
        screen = pilot.app.screen_stack[-1]
        assert isinstance(screen, DaemonScreen)
        row = screen.query_one(CycleSummaryRow)
        assert row._last_error is pilot.app.daemon_last_error


@pytest.mark.asyncio
async def test_tui_log_handler_removed_on_unmount(tmp_path):
    """Re-instantiating the App must not pile up handlers on the root logger."""
    import logging
    from pm_agent.tui import PMAgentTUI, TUILogHandler

    async with PMAgentTUI(repo=tmp_path).run_test():
        pass  # app mounted then unmounted

    root = logging.getLogger()
    handlers = [h for h in root.handlers if isinstance(h, TUILogHandler)]
    assert handlers == []
