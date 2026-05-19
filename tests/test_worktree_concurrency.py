"""Regression tests for WorktreeManager concurrency.

Bug 1 (root cause of the "ai/T-2 - 不是可以合并的东西" failure):
WorktreeManager._acquire_lock used a non-atomic guard (`if self._lock_fh is
not None: return`). When two coders called acreate() concurrently via
asyncio.to_thread, both worker threads passed the guard, both opened the
lock file, and the second flock(LOCK_EX|LOCK_NB) raised BlockingIOError —
even though both calls came from the same WorktreeManager instance and
the lock is only meant to serialize across processes.

Result: the losing task's branch was never created, and integration later
failed with "not something we can merge" because git could not find the
ref it was asked to merge.
"""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from pm_agent.worktree import WorktreeManager


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "pm@local"], cwd=path, check=True
    )
    subprocess.run(
        ["git", "config", "user.name", "pm"], cwd=path, check=True
    )
    (path / "x").write_text("init\n")
    subprocess.run(["git", "add", "x"], cwd=path, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "init"], cwd=path, check=True
    )


@pytest.mark.asyncio
async def test_concurrent_acreate_both_succeed(tmp_path):
    """Two concurrent acreate() calls from the same WorktreeManager must
    both succeed and produce both branches. Pre-fix, one of them raised
    WorktreeError("another pm-agent instance is using ...")."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    wm = WorktreeManager(repo)

    results = await asyncio.gather(
        wm.acreate("T-1"),
        wm.acreate("T-2"),
        return_exceptions=True,
    )

    # Neither call may raise. Pre-fix, one of them was a WorktreeError.
    for tid, r in zip(["T-1", "T-2"], results):
        assert not isinstance(r, Exception), (
            f"acreate({tid!r}) raised {type(r).__name__}: {r}"
        )

    # Both branches must actually exist in the repo so a later integration
    # merge can find them.
    out = subprocess.run(
        ["git", "branch", "--list", "ai/T-1", "ai/T-2"],
        cwd=repo, capture_output=True, text=True,
    ).stdout
    assert "ai/T-1" in out and "ai/T-2" in out, (
        f"both ai/T-1 and ai/T-2 must exist; got:\n{out}"
    )
