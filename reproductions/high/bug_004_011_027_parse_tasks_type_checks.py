#!/usr/bin/env python3
"""BUG-004 + BUG-011 + BUG-027: parse_tasks coerces id/title/prompt via str()
without isinstance check → None/list/dict become 'None'/'[1, 2]'/'{...}'.

Severity: High (merged)
Code: pm_agent/planner.py:166-174

Demo: feed 4 crafted YAML payloads to parse_tasks; assert that each
produces a CoderTask whose id/title/prompt is a corrupted stringified
non-string value.

Reproduced ⇒ parse_tasks did NOT raise PlannerError; the bad value
silently became e.g. id='None' or id='[1, 2]'.
Fixed ⇒ parse_tasks raised PlannerError for each malformed input.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _harness import report  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pm_agent.planner import parse_tasks, PlannerError  # noqa: E402


CASES = {
    "id=None": """
tasks:
  - id:
    title: A
    prompt: |
      do A
    allowed_paths: [a]
    acceptance: [b]
""",
    "id=list": """
tasks:
  - id: [1, 2]
    title: A
    prompt: |
      do A
    allowed_paths: [a]
    acceptance: [b]
""",
    "title=None": """
tasks:
  - id: T-1
    title:
    prompt: |
      do A
    allowed_paths: [a]
    acceptance: [b]
""",
    "prompt=int": """
tasks:
  - id: T-1
    title: A
    prompt: 42
    allowed_paths: [a]
    acceptance: [b]
""",
}


def main() -> int:
    silently_accepted: list[str] = []
    for label, yaml_text in CASES.items():
        try:
            tasks = parse_tasks(yaml_text)
        except PlannerError:
            continue  # Good: validator rejected it.
        # Bad: silently accepted with stringified garbage.
        t = tasks[0]
        bad = []
        if not isinstance(getattr(t, "id", None), str) or t.id in {"None"} or t.id.startswith("["):
            bad.append(f"id={t.id!r}")
        if t.title in {"None"} or t.title == "":
            bad.append(f"title={t.title!r}")
        if t.prompt in {"None", "42"}:
            bad.append(f"prompt={t.prompt!r}")
        silently_accepted.append(f"{label} → {', '.join(bad) or '(no obvious corruption)'}")

    if silently_accepted:
        return report("BUG-004/011/027", reproduced=True,
                      evidence="; ".join(silently_accepted))
    return report("BUG-004/011/027", reproduced=False,
                  evidence="all malformed inputs raised PlannerError")


if __name__ == "__main__":
    sys.exit(main())
