#!/usr/bin/env python3
"""BUG-024: demo-commands.sh uses /tmp/pm-agent-day7-target but tui.py default
is /tmp/pm-agent-target. A user copying from QUICKSTART without --repo
hits an unseeded repo.

Severity: Medium
Code: tui.py:1117 vs docs/demo-commands.sh:11
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _harness import report, PROJECT_ROOT  # noqa: E402


def main() -> int:
    tui = (PROJECT_ROOT / "pm_agent" / "tui.py").read_text()
    demo = (PROJECT_ROOT / "docs" / "demo-commands.sh").read_text()

    tui_default = "/tmp/pm-agent-target" in tui
    demo_target = "/tmp/pm-agent-day7-target" in demo

    reproduced = tui_default and demo_target
    return report("BUG-024", reproduced=reproduced,
                  evidence=f"tui default '/tmp/pm-agent-target' present={tui_default}; "
                           f"demo target '/tmp/pm-agent-day7-target' present={demo_target}")


if __name__ == "__main__":
    sys.exit(main())
