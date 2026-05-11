#!/usr/bin/env python3
"""BUG-035: project has zero unit tests under tests/ or pm_agent/tests/.

Severity: Medium
Code: project root

Verifier: scan the project for any test_*.py / *_test.py outside .venv and
docs/. Demo target tests (in docs/demo-commands.sh heredoc) do not count.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _harness import report, PROJECT_ROOT  # noqa: E402


def main() -> int:
    candidates: list[Path] = []
    for p in PROJECT_ROOT.rglob("test_*.py"):
        if ".venv" in p.parts or "docs" in p.parts:
            continue
        candidates.append(p)
    for p in PROJECT_ROOT.rglob("*_test.py"):
        if ".venv" in p.parts or "docs" in p.parts:
            continue
        candidates.append(p)

    reproduced = len(candidates) == 0
    return report("BUG-035", reproduced=reproduced,
                  evidence=(f"no test files found under {PROJECT_ROOT}"
                            if reproduced else
                            f"found {len(candidates)} test files: "
                            f"{[str(p.relative_to(PROJECT_ROOT)) for p in candidates[:5]]}"))


if __name__ == "__main__":
    sys.exit(main())
