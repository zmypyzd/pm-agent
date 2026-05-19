"""Bug 4 regression: every _log() call must also append a markup-stripped
line to ~/.pm-agent/runs/<run_id>/live.log so failures can be diagnosed
after the TUI window is gone (which is what blocked the original RAG-goal
post-mortem)."""
from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_log_persists_to_live_log_file(tmp_path, tmp_pm_agent_home):
    """_log() must create live.log under the run's artifacts dir, strip
    rich markup, and include the HH:MM:SS timestamp."""
    from pm_agent.tui import PMAgentTUI

    async with PMAgentTUI(repo=tmp_path).run_test() as pilot:
        await pilot.pause()
        screen = pilot.app.screen_stack[-1]
        screen._log("[red]integrator crashed: missing branch[/]")
        screen._log("[green]✓ T-1 done[/]")
        await pilot.pause()

        log_path = screen._artifacts_dir / "live.log"
        assert log_path.exists(), f"expected {log_path} to exist"
        content = log_path.read_text(encoding="utf-8")

        # Markup must be stripped — file is for human eyes, not Rich.
        assert "[red]" not in content
        assert "[/]" not in content

        # Both messages and the timestamp prefix must be present.
        assert "integrator crashed: missing branch" in content
        assert "✓ T-1 done" in content
        # Timestamp format HH:MM:SS at start of each line.
        first_line = content.splitlines()[0]
        ts_prefix = first_line.split(" ", 1)[0]
        assert len(ts_prefix) == 8 and ts_prefix[2] == ":" and ts_prefix[5] == ":", (
            f"expected HH:MM:SS prefix, got {ts_prefix!r}"
        )
