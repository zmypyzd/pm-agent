#!/usr/bin/env python3
"""BUG-006: Coder prompt does NOT include allowed_paths or acceptance.

Severity: High
Code: pm_agent/tui.py:594

Static demo: read tui.py:_stream_one, locate the line that builds
`full_prompt`, and confirm that neither `task.allowed_paths` nor
`task.acceptance` appears in any string template that goes into the
Coder's prompt. The Coder runs blind to its sandbox boundary.

Reproduced ⇒ neither attribute is referenced near full_prompt construction.
Fixed ⇒ at least one of them is interpolated into the Coder prompt.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _harness import report, PROJECT_ROOT  # noqa: E402


def main() -> int:
    tui = (PROJECT_ROOT / "pm_agent" / "tui.py").read_text()
    # Find the body of _stream_one
    m = re.search(r"async def _stream_one\(self, task: CoderTask\) -> None:.*?(?=\n    [a-zA-Z@])",
                  tui, re.DOTALL)
    if not m:
        return report("BUG-006", reproduced=False,
                      evidence="could not locate _stream_one body")

    body = m.group(0)
    builds_prompt = "full_prompt" in body and "task.prompt" in body
    references_allowed = "allowed_paths" in body
    references_acceptance = "task.acceptance" in body or "acceptance" in body

    if builds_prompt and not references_allowed and not references_acceptance:
        return report(
            "BUG-006", reproduced=True,
            evidence="_stream_one builds full_prompt from task.prompt only — "
                     "neither allowed_paths nor acceptance is passed to Coder")
    return report(
        "BUG-006", reproduced=False,
        evidence=f"builds_prompt={builds_prompt} "
                 f"allowed={references_allowed} acceptance={references_acceptance}")


if __name__ == "__main__":
    sys.exit(main())
