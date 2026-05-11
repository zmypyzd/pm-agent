#!/usr/bin/env python3
"""BUG-022: Reviewer agent card is initialised once and never updated
again — dead UI surface.

Severity: Low
Code: pm_agent/tui.py:80 (initial), 397 ("(needs result — day 7+)")
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _harness import report, PROJECT_ROOT  # noqa: E402


def main() -> int:
    tui = (PROJECT_ROOT / "pm_agent" / "tui.py").read_text()
    # Count update calls for the Reviewer card.
    updates = re.findall(r"_set_agent_status\(\s*\"Reviewer\"", tui)
    in_initial = '"Reviewer"' in tui
    reproduced = in_initial and len(updates) <= 1
    return report("BUG-022", reproduced=reproduced,
                  evidence=f"Reviewer in AGENTS_INITIAL={in_initial}; "
                           f"_set_agent_status('Reviewer', ...) call count={len(updates)} "
                           f"(only initial idle status)")


if __name__ == "__main__":
    sys.exit(main())
