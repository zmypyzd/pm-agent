#!/usr/bin/env python3
"""BUG-017: planner.plan max_retries default is 2 and TUI never overrides;
no CLI flag exists.

Severity: Low
Code: pm_agent/planner.py:225 / pm_agent/tui.py argparse
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _harness import report, PROJECT_ROOT  # noqa: E402


def main() -> int:
    planner = (PROJECT_ROOT / "pm_agent" / "planner.py").read_text()
    tui = (PROJECT_ROOT / "pm_agent" / "tui.py").read_text()

    hardcoded = "max_retries: int = 2" in planner
    no_flag = "--max-retries" not in tui
    not_passed = bool(re.search(r"await plan\([^)]*\)", tui))  # call without max_retries kwarg
    not_passed = not_passed and "max_retries=" not in (re.search(r"await plan\([^)]*\)", tui).group(0)
                                                       if re.search(r"await plan\([^)]*\)", tui) else "")

    reproduced = hardcoded and no_flag and not_passed
    return report("BUG-017", reproduced=reproduced,
                  evidence=f"planner default 2={hardcoded}; "
                           f"cli flag missing={no_flag}; "
                           f"tui doesn't pass kwarg={not_passed}")


if __name__ == "__main__":
    sys.exit(main())
