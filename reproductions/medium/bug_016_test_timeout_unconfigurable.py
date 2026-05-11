#!/usr/bin/env python3
"""BUG-016: test_timeout for integration test is hardcoded 120s; TUI does
not expose --test-timeout flag.

Severity: Medium
Code: pm_agent/worktree.py:135 (default), tui.py argparse (missing flag),
      tui.py:437-439 (calls aintegrate without test_timeout)

Static demo: confirm three things via source inspection:
  1. worktree.integrate has `test_timeout=120.0` default
  2. TUI argparse has no --test-timeout option
  3. TUI's aintegrate call site does not pass test_timeout kwarg
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _harness import report, PROJECT_ROOT  # noqa: E402


def main() -> int:
    worktree = (PROJECT_ROOT / "pm_agent" / "worktree.py").read_text()
    tui = (PROJECT_ROOT / "pm_agent" / "tui.py").read_text()

    has_default = "test_timeout: float = 120.0" in worktree
    has_arg = "--test-timeout" in tui
    call = re.search(r"self\.wm\.aintegrate\((.*?)\)", tui, re.DOTALL)
    passes_kwarg = bool(call and "test_timeout" in call.group(1))

    reproduced = has_default and not has_arg and not passes_kwarg
    return report("BUG-016", reproduced=reproduced,
                  evidence=f"hardcoded_120s={has_default} "
                           f"cli_flag_exists={has_arg} "
                           f"passes_kwarg={passes_kwarg}")


if __name__ == "__main__":
    sys.exit(main())
