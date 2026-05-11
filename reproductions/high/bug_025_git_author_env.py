#!/usr/bin/env python3
"""BUG-025: WorktreeManager.integrate uses `git merge` without injecting
git author env, so a clean machine without ~/.gitconfig user.name/email
fails silently.

Severity: High
Code: pm_agent/worktree.py:37-40 (_run helper has no env), 164-178 (merge)

Demo: run `git merge` inside the test repo while explicitly clearing
GIT_* env and user.name/user.email overrides. Show that the merge call
errors with "Please tell me who you are".

We simulate the worst case by spawning a child shell with HOME pointed
at a temp dir (so no ~/.gitconfig) and no GIT_* env, then driving
WorktreeManager.integrate.

Reproduced ⇒ integrate returned with conflicts list populated by an
identity error.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _harness import report  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pm_agent.worktree import WorktreeManager  # noqa: E402


def main() -> int:
    # Build a repo using normal env, then strip identity before driving integrate.
    tmp = Path(tempfile.mkdtemp(prefix="bug025-"))
    fake_home = Path(tempfile.mkdtemp(prefix="bug025-home-"))
    try:
        env_seed = {**os.environ,
                    "GIT_AUTHOR_NAME": "x", "GIT_AUTHOR_EMAIL": "x@x",
                    "GIT_COMMITTER_NAME": "x", "GIT_COMMITTER_EMAIL": "x@x"}
        subprocess.run(["git", "init", "-q", "-b", "master"], cwd=tmp,
                       check=True, env=env_seed)
        (tmp / "a").write_text("1")
        subprocess.run(["git", "add", "."], cwd=tmp, check=True, env=env_seed)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp,
                       check=True, env=env_seed)

        wm = WorktreeManager(tmp)
        wm.create("T-1")
        wt = tmp / ".pm-agent-worktrees" / "T-1"
        (wt / "b").write_text("2")
        subprocess.run(["git", "add", "."], cwd=wt, check=True, env=env_seed)
        subprocess.run(["git", "commit", "-q", "-m", "t1"], cwd=wt,
                       check=True, env=env_seed)
        wm.cleanup_worktree("T-1")

        # Now strip identity. Git on macOS/Linux auto-generates an identity
        # from $USER + hostname when no env / config is set, which would hide
        # the bug. Force git to require explicit identity by writing a
        # GIT_CONFIG_GLOBAL with user.useConfigOnly = true.
        for k in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
                  "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
            os.environ.pop(k, None)
        strict_config = fake_home / ".gitconfig"
        strict_config.write_text("[user]\n    useConfigOnly = true\n")
        os.environ["HOME"] = str(fake_home)
        os.environ["XDG_CONFIG_HOME"] = str(fake_home)
        os.environ["GIT_CONFIG_GLOBAL"] = str(strict_config)
        os.environ["GIT_CONFIG_SYSTEM"] = "/dev/null"
        os.environ["GIT_CONFIG_NOSYSTEM"] = "1"

        result = wm.integrate(run_id="bug025", task_ids=["T-1"], test_cmd=None)

    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(fake_home, ignore_errors=True)

    failed = bool(result.conflicts) or not result.merged_tasks
    msg = (f"merged_tasks={result.merged_tasks} conflicts={len(result.conflicts)}"
           + (f" first_conflict={result.conflicts[0]['output'][:120]!r}"
              if result.conflicts else ""))

    return report("BUG-025", reproduced=failed, evidence=msg)


if __name__ == "__main__":
    sys.exit(main())
