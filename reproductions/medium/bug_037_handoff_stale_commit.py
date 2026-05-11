#!/usr/bin/env python3
"""BUG-037: HANDOFF.md §0 references last commit 4268a36, but git log shows
3 commits since. Doc-drift breaks the "verify environment" step new
sessions are told to run.

Severity: Medium
Code: HANDOFF.md:12-13
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _harness import report, PROJECT_ROOT  # noqa: E402


def main() -> int:
    handoff = (PROJECT_ROOT / "HANDOFF.md").read_text()
    m = re.search(r"last commit.*?`([0-9a-f]{7,40})\b", handoff)
    if not m:
        return report("BUG-037", reproduced=False,
                      evidence="HANDOFF.md no longer pins a commit hash")
    pinned = m.group(1)

    head = subprocess.run(
        ["git", "log", "--format=%h", "-n", "1"],
        cwd=PROJECT_ROOT, capture_output=True, text=True, check=True,
    ).stdout.strip()

    distance = subprocess.run(
        ["git", "rev-list", "--count", f"{pinned}..HEAD"],
        cwd=PROJECT_ROOT, capture_output=True, text=True, check=False,
    ).stdout.strip() or "?"

    stale = pinned != head
    return report("BUG-037", reproduced=stale,
                  evidence=f"HANDOFF pins {pinned!r}; HEAD is {head!r}; "
                           f"commits since HANDOFF was written: {distance}")


if __name__ == "__main__":
    sys.exit(main())
