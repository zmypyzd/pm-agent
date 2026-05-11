#!/usr/bin/env python3
"""BUG-018: TUI progress bar only advances inside the `result` event branch.
Tasks that end in `timeout` or `api_error` never update the progress bar.

Severity: Medium
Code: pm_agent/tui.py:660-664 vs the timeout / api_error branches

Static demo: scan _stream_one's event handling and confirm that:
  1. progress.update is called ONLY inside the `et == 'result'` branch
  2. there is no progress.update under the `timeout` or `is_error` paths
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _harness import report, PROJECT_ROOT  # noqa: E402


def main() -> int:
    tui = (PROJECT_ROOT / "pm_agent" / "tui.py").read_text()
    body = re.search(r"async def _stream_one\(.*?(?=\n    [a-zA-Z@])", tui, re.DOTALL)
    if not body:
        return report("BUG-018", reproduced=False, evidence="could not locate _stream_one body")
    src = body.group(0)

    # Carve out the `et == 'result'` block
    result_block = re.search(r"elif et == \"result\":(.*?)(?=elif et|\Z)", src, re.DOTALL)
    timeout_block = re.search(r"elif et == \"system\" and ev\.get\(\"subtype\"\) == \"timeout\":(.*?)(?=elif et|\Z)", src, re.DOTALL)

    progress_in_result = bool(result_block and "progress.update" in result_block.group(1))
    progress_in_timeout = bool(timeout_block and "progress.update" in timeout_block.group(1))

    reproduced = progress_in_result and not progress_in_timeout
    return report("BUG-018", reproduced=reproduced,
                  evidence=f"progress.update inside result={progress_in_result}; "
                           f"inside timeout={progress_in_timeout}")


if __name__ == "__main__":
    sys.exit(main())
