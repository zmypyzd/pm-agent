#!/usr/bin/env python3
"""BUG-020 (downgraded to Low after re-review in BUGS.md, but kept here for
audit completeness): WorktreeManager.create() preclean calls cleanup()
which deletes both worktree AND branch — this is the legacy day-7 API,
but day-8 explicitly split worktree-removal from branch-removal.

Severity: Low (was Medium pre-review)
Code: pm_agent/worktree.py:82-86, 99-121
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _harness import report, PROJECT_ROOT  # noqa: E402


def main() -> int:
    src = (PROJECT_ROOT / "pm_agent" / "worktree.py").read_text()
    create_body = re.search(r"def create\(self.*?def cleanup_worktree", src, re.DOTALL)
    if not create_body:
        return report("BUG-020", reproduced=False, evidence="could not locate create()")
    calls_full_cleanup = "self.cleanup(task_id, _quiet=True)" in create_body.group(0)
    reproduced = calls_full_cleanup
    return report("BUG-020", reproduced=reproduced,
                  evidence=f"create() preclean calls full cleanup (worktree+branch)={calls_full_cleanup}")


if __name__ == "__main__":
    sys.exit(main())
