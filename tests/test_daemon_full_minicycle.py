"""End-to-end: TUI starts daemon, scanner returns 1 finding, coder is mocked
so no diff is produced (NO_CHANGES path), gh ops are mocked. Assert that at
least one cycle and one finding row appear in SQLite.

The coder path completes as follows:
  scanner → 1 finding → Coder-1 worktree created → run_claude_async yields
  a clean result event → diff is empty (no file was actually changed) →
  finding marked 'failed' (NO_CHANGES) → cycle finishes with status='done'.

That still satisfies the integration contract: cycle + finding rows exist.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from tests._fixtures.fake_repo import fake_repo


@pytest.mark.asyncio
async def test_tui_daemon_full_minicycle(tmp_path, monkeypatch):
    from pm_agent.tui import PMAgentTUI
    from pm_agent import github, persistence, scanner, preflight
    import pm_agent.loop as _loop_mod
    from pm_agent.scanner import Finding
    from pm_agent.preflight import CheckResult
    from pm_agent.loop import LoopConfig

    # ------------------------------------------------------------------ #
    # 0. Redirect HOME and STATE_DB before importing / constructing app   #
    # ------------------------------------------------------------------ #
    state_db = tmp_path / "state.db"
    monkeypatch.setattr(_loop_mod, "STATE_DB", state_db)

    # ------------------------------------------------------------------ #
    # 1. All-pass preflight                                               #
    # ------------------------------------------------------------------ #
    fake_results = [
        CheckResult("state.db dir writable", True, str(tmp_path)),
        CheckResult("state.db clean",        True, "fresh"),
        CheckResult("claude CLI on PATH",    True, "ok"),
        CheckResult("gh CLI authenticated",  True, "ok"),
        CheckResult("repo clean",            True, str(tmp_path)),
        CheckResult("repo on main",          True, "main", warn_only=True),
        CheckResult("/tmp writable",         True, "/tmp"),
    ]
    monkeypatch.setattr(preflight, "run_preflight",
                        lambda db, repo, **kw: (fake_results, True))

    # ------------------------------------------------------------------ #
    # 2. Scanner mock — returns one Low-severity finding                  #
    # ------------------------------------------------------------------ #
    async def fake_scan(repo, *, max_retries=2, **kw):
        return ([Finding(
            bug_id="aaaa1111",
            title="test finding",
            severity="Low",
            paths=["x.py"],
            acceptance=["does nothing"],
            evidence="e",
            kind="bug",
        )], 0.10)

    monkeypatch.setattr(scanner, "scan", fake_scan)

    # ------------------------------------------------------------------ #
    # 3. run_claude_async mock — async generator that yields a clean      #
    #    result event (no error) so _drive_coder returns (True, 0.0).     #
    #    The worktree diff will be empty → finding is marked 'failed'     #
    #    (NO_CHANGES path), but the cycle itself completes with 'done'.   #
    # ------------------------------------------------------------------ #
    async def fake_run_claude_async(prompt, **kw):
        yield {"type": "result", "is_error": False,
               "total_cost_usd": 0.01, "duration_ms": 10}

    # loop.py imports run_claude_async directly at module level; patch
    # it there so _drive_coder picks up the mock.
    monkeypatch.setattr(_loop_mod, "run_claude_async", fake_run_claude_async)

    # ------------------------------------------------------------------ #
    # 4. Gate mock — avoid real pytest/mypy/ruff in fake repo             #
    # ------------------------------------------------------------------ #
    monkeypatch.setattr(_loop_mod, "run_gates", lambda repo: (True, ""))

    # ------------------------------------------------------------------ #
    # 5. gh higher-level mocks                                            #
    # ------------------------------------------------------------------ #
    async def fake_sync_pr_states():
        return []

    async def fake_push_branch(repo, branch):
        return (True, "")

    async def fake_open_pr(branch, finding, body_extras=""):
        return github.PRResult(
            number=1,
            url="https://example.com/pr/1",
            action="opened",
        )

    async def fake_auto_merge(branch, finding):
        return github.PRResult(
            number=2,
            url="https://example.com/pr/2",
            action="merged-now",
        )

    monkeypatch.setattr(github, "sync_pr_states", fake_sync_pr_states)
    monkeypatch.setattr(github, "push_branch_to_origin", fake_push_branch)
    monkeypatch.setattr(github, "open_pr", fake_open_pr)
    monkeypatch.setattr(github, "auto_merge", fake_auto_merge)

    # ------------------------------------------------------------------ #
    # 6. Fake git repo + init DB                                          #
    # ------------------------------------------------------------------ #
    with fake_repo({"x.py": "def x(): return 1\n", "README.md": "x\n"}) as repo:
        persistence.init_db(state_db)

        # ---------------------------------------------------------------- #
        # 7. Construct App with fast interval (overrides default 1800s)    #
        # ---------------------------------------------------------------- #
        loop_cfg = LoopConfig(interval_s=1)
        app = PMAgentTUI(repo=repo, loop_cfg=loop_cfg)

        async with app.run_test(size=(120, 40)) as pilot:
            # 8. Switch to daemon screen
            await pilot.press("d")
            await pilot.pause()

            # 9. Inject pre-computed preflight results so the gate passes
            #    immediately (avoids running the real preflight subprocess).
            pilot.app.preflight_results = fake_results

            # 10. Start the daemon directly (faster than pressing 's')
            screen = pilot.app.screen_stack[-1]
            await screen.action_start_daemon()

            # 11. Poll SQLite until at least 1 cycle reaches a terminal
            #     status, or until we exhaust the retry budget.
            for _ in range(150):
                await asyncio.sleep(0.1)
                try:
                    c = persistence.get_conn()
                    row = c.execute(
                        "SELECT COUNT(*) AS n FROM cycles "
                        "WHERE status IN ('done','scan-empty','aborted','errored')"
                    ).fetchone()
                    if row and row["n"] >= 1:
                        break
                except Exception:
                    pass

            # 12. Stop the daemon gracefully
            await screen.action_stop_daemon()
            for _ in range(60):
                if pilot.app.daemon_task is None:
                    break
                await asyncio.sleep(0.1)

        # ---------------------------------------------------------------- #
        # 13. Assert SQLite state after App has exited                      #
        # ---------------------------------------------------------------- #
        c = persistence.get_conn()
        cycles_n = c.execute("SELECT COUNT(*) FROM cycles").fetchone()[0]
        findings_n = c.execute("SELECT COUNT(*) FROM findings").fetchone()[0]

    assert cycles_n >= 1, f"Expected ≥1 cycle row, got {cycles_n}"
    assert findings_n >= 1, f"Expected ≥1 finding row, got {findings_n}"
