"""Regression harness: every BUGS.md repro under reproductions/ must exit 1
(NOT-REPRODUCED) on a fixed codebase.

Each repro is a standalone script that exits:
  0 → bug still present (REPRODUCED)
  1 → bug fixed (NOT-REPRODUCED)
  other → script crashed (count as failure)

This file parametrizes pytest over all of them so `uv run pytest` is the
single source of truth for "did we regress?".
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REPROS_DIR = PROJECT_ROOT / "reproductions"

REPROS = sorted(REPROS_DIR.rglob("bug_*.py"))
assert REPROS, f"no repro scripts found under {REPROS_DIR}"


@pytest.mark.parametrize(
    "repro",
    REPROS,
    ids=[p.relative_to(REPROS_DIR).as_posix() for p in REPROS],
)
def test_bug_was_fixed(repro: Path) -> None:
    """A bug-* script should exit 1 (NOT-REPRODUCED) on a fixed codebase.

    Failures here mean either:
      - the bug regressed (script saw the failure pattern again), or
      - the repro itself broke (e.g. an outdated assumption about source).
    Either way it's an integration smoke signal worth investigating.
    """
    result = subprocess.run(
        [sys.executable, str(repro)],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode == 1:
        return  # NOT-REPRODUCED, as expected on a fixed codebase
    pytest.fail(
        f"{repro.name} exit={result.returncode}\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr[:2000]}"
    )
