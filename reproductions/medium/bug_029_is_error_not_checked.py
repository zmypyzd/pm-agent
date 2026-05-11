#!/usr/bin/env python3
"""BUG-029: _call_planner_once does not check result.is_error; an API-side
error stream (no assistant text, is_error=True) returns empty text, the
retry loop treats it as a parse error, and exhausts retries on API
problems.

Severity: Medium
Code: pm_agent/planner.py:213-220

Static demo: scan _call_planner_once and assert that:
  1. it handles `et == 'result'` and reads total_cost_usd
  2. it does NOT branch on `is_error`
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _harness import report, PROJECT_ROOT  # noqa: E402


def main() -> int:
    planner = (PROJECT_ROOT / "pm_agent" / "planner.py").read_text()
    body = re.search(r"async def _call_planner_once\(.*?\) -> tuple\[str, float\]:(.+?)(?=\n\nasync def|\Z)",
                     planner, re.DOTALL)
    if not body:
        return report("BUG-029", reproduced=False, evidence="could not locate _call_planner_once")
    src = body.group(1)

    handles_result = "result" in src and "total_cost_usd" in src
    checks_is_error = "is_error" in src

    reproduced = handles_result and not checks_is_error
    return report("BUG-029", reproduced=reproduced,
                  evidence=f"handles_result={handles_result}; checks_is_error={checks_is_error}")


if __name__ == "__main__":
    sys.exit(main())
