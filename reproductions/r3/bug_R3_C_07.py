#!/usr/bin/env python3
"""R3-C-07: --coder-timeout and --max-retries accept negative values.

Same root cause as R3-A-01: argparse `type=int` / `type=float` validates
the type but not the range. `--coder-timeout -5` would make every coder
call time out instantly. `--max-retries -3` would skip every retry and
also confuse downstream `if attempts < max_retries` logic.

Fix: argparse `type=` validators that reject values out of range.

Exit 0 = REPRODUCED (pre-fix: at least one negative value accepted).
Exit 1 = NOT REPRODUCED (post-fix: both rejected with rc=2).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _harness import report  # noqa: E402


def _run(*flags: str) -> subprocess.CompletedProcess[str]:
    project_root = Path(__file__).resolve().parents[2]
    return subprocess.run(
        [sys.executable, "-m", "pm_agent.cli", "loop", "run", *flags, "--help"],
        cwd=project_root,
        capture_output=True,
        text=True,
        timeout=15,
    )


def main() -> int:
    a = _run("--coder-timeout", "-5")
    b = _run("--max-retries", "-3")
    coder_accepted = a.returncode == 0 and "must be" not in a.stderr.lower()
    retries_accepted = b.returncode == 0 and "must be" not in b.stderr.lower()
    reproduced = coder_accepted or retries_accepted
    return report(
        "R3-C-07",
        reproduced=reproduced,
        evidence=(
            f"coder-timeout=-5 rc={a.returncode} stderr={a.stderr[:120]!r}; "
            f"max-retries=-3 rc={b.returncode} stderr={b.stderr[:120]!r}"
        ),
    )


if __name__ == "__main__":
    sys.exit(main())
