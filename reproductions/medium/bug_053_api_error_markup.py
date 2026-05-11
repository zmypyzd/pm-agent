#!/usr/bin/env python3
"""BUG-053: API error 'reason' text is interpolated into a Rich markup
string without escape, allowing API-side markup to bleed through.

Severity: Medium
Code: pm_agent/tui.py:642

Static check: confirm tui.py line that builds the API error log message
includes `reason` raw (no escape call).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _harness import report, PROJECT_ROOT  # noqa: E402


def main() -> int:
    tui = (PROJECT_ROOT / "pm_agent" / "tui.py").read_text()
    # Match across the implicit string concatenation Python uses.
    # The construct is:
    #   self._log(
    #       f"[red][{task.id}] ✗ API ERROR[/] "
    #       f"reason={str(reason)[:120]}"
    #   )
    pattern = re.search(
        r"API ERROR.*?reason=\{str\(reason\)\[:120\]\}",
        tui, re.DOTALL,
    )
    has_pattern = bool(pattern)
    escapes_reason = "escape(reason" in tui or "escape(str(reason" in tui

    reproduced = has_pattern and not escapes_reason
    return report("BUG-053", reproduced=reproduced,
                  evidence=f"raw reason interpolation found={has_pattern}; "
                           f"markup.escape applied to reason={escapes_reason}")


if __name__ == "__main__":
    sys.exit(main())
