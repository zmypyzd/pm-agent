#!/usr/bin/env python3
"""BUG-049: run_claude (sync version) has the same un-drained stderr PIPE
pattern as run_claude_async → identical deadlock if claude writes lots
of stderr.

Severity: High
Code: pm_agent/runner.py:146-184

Demo: same stderr_flood shim as BUG-001, but driving the sync
run_claude function from a child process. Outer wall-clock catches the
deadlock.
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


def _drive_sync_runner(shim_dir_str: str) -> None:
    os.setpgrp()
    os.environ["PATH"] = f"{shim_dir_str}:{os.environ.get('PATH','')}"
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from pm_agent.runner import run_claude
    # No timeout on the sync API; only the outer process group bound saves us.
    run_claude("noop", isolate=False)


def main() -> int:
    payload = "X" * 256  # 1 MB total stderr → overflows pipe ×16
    with claude_shim(behavior="stderr_flood", stderr_payload=payload) as shim_dir:
        with prepend_path(shim_dir):
            ctx = mp.get_context("fork")
            proc = ctx.Process(target=_drive_sync_runner, args=(str(shim_dir),))
            t0 = time.time()
            proc.start()
            proc.join(timeout=6.0)
            elapsed = time.time() - t0
            hung = proc.is_alive()
            if hung:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, OSError):
                    pass
                proc.join(timeout=2.0)

    if hung:
        return report("BUG-049", reproduced=True,
                      evidence=f"sync run_claude did not return within 6s "
                               f"(elapsed={elapsed:.1f}s) — stderr deadlock")
    return report("BUG-049", reproduced=False,
                  evidence=f"sync run_claude returned in {elapsed:.1f}s")


if __name__ == "__main__":
    sys.exit(main())
