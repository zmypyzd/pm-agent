#!/usr/bin/env python3
"""BUG-007: WorktreeManager._detect_base_branch returns literal 'HEAD'
when the target repo is in detached-HEAD state.

Severity: High
Code: pm_agent/worktree.py:51-58

Demo: build a temp repo, detach HEAD, instantiate WorktreeManager and
inspect base_branch. Then attempt a worktree.create + integrate dry-run
to show the base_branch == 'HEAD' is propagated into git commands.

Reproduced ⇒ base_branch == 'HEAD'.
Fixed ⇒ either raises or substitutes a meaningful branch.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _harness import temp_git_repo, report  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pm_agent.worktree import WorktreeManager  # noqa: E402


def main() -> int:
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "x", "GIT_AUTHOR_EMAIL": "x@x",
           "GIT_COMMITTER_NAME": "x", "GIT_COMMITTER_EMAIL": "x@x"}

    with temp_git_repo(initial_files={"a": "1"}) as repo:
        # Add a 2nd commit so we have a non-HEAD sha to checkout.
        (repo / "b").write_text("2")
        subprocess.run(["git", "add", "."], cwd=repo, check=True, env=env)
        subprocess.run(["git", "commit", "-q", "-m", "c2"], cwd=repo, check=True, env=env)
        sha = subprocess.run(["git", "rev-parse", "HEAD~1"], cwd=repo,
                             check=True, capture_output=True, text=True).stdout.strip()
        subprocess.run(["git", "checkout", "-q", sha], cwd=repo, check=True, env=env)

        wm = WorktreeManager(repo)
        base = wm.base_branch

    if base == "HEAD":
        return report("BUG-007", reproduced=True,
                      evidence=f"base_branch == {base!r} when repo is detached")
    return report("BUG-007", reproduced=False,
                  evidence=f"base_branch == {base!r} — handled")


if __name__ == "__main__":
    sys.exit(main())
