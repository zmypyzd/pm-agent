#!/usr/bin/env python3
"""BUG-038: .teamagent/ has manifest.json tracked but knowledge.db and
shared-claude.md untracked + no .gitignore entry — git add . will commit
a 98 KB binary.

Severity: Low (Medium if knowledge.db has private data)
Code: .gitignore, .teamagent/
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _harness import report, PROJECT_ROOT  # noqa: E402


def main() -> int:
    tracked = subprocess.run(
        ["git", "ls-files", ".teamagent"],
        cwd=PROJECT_ROOT, capture_output=True, text=True, check=False,
    ).stdout.splitlines()

    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", ".teamagent"],
        cwd=PROJECT_ROOT, capture_output=True, text=True, check=False,
    ).stdout.splitlines()

    gi = (PROJECT_ROOT / ".gitignore").read_text()
    ignored = ".teamagent" in gi

    half = bool(tracked) and bool(untracked) and not ignored
    return report("BUG-038", reproduced=half,
                  evidence=f"tracked={tracked} untracked={untracked} "
                           f".gitignore mentions .teamagent={ignored}")


if __name__ == "__main__":
    sys.exit(main())
