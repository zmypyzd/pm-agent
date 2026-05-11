#!/usr/bin/env python3
"""BUG-001: run_claude_async stderr PIPE never drained → child blocks on write.

Severity: Critical
Code: pm_agent/runner.py:81-127

Demo: install a fake `claude` that writes 1 MB to stderr (filling the pipe
many times over) then sleeps. With the buggy runner, the child blocks on
write(stderr) because the parent never reads it; subsequently
proc.wait() in the runner's finally hangs forever. We run the call in a
child process so we can detect the hang via an outer wall clock without
poisoning our own asyncio loop.

Reproduced ⇒ the child process did not finish within 6 s (deadlock).
Fixed ⇒ child returned promptly because stderr was drained or set to
DEVNULL.
"""
from __future__ import annotations

import multiprocessing as mp
import os
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _harness import claude_shim, prepend_path, report  # noqa: E402


def _drive_runner(shim_dir_str: str) -> None:
    """Runs in a child process: import the runner and try to iterate it.
    Will hang inside run_claude_async's finally → proc.wait() if the bug
    is present. We detach into our own process group so the test harness
    can kill us (and the shim grandchild) without taking out the parent.
    """
    import asyncio
    os.setpgrp()  # new process group leader
    os.environ["PATH"] = f"{shim_dir_str}:{os.environ.get('PATH','')}"
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from pm_agent.runner import run_claude_async

    async def consume():
        async for _ in run_claude_async("noop", isolate=False, timeout=None):
            pass

    asyncio.run(consume())


def main() -> int:
    payload = "X" * 256  # 256 B × 4096 = 1 MB written by shim → fills 64 KB pipe
    with claude_shim(behavior="stderr_flood", stderr_payload=payload) as shim_dir:
        with prepend_path(shim_dir):
            ctx = mp.get_context("fork")
            proc = ctx.Process(target=_drive_runner, args=(str(shim_dir),))
            t0 = time.time()
            proc.start()
            proc.join(timeout=6.0)
            elapsed = time.time() - t0
            hung = proc.is_alive()
            if hung:
                # Kill the deadlocked child's own process group (it called
                # setpgrp so its pgid == its pid). This wipes the shim
                # grandchild too without touching our own group.
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, OSError):
                    pass
                proc.join(timeout=2.0)

    if hung:
        return report("BUG-001", reproduced=True,
                      evidence=f"runner subprocess did not return within 6s "
                               f"(elapsed={elapsed:.1f}s) — stderr deadlock")
    return report("BUG-001", reproduced=False,
                  evidence=f"runner subprocess returned in {elapsed:.1f}s "
                           f"(≤ 6s budget)")


if __name__ == "__main__":
    sys.exit(main())
