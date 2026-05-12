#!/usr/bin/env python3
"""R3-C-04: cmd_loop_run always returns 130, even on graceful shutdown.

The pre-fix code:

    try:
        asyncio.run(run_forever(args.repo, cfg))
    except KeyboardInterrupt:
        print("interrupted", ...); return 130
    print("interrupted", ...); return 130   # <-- bug: clean exit also 130

Even when run_forever returns normally (signal handler set stop_event,
loop drained cleanly), cmd_loop_run prints "interrupted" and returns 130.
Downstream supervisors (systemd, GitHub Actions) treat 130 as failure and
restart the daemon → infinite restart loop.

Fix: only return 130 from the KeyboardInterrupt branch; return 0 on clean
exit.

Exit 0 = REPRODUCED (clean run_forever returns rc=130).
Exit 1 = NOT REPRODUCED (clean run_forever returns rc=0).
"""
from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _harness import report  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pm_agent import cli  # noqa: E402


async def _clean_run_forever(repo: Path, cfg: object) -> None:
    """Simulates daemon exiting cleanly (signal-driven, drained loop)."""
    return None


def main() -> int:
    fake_loop_module = types.SimpleNamespace(
        run_forever=_clean_run_forever,
        LoopConfig=lambda **kw: object(),
    )

    args = types.SimpleNamespace(
        repo=Path.cwd(),
        interval_s=1800,
        max_retries=2,
        coder_timeout=180.0,
        test_timeout=120.0,
    )

    with patch.dict(sys.modules, {"pm_agent.loop": fake_loop_module}):
        rc = cli.cmd_loop_run(args)

    reproduced = rc == 130
    return report(
        "R3-C-04",
        reproduced=reproduced,
        evidence=f"clean shutdown returned rc={rc}; expected 0, got 130 pre-fix",
    )


if __name__ == "__main__":
    sys.exit(main())
