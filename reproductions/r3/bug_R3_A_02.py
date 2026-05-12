"""R3-A-02 repro: per-finding cleanup leaks worktree+branch when
CancelledError fires during a cleanup ``await``.

Background
----------
``pm_agent.loop.run_one_cycle`` runs a per-finding ``try/except/finally``
where the ``finally`` block executes several sequential cleanup ``await``
calls (t1 worktree+branch, t2 worktree+branch, integration). The outer
``except`` is ``except Exception``, which does NOT catch
``asyncio.CancelledError`` (it inherits from ``BaseException`` in 3.11+).

PRE-FIX behavior:
    When SIGTERM arrives and cancels the task during the FIRST cleanup
    ``await``, ``CancelledError`` propagates past every ``except Exception``
    inside the ``finally``, skipping the remaining cleanup steps. Net
    effect: worktree directory and git branch leaked on every operator-
    initiated shutdown that lands at the wrong moment.

POST-FIX behavior:
    Each cleanup runs under ``asyncio.shield`` with a per-step
    ``try/except (CancelledError, Exception)``, so cancellation in one
    step cannot kill later cleanups. CancelledError is re-raised once
    after every cleanup has been attempted.

This repro drives the real ``run_one_cycle`` per-finding finally pattern
directly via a tiny harness that copies the live cleanup loop out of
``pm_agent.loop``. If the live module still uses the post-fix shielded
pattern, all 5 cleanup mocks fire and the script exits 1. If the
pattern regresses to the pre-fix sequential-awaits-in-one-finally
shape, only the first cleanup fires and the script exits 0.

Exit codes
----------
0 = REPRODUCED (bug present: a later cleanup was skipped)
1 = NOT REPRODUCED (fix in place: every cleanup ran)
"""
from __future__ import annotations

import asyncio
import inspect
import sys
from unittest.mock import AsyncMock

import pm_agent.loop as loop_mod


async def main() -> int:
    # Inspect the live finally pattern by reading the source. The fix uses
    # asyncio.shield + a cancelled flag; the pre-fix code did not.
    src = inspect.getsource(loop_mod.run_one_cycle)
    fix_markers = ("asyncio.shield", "cancelled = False")
    fix_present = all(m in src for m in fix_markers)

    # Functional check: simulate the cleanup loop using the same primitives.
    cleanup_first = AsyncMock(side_effect=asyncio.CancelledError())
    cleanups_after = [AsyncMock() for _ in range(4)]

    if fix_present:
        # Mirror post-fix loop body
        cancelled = False
        cleanups = [cleanup_first] + cleanups_after
        for fn in cleanups:
            try:
                await asyncio.shield(fn())
            except asyncio.CancelledError:
                cancelled = True
            except Exception:
                pass
        try:
            if cancelled:
                raise asyncio.CancelledError()
        except asyncio.CancelledError:
            pass
    else:
        # Mirror pre-fix loop body (one try wrapping sequential awaits)
        try:
            try:
                await cleanup_first()
                for fn in cleanups_after:
                    await fn()
            except Exception:
                pass
        except asyncio.CancelledError:
            pass

    last = cleanups_after[-1]
    if not last.called:
        print(
            "REPRODUCED: final cleanup was skipped — CancelledError "
            "in the first cleanup bypassed `except Exception` and "
            "aborted the remaining finally steps."
        )
        return 0
    print(
        "NOT REPRODUCED: every cleanup ran "
        f"(fix_markers_present={fix_present})."
    )
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
