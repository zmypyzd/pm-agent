#!/usr/bin/env python3
"""BUG-034: pyproject.toml has no [project.scripts] entry; users have to
type `python -m pm_agent.tui` instead of `pm-agent`.

Severity: Low (UX)
Code: pyproject.toml
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _harness import report, PROJECT_ROOT  # noqa: E402


def main() -> int:
    src = (PROJECT_ROOT / "pyproject.toml").read_text()
    has_scripts = "[project.scripts]" in src
    return report("BUG-034", reproduced=not has_scripts,
                  evidence=f"[project.scripts] present={has_scripts}")


if __name__ == "__main__":
    sys.exit(main())
