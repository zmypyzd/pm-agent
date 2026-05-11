#!/usr/bin/env python3
"""BUG-047: validate_disjoint allows two tasks with identical IDs as long
as their allowed_paths don't overlap.

Severity: High
Code: pm_agent/planner.py:180-194

Demo: feed YAML with two id: T-1 tasks (disjoint paths). parse_tasks
accepts them; validate_disjoint accepts them. Downstream
WorktreeManager.create("T-1") gets called twice concurrently.
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
      do A
    allowed_paths: [a.py]
    acceptance: [ok]
  - id: T-1
    title: B
    prompt: |
      do B
    allowed_paths: [b.py]
    acceptance: [ok]
"""


def main() -> int:
    # Either parse_tasks OR validate_disjoint must reject duplicate IDs.
    try:
        tasks = parse_tasks(YAML)
    except PlannerError as e:
        return report("BUG-047", reproduced=False,
                      evidence=f"parse_tasks rejected duplicate IDs: {e}")

    ids = [t.id for t in tasks]
    duplicate = len(set(ids)) < len(ids)
    try:
        validate_disjoint(tasks)
        validator_passed = True
        err = ""
    except PlannerError as e:
        validator_passed = False
        err = str(e)

    reproduced = duplicate and validator_passed
    return report("BUG-047", reproduced=reproduced,
                  evidence=f"ids={ids}; validator_passed={validator_passed}; "
                           f"validator_error={err!r}")


if __name__ == "__main__":
    sys.exit(main())
