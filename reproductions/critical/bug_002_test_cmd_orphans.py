#!/usr/bin/env python3
"""BUG-002: test_cmd uses shell=True without process-group kill → orphans.

Severity: Critical
Code: pm_agent/worktree.py:183-205

Demo: drive WorktreeManager.integrate() with a test_cmd that backgrounds
a `sleep 600` via `&`, then has the foreground succeed. After integrate()
returns we check whether the backgrounded sleep is still alive — it
should NOT be (clean exit kills shell child tree), but the same pattern
under TimeoutExpired path will leak. We test both:
  case A: test_cmd that backgrounds then exits quickly (PASS path)
  case B: test_cmd that backgrounds then sleeps longer than test_timeout
          (TimeoutExpired path) → leak guaranteed.

Reproduced ⇒ in case B, the backgrounded child still exists after timeout.
Fixed ⇒ child was killed (process-group cleanup).
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _harness import temp_git_repo, report  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pm_agent.worktree import WorktreeManager  # noqa: E402


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def main() -> int:
    pidfile = Path(tempfile.mkstemp(prefix="bug002-pid-")[1])

    test_cmd = (
        f'(sleep 30 & echo $! > {pidfile}); sleep 60; echo NEVER_REACHED'
    )

    with temp_git_repo(initial_files={"a.txt": "x"}) as repo:
        wm = WorktreeManager(repo)
        # Need at least one task branch to merge so the test path runs.
        wm.create("T-1")
        # Make a trivial commit on the task branch so a merge produces something.
        wt = repo / ".pm-agent-worktrees" / "T-1"
        (wt / "b.txt").write_text("y")
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "x", "GIT_AUTHOR_EMAIL": "x@x",
            "GIT_COMMITTER_NAME": "x", "GIT_COMMITTER_EMAIL": "x@x",
        }
        subprocess.run(["git", "add", "."], cwd=wt, check=True, env=env)
        subprocess.run(["git", "commit", "-q", "-m", "t1"], cwd=wt, check=True, env=env)
        wm.cleanup_worktree("T-1")

        result = wm.integrate(
            run_id="bug002",
            task_ids=["T-1"],
            test_cmd=test_cmd,
            test_timeout=2.0,  # forces TimeoutExpired path
        )

    # Inspect pidfile written by background sleep.
    try:
        pid = int(pidfile.read_text().strip())
    except Exception as e:
        return report("BUG-002", reproduced=False,
                      evidence=f"could not read pidfile: {e}")
    finally:
        try:
            pidfile.unlink()
        except OSError:
            pass

    time.sleep(0.5)  # give kernel a beat to reap if cleanup happened
    alive = pid_alive(pid)
    # Clean up the orphan so the host machine isn't leaking.
    if alive:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass

    if alive:
        return report("BUG-002", reproduced=True,
                      evidence=f"backgrounded sleep PID {pid} survived after "
                               f"test_cmd timeout — orphan leak confirmed")
    return report("BUG-002", reproduced=False,
                  evidence="backgrounded sleep was killed — process-group cleanup ok")


if __name__ == "__main__":
    sys.exit(main())
