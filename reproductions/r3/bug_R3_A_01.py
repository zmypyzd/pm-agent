#!/usr/bin/env python3
"""R3-A-01: --interval-s 0 accepted silently → daemon hot-loops API.

`pm-agent loop run --interval-s 0` was accepted by argparse because the
flag used `type=int` with no validation. interval_s=0 makes the daemon's
`await asyncio.wait_for(stop_event.wait(), timeout=0)` return immediately
on every cycle, so it hammers the Anthropic/GitHub APIs as fast as it can
until the cost budget runs out.

Fix: argparse `type=` validator that rejects values <60 with
ArgumentTypeError.

Exit 0 = REPRODUCED (pre-fix: argparse accepts the value silently).
Exit 1 = NOT REPRODUCED (post-fix: argparse exits 2 with "must be").
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _harness import report  # noqa: E402


def main() -> int:
    project_root = Path(__file__).resolve().parents[2]
    # --help would print after the unparsed value if argparse accepted the
    # bad value; with the validator argparse exits 2 before reaching --help.
    result = subprocess.run(
        [sys.executable, "-m", "pm_agent.cli", "loop", "run",
         "--interval-s", "0", "--help"],
        cwd=project_root,
        capture_output=True,
        text=True,
        timeout=15,
    )
    reproduced = (
        result.returncode == 0
        and "must be" not in result.stderr.lower()
    )
    return report(
        "R3-A-01",
        reproduced=reproduced,
        evidence=(
            f"rc={result.returncode}, "
            f"stderr[:200]={result.stderr[:200]!r}"
        ),
    )


if __name__ == "__main__":
    sys.exit(main())
