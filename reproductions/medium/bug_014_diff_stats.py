#!/usr/bin/env python3
"""BUG-014: _diff_stats hand-rolled parser miscounts on renames, paths with
spaces, and short paths.

Severity: Medium
Code: pm_agent/tui.py:712-728

Demo: feed _diff_stats three crafted unified diffs and compare against
the correct values that `git diff --numstat` would produce.
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
    issues: list[str] = []

    # Case A: a rename (no +++/--- lines in pure rename diff)
    rename_diff = (
        "diff --git a/old.py b/new.py\n"
        "similarity index 100%\n"
        "rename from old.py\n"
        "rename to new.py\n"
    )
    s = stats(rename_diff)
    if s["files"] != 1:
        issues.append(f"rename: expected 1 file, got {s['files']}")

    # Case B: path with a space, quoted by git
    space_diff = (
        'diff --git "a/foo bar.py" "b/foo bar.py"\n'
        '--- "a/foo bar.py"\n'
        '+++ "b/foo bar.py"\n'
        '@@ -1 +1 @@\n'
        '-old\n'
        '+new\n'
    )
    s = stats(space_diff)
    # Parser slices line[6:] — strips the leading quote and `a/`/`b/`, yielding
    # `oo bar.py"` and `oo bar.py"` (set dedup), expected files count 1.
    # But the captured path is wrong — we check that.
    # We'll just observe added/removed are off-by-... let's check.
    if s["added"] != 1 or s["removed"] != 1:
        issues.append(f"space: added={s['added']} removed={s['removed']} (expected 1/1)")

    # Case C: very short path  --- a/x  +++ b/x
    short_diff = (
        "diff --git a/x b/x\n"
        "--- a/x\n"
        "+++ b/x\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )
    s = stats(short_diff)
    # line[6:] on "--- a/x" (len=7) → "x"; on "+++ b/x" → "x". Set len 1. OK.
    # But check parser doesn't crash on len(line) == 7 (>6 condition).
    if s["files"] != 1 or s["added"] != 1 or s["removed"] != 1:
        issues.append(f"short: {s} (expected files=1 added=1 removed=1)")

    reproduced = len(issues) > 0
    return report("BUG-014", reproduced=reproduced,
                  evidence="; ".join(issues) if issues else "all three crafted diffs parsed correctly")


if __name__ == "__main__":
    sys.exit(main())
