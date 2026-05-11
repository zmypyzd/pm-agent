#!/usr/bin/env python3
"""BUG-009: AGENTS_INITIAL is hard-coded with Coder-1 and Coder-2 only;
3+ tasks silently lose UI feedback because _set_agent_status swallows
NoMatches exceptions.

Severity: High
Code: pm_agent/tui.py:76-81 (AGENTS_INITIAL), 951-955 (silent except)

Static demo: read tui.py and confirm:
  1. AGENTS_INITIAL has exactly Coder-1 and Coder-2 (no dynamic loop)
  2. _set_agent_status wraps query_one in try/except that returns silently
This combination means any T-3+ task produces no card update.

Reproduced ⇒ both conditions hold.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _harness import report, PROJECT_ROOT  # noqa: E402


def main() -> int:
    tui = (PROJECT_ROOT / "pm_agent" / "tui.py").read_text()

    coders_in_init = re.findall(r'"(Coder-\d+)"', tui)
    initial_block = re.search(r"AGENTS_INITIAL\s*=\s*\[(.+?)\]", tui, re.DOTALL)
    initial_coders = set(re.findall(r'"(Coder-\d+)"', initial_block.group(1) if initial_block else ""))

    # Bug = both (a) only 2 cards AND (b) miss is silently swallowed.
    # Fix can take either form: more cards, OR an audible warning on miss.
    silent_except = bool(re.search(
        r"def _set_agent_status\(.*?query_one\(f\"#agent-\{name\}\".*?except Exception:\s*return\n",
        tui, re.DOTALL))

    has_only_two = initial_coders == {"Coder-1", "Coder-2"}
    reproduced = has_only_two and silent_except
    return report("BUG-009", reproduced=reproduced,
                  evidence=f"AGENTS_INITIAL coders={sorted(initial_coders)}; "
                           f"silent_except_in_set_agent_status={silent_except}")


if __name__ == "__main__":
    sys.exit(main())
