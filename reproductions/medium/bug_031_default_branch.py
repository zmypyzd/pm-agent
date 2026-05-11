#!/usr/bin/env python3
"""BUG-031: _ensure_target_repo calls `git init -q` without `-b master`,
so the default branch depends on the user's init.defaultBranch git
config. demo-commands.sh DOES use `-b master`. Inconsistency.

Severity: Low → escalated Medium for inconsistency
Code: pm_agent/tui.py:1098 vs docs/demo-commands.sh:39
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _harness import report, PROJECT_ROOT  # noqa: E402


def main() -> int:
    tui = (PROJECT_ROOT / "pm_agent" / "tui.py").read_text()
    demo = (PROJECT_ROOT / "docs" / "demo-commands.sh").read_text()

    tui_uses_explicit_branch = bool(re.search(r'git", "init".*"-b"', tui))
    demo_uses_explicit_branch = bool(re.search(r"git -C \"\$TGT\" init -q -b master|init -q -b master|init.*?-b ", demo))

    reproduced = (not tui_uses_explicit_branch) and demo_uses_explicit_branch
    return report("BUG-031", reproduced=reproduced,
                  evidence=f"tui sets -b explicitly={tui_uses_explicit_branch}; "
                           f"demo sets -b explicitly={demo_uses_explicit_branch}")


if __name__ == "__main__":
    sys.exit(main())
