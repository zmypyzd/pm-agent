#!/usr/bin/env python3
"""BUG-019: TUI._cleanup_branches swallows all errors via `except: pass`,
so failed branch deletions accumulate silently.

Severity: Medium
Code: pm_agent/tui.py:476-490

Static demo: scan _cleanup_branches and assert both adelete_branch and
acleanup_integration calls are wrapped in `try: ... except Exception: pass`
without any logging.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _harness import report, PROJECT_ROOT  # noqa: E402


def main() -> int:
    tui = (PROJECT_ROOT / "pm_agent" / "tui.py").read_text()
    body = re.search(r"async def _cleanup_branches\(self\) -> None:(.+?)(?=\n    async def|\n    def)",
                     tui, re.DOTALL)
    if not body:
        return report("BUG-019", reproduced=False, evidence="could not locate _cleanup_branches")
    src = body.group(1)

    # Look for `try: ... except Exception: pass` blocks
    silent_excepts = re.findall(r"except Exception:\s*pass", src)
    has_logging = "_log" in src

    reproduced = len(silent_excepts) >= 2 and not has_logging
    return report("BUG-019", reproduced=reproduced,
                  evidence=f"silent except pass count={len(silent_excepts)}; "
                           f"any logging in body={has_logging}")


if __name__ == "__main__":
    sys.exit(main())
