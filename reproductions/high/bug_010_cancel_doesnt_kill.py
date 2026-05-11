#!/usr/bin/env python3
"""BUG-010: when run_claude_async coroutine is cancelled (e.g. user quits
TUI), the spawned `claude` subprocess is NOT terminated — only awaited.

Severity: High
Code: pm_agent/runner.py:118-127

Demo: install a `hang` shim (sleeps 600s). Start run_claude_async, let
it spawn the child, then cancel the task. After cancellation we check
if any child `claude` process from our shim is still alive.

Reproduced ⇒ the shim child is still alive ≥1s after cancellation.
Fixed ⇒ child terminated within 1s.
"""
from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _harness import claude_shim, prepend_path, report  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pm_agent.runner import run_claude_async  # noqa: E402


def find_shim_pids(shim_dir: Path) -> list[int]:
    """Find any claude subprocess from our shim by matching argv path."""
    try:
        out = subprocess.run(
            ["pgrep", "-fa", str(shim_dir / "claude")],
            capture_output=True, text=True, check=False,
        ).stdout
    except FileNotFoundError:
        # No pgrep — fallback to /proc style (Linux) or ps
        out = subprocess.run(["ps", "-axo", "pid,command"],
                             capture_output=True, text=True, check=False).stdout
        return [int(line.split()[0]) for line in out.splitlines()
                if str(shim_dir / "claude") in line]
    return [int(line.split()[0]) for line in out.splitlines() if line.strip()]


async def cancellable_run() -> None:
    async for _ in run_claude_async("noop", isolate=False, timeout=None):
        pass


async def driver(shim_dir: Path) -> tuple[bool, list[int]]:
    """Spawn the runner, cancel it, then check whether:
      (a) cancellation completes in bounded time (finally cleanup doesn't deadlock)
      (b) shim child is still alive while loop runs

    Returns (cancel_completed_in_time, alive_pids).
    """
    t = asyncio.create_task(cancellable_run())
    await asyncio.sleep(0.5)  # let claude shim spawn
    t.cancel()
    cancel_clean = True
    try:
        # If the bug exists, finally `await proc.wait()` blocks here forever.
        # 5s is generous: a clean fix has terminate→2s→kill→2s budget = 4s.
        await asyncio.wait_for(t, timeout=5.0)
    except asyncio.CancelledError:
        pass
    except asyncio.TimeoutError:
        cancel_clean = False  # cancellation itself deadlocked

    alive = find_shim_pids(shim_dir)
    return cancel_clean, alive


def main() -> int:
    with claude_shim(behavior="hang") as shim_dir:
        with prepend_path(shim_dir):
            try:
                cancel_clean, alive = asyncio.run(
                    asyncio.wait_for(driver(shim_dir), timeout=10.0)
                )
            except asyncio.TimeoutError:
                cancel_clean, alive = False, find_shim_pids(shim_dir)

            for pid in alive:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass

    reproduced = (not cancel_clean) or bool(alive)
    evidence = (f"cancel_completed_cleanly={cancel_clean}; "
                f"shim_child_alive_pids={alive}")
    return report("BUG-010", reproduced=reproduced, evidence=evidence)


if __name__ == "__main__":
    sys.exit(main())
