#!/usr/bin/env python3
"""BUG-045: _diff_stats treats any line starting with '---' or '+++' as a
file header; this misclassifies real content lines like markdown HRs or
docstring banners that start with those sequences.

Severity: Low
Code: pm_agent/tui.py:712-728

Demo: feed a unified diff containing a removed `---` line (markdown HR)
and an added `+++` line. _diff_stats should report removed/added counts
that include those content lines, but instead skips them via `continue`.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _harness import report  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pm_agent.tui import PMAgentTUI  # noqa: E402

stats = PMAgentTUI._diff_stats


def main() -> int:
    # A diff where the modified file has content lines literally starting with
    # `---` and `+++`. These are CONTENT, not git headers — easy to construct
    # in markdown edits.
    diff = (
        "diff --git a/notes.md b/notes.md\n"
        "--- a/notes.md\n"
        "+++ b/notes.md\n"
        "@@ -1,4 +1,4 @@\n"
        " before\n"
        "----\n"          # removed markdown HR — starts with ---
        "+++ added header\n"  # added line — starts with +++
        " after\n"
    )
    s = stats(diff)
    # Correct counts: 1 removed (the HR), 1 added (the header).
    # Buggy parser: both lines hit startswith("---") / startswith("+++") and
    # are `continue`d, so added=0 removed=0.
    miscount = (s["added"] != 1) or (s["removed"] != 1)
    return report("BUG-045", reproduced=miscount,
                  evidence=f"stats on crafted diff: {s} (expected added=1, removed=1)")


if __name__ == "__main__":
    sys.exit(main())
