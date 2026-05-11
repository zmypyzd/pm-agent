#!/usr/bin/env python3
"""BUG-003: TUI's _run_id used pure second-precision strftime → same-second
rerun overwrites artifacts.

Severity: High
Code: pm_agent/tui.py — _run_id initialization + _reset_for_new_run

Demo: instantiate PMAgentTUI twice in the same second and compare their
_run_id values. Fixed = they're unique (uuid suffix); buggy = identical.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _harness import report  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pm_agent.tui import PMAgentTUI  # noqa: E402


def main() -> int:
    # In mock mode no repo/worktree needed.
    a = PMAgentTUI(goal=None)
    b = PMAgentTUI(goal=None)
    same = a._run_id == b._run_id
    return report("BUG-003", reproduced=same,
                  evidence=f"a._run_id={a._run_id!r} b._run_id={b._run_id!r}")


if __name__ == "__main__":
    sys.exit(main())
