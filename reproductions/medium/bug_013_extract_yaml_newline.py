#!/usr/bin/env python3
"""BUG-013: extract_yaml regex requires `\\n` immediately before the close
fence. claude output without a trailing newline silently falls through to
the raw-text branch and breaks YAML parsing downstream.

Severity: Medium
Code: pm_agent/planner.py:132-140

Demo: feed an extract_yaml input shaped like the regex's expectation but
missing the trailing newline before ```. Verify that the function returns
the WHOLE blob (with backticks) instead of just the YAML body.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _harness import report  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pm_agent.planner import extract_yaml  # noqa: E402


def main() -> int:
    # Missing newline immediately before the closing ```.
    text = "```yaml\ntasks:\n  - id: T-1```"
    out = extract_yaml(text)

    leaked = "```" in out or "yaml" in out.split("\n", 1)[0]
    return report(
        "BUG-013", reproduced=leaked,
        evidence=(f"returned raw blob containing fence markers: {out!r}"
                  if leaked else
                  f"returned clean yaml body: {out!r}"))


if __name__ == "__main__":
    sys.exit(main())
