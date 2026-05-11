#!/usr/bin/env python3
"""BUG-033: _log writes each assistant token directly to RichLog without
any rate-limit / batching. Large streamed responses cause UI lag.

Severity: Low (UX)
Code: pm_agent/tui.py:620-624
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _harness import report, PROJECT_ROOT  # noqa: E402


def main() -> int:
    src = (PROJECT_ROOT / "pm_agent" / "tui.py").read_text()
    body = re.search(r"async def _stream_one\(.*?(?=\n    [a-zA-Z@])", src, re.DOTALL)
    if not body:
        return report("BUG-033", reproduced=False, evidence="cannot find _stream_one")

    assistant_block = re.search(r"elif et == \"assistant\":(.*?)(?=elif|\Z)", body.group(0), re.DOTALL)
    if not assistant_block:
        return report("BUG-033", reproduced=False, evidence="no assistant branch")

    has_log_call = "self._log" in assistant_block.group(1)
    has_throttle = any(k in assistant_block.group(1)
                       for k in ("buffer", "batch", "throttle",
                                 "assistant_buf", "while \"\\n\"",
                                 "set_interval"))

    reproduced = has_log_call and not has_throttle
    return report("BUG-033", reproduced=reproduced,
                  evidence=f"per-token _log call={has_log_call}; throttling={has_throttle}")


if __name__ == "__main__":
    sys.exit(main())
