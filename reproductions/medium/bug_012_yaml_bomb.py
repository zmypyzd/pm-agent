#!/usr/bin/env python3
"""BUG-012: parse_tasks has no size or depth limit on yaml.safe_load input.

Severity: Medium
Code: pm_agent/planner.py:144-155

Demo: feed a large but syntactically valid YAML to parse_tasks and time
how long it takes. yaml.safe_load is size-unbounded by default — the call
will accept multi-megabyte payloads happily.

Reproduced ⇒ parse_tasks accepted the giant payload.
Fixed ⇒ rejected via an explicit length check.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _harness import report  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pm_agent.planner import parse_tasks, PlannerError  # noqa: E402


def main() -> int:
    # Build a syntactically valid YAML whose acceptance list is huge.
    accepts = "      - filler item number {}\n"
    big_block = "".join(accepts.format(i) for i in range(50_000))  # ~1.7 MB
    yaml_text = (
        "tasks:\n"
        "  - id: T-1\n"
        "    title: A\n"
        "    prompt: |\n"
        "      do A\n"
        "    allowed_paths:\n"
        "      - a.py\n"
        "    acceptance:\n"
        f"{big_block}"
        "  - id: T-2\n"
        "    title: B\n"
        "    prompt: |\n"
        "      do B\n"
        "    allowed_paths:\n"
        "      - b.py\n"
        "    acceptance:\n"
        "      - ok\n"
    )

    t0 = time.time()
    try:
        tasks = parse_tasks(yaml_text)
        elapsed = time.time() - t0
        size_mb = len(yaml_text) / 1024 / 1024
        return report(
            "BUG-012", reproduced=True,
            evidence=f"parse_tasks accepted {size_mb:.1f} MB input "
                     f"({len(tasks[0].acceptance):,} acceptance items) in {elapsed:.1f}s — no size guard")
    except PlannerError as e:
        elapsed = time.time() - t0
        msg = str(e)[:120]
        return report(
            "BUG-012", reproduced=False,
            evidence=f"rejected after {elapsed:.1f}s: {msg}")


if __name__ == "__main__":
    sys.exit(main())
