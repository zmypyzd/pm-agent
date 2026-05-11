#!/usr/bin/env python3
"""BUG-023: main.py is a leftover `uv init` Hello-World script. Not
referenced from pyproject.toml, README, or any module.

Severity: Low
Code: main.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _harness import report, PROJECT_ROOT  # noqa: E402


def main() -> int:
    mp = PROJECT_ROOT / "main.py"
    if not mp.exists():
        return report("BUG-023", reproduced=False, evidence="main.py removed")

    src = mp.read_text()
    is_hello_world = 'print("Hello from pm-agent!")' in src

    pyproject = (PROJECT_ROOT / "pyproject.toml").read_text()
    referenced = "main.py" in pyproject or "[project.scripts]" in pyproject

    # Cross-check that README doesn't recommend running main.py
    readme = (PROJECT_ROOT / "README.md").read_text()
    readme_refs = bool(re.search(r"\bmain\.py\b", readme))

    reproduced = is_hello_world and not referenced and not readme_refs
    return report("BUG-023", reproduced=reproduced,
                  evidence=f"hello_world_body={is_hello_world}; "
                           f"pyproject refs main.py={referenced}; "
                           f"readme refs main.py={readme_refs}")


if __name__ == "__main__":
    sys.exit(main())
