#!/usr/bin/env python3
"""BUG-032: spawn-time OSError (ARG_MAX overrun, claude not on PATH, etc.)
is not caught inside run_claude_async — the exception bubbles all the way
up to the TUI's generic except.

Severity: Medium
Code: pm_agent/runner.py:81-86

Demo: install a NON-existent `claude` shim (PATH directory exists but the
claude binary isn't in it). Try to drive run_claude_async; observe whether
it yields a synthetic spawn_error event (fixed) or raises FileNotFoundError
(buggy).
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _harness import prepend_path, report  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pm_agent.runner import run_claude_async  # noqa: E402


async def drive():
    events: list[dict] = []
    raised: Exception | None = None
    try:
        async for ev in run_claude_async("noop", isolate=False, timeout=2.0):
            events.append(ev)
    except Exception as e:  # noqa: BLE001
        raised = e
    return events, raised


def main() -> int:
    # Empty bin dir → claude not on PATH at all.
    empty = Path(tempfile.mkdtemp(prefix="empty-bin-"))
    with prepend_path(empty):
        # Wipe rest of PATH so claude truly not found.
        import os
        old = os.environ["PATH"]
        os.environ["PATH"] = str(empty)
        try:
            events, raised = asyncio.run(drive())
        finally:
            os.environ["PATH"] = old

    has_spawn_error_event = any(
        ev.get("subtype") == "spawn_error" for ev in events
    )
    bubbled = isinstance(raised, (FileNotFoundError, OSError))

    # Reproduced if the OSError bubbles up uncaught.
    # Fixed if a synthetic spawn_error event was yielded instead.
    reproduced = bubbled and not has_spawn_error_event
    return report("BUG-032", reproduced=reproduced,
                  evidence=f"raised={type(raised).__name__ if raised else None}; "
                           f"spawn_error_event={has_spawn_error_event}")


if __name__ == "__main__":
    sys.exit(main())
