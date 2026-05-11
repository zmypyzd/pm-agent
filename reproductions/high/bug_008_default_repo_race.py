#!/usr/bin/env python3
"""BUG-008: two pm-agent instances on the default /tmp/pm-agent-target repo
race over the same worktree paths and branches.

Severity: High
Code: pm_agent/tui.py:1092-1108, pm_agent/worktree.py:82-97

Demo: create the same WorktreeManager target from two threads
simultaneously, both trying to create a worktree with the same task id.
Show that one wins, the other fails (or both partially succeed leaving
inconsistent state).

Reproduced ⇒ second create() raised WorktreeError OR the first's worktree
directory was wiped by the second's preclean. Fixed ⇒ both runs got their
own isolated worktree path (e.g., via PID suffix or file lock).
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _harness import temp_git_repo, report  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pm_agent.worktree import WorktreeManager, WorktreeError  # noqa: E402


def main() -> int:
    results: list[tuple[str, str]] = []
    lock = threading.Lock()

    with temp_git_repo(initial_files={"a": "1"}) as repo:
        def worker(tag: str) -> None:
            try:
                wm = WorktreeManager(repo)
                path = wm.create("T-1")
                time.sleep(0.3)  # hold the worktree a beat
                exists = path.exists()
                with lock:
                    results.append((tag, f"created path={path.name} exists_after_300ms={exists}"))
                wm.cleanup_worktree("T-1")
            except WorktreeError as e:
                with lock:
                    results.append((tag, f"WorktreeError: {e}"))
            except Exception as e:
                with lock:
                    results.append((tag, f"{type(e).__name__}: {e}"))

        t1 = threading.Thread(target=worker, args=("A",))
        t2 = threading.Thread(target=worker, args=("B",))
        t1.start(); t2.start()
        t1.join(); t2.join()

    a, b = sorted(results)
    def squish(s: str) -> str:
        return s.replace("\n", " ⏎ ")[:200]
    msg = f"A: {squish(a[1])} | B: {squish(b[1])}"

    # Buggy behavior: race manifests as cryptic git error ("cannot lock ref")
    # OR silent worktree wipe (exists_after_300ms=False on the survivor).
    # Fixed behavior: ONE side fails with a CLEAN, identifiable error
    # ("another pm-agent instance is using ...") AND the other side completes
    # with its worktree intact.
    cryptic_error = any("cannot lock ref" in r[1] or "exists_after_300ms=False" in r[1]
                        for r in results)
    clean_refusal = any("another pm-agent instance" in r[1] for r in results)

    # Reproduced ⇒ saw cryptic-race symptoms, or BOTH sides succeeded
    # (both worktrees claim the same path → corruption).
    both_succeeded = all("created path=" in r[1] and "exists_after_300ms=True" in r[1]
                         for r in results)
    reproduced = cryptic_error or both_succeeded or not clean_refusal
    return report("BUG-008", reproduced=reproduced, evidence=msg)


if __name__ == "__main__":
    sys.exit(main())
