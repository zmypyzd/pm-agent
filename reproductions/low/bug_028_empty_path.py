#!/usr/bin/env python3
"""BUG-028: validate_disjoint accepts empty-string entries in allowed_paths.

Severity: Low
Code: pm_agent/planner.py:188-193
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
    title: A
    prompt: |
      do
    allowed_paths:
      - ""
      - "a.py"
    acceptance:
      - ok
  - id: T-2
    title: B
    prompt: |
      do
    allowed_paths:
      - "b.py"
    acceptance:
      - ok
"""


def main() -> int:
    tasks = parse_tasks(YAML)
    try:
        validate_disjoint(tasks)
        return report("BUG-028", reproduced=True,
                      evidence="empty-string path accepted in allowed_paths")
    except PlannerError as e:
        return report("BUG-028", reproduced=False, evidence=f"rejected: {e}")


if __name__ == "__main__":
    sys.exit(main())
