#!/usr/bin/env python3
"""BUG-015: tui._diff_stats discards the magic value 'ev/null' but the
inline comment says '/dev/null appears for new files' — misleading.

Severity: Low
Code: pm_agent/tui.py:727
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _harness import report, PROJECT_ROOT  # noqa: E402


def main() -> int:
    src = (PROJECT_ROOT / "pm_agent" / "tui.py").read_text()
    has_magic = 'files.discard("ev/null")' in src
    misleading_comment = "/dev/null appears for new files" in src
    reproduced = has_magic and misleading_comment
    return report("BUG-015", reproduced=reproduced,
                  evidence=f"magic 'ev/null' discard={has_magic}; "
                           f"misleading '/dev/null' comment={misleading_comment}")


if __name__ == "__main__":
    sys.exit(main())
