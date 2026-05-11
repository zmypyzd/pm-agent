#!/usr/bin/env python3
"""BUG-005: validate_disjoint only does exact-string match, glob/prefix
overlap is not detected.

Severity: High
Code: pm_agent/planner.py:180-194

Demo: two tasks with overlapping path globs (`src/**` vs `src/auth/**`)
clearly should collide. Feed them through parse_tasks + validate_disjoint
and assert no PlannerError is raised.

Reproduced ⇒ validation passed for overlapping globs.
Fixed ⇒ validator now rejects.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _harness import report  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pm_agent.planner import parse_tasks, validate_disjoint, PlannerError  # noqa: E402


YAML = """
tasks:
  - id: T-1
    title: Touch all of src
    prompt: |
      edit anything in src
    allowed_paths:
      - "src/**"
    acceptance:
      - works
  - id: T-2
    title: Touch only src/auth
    prompt: |
      edit auth
    allowed_paths:
      - "src/auth/**"
    acceptance:
      - works
"""


def main() -> int:
    tasks = parse_tasks(YAML)
    try:
        validate_disjoint(tasks)
    except PlannerError as e:
        return report("BUG-005", reproduced=False,
                      evidence=f"validator rejected overlapping globs: {e}")
    return report("BUG-005", reproduced=True,
                  evidence="validator accepted overlapping globs "
                           "src/** ⊃ src/auth/** — no PlannerError raised")


if __name__ == "__main__":
    sys.exit(main())
