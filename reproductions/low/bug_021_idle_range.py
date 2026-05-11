#!/usr/bin/env python3
"""BUG-021: _run_session has `for i in range(len(self._tasks) + 1, 3)` to
idle-out unused Coder cards. With the typical 2-task plan, range(3,3) is
empty — code is effectively dead.

Severity: Low
Code: pm_agent/tui.py:393-396
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _harness import report, PROJECT_ROOT  # noqa: E402


def main() -> int:
    tui = (PROJECT_ROOT / "pm_agent" / "tui.py").read_text()
    has_dead_loop = "for i in range(len(self._tasks) + 1, 3):" in tui
    return report("BUG-021", reproduced=has_dead_loop,
                  evidence=f"hardcoded range(...) + 1, 3) loop present={has_dead_loop} "
                           f"— empty for len(tasks)>=2")


if __name__ == "__main__":
    sys.exit(main())
