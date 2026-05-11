#!/usr/bin/env python3
"""BUG-039: _log feeds user goal / Planner output / claude stream text into
RichLog with markup=True without rich.markup.escape() — well-formed but
malicious markup like '[red]FAKE ERROR[/]' renders as styled text,
masquerading as system output.

Severity: High
Code: pm_agent/tui.py:331, 568, 624, 642, 1069

Static check: count _log call sites that interpolate user/LLM-controlled
strings without escape.
Runtime check: confirm Rich actually renders injected markup as styled
output (not as literal text).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _harness import report, PROJECT_ROOT  # noqa: E402


def main() -> int:
    tui = (PROJECT_ROOT / "pm_agent" / "tui.py").read_text()

    # Find _log call sites interpolating user/LLM data.
    suspicious_vars = re.findall(
        r"self\._log\(f\"[^\"]*\{(self\.goal|task\.id|t\.id|text|reason|value|crit|e|self\.repo)[^\"]*\"",
        tui,
    )
    has_escape = "rich.markup.escape" in tui or "from rich.markup import escape" in tui

    # Runtime: does Rich render markup-as-styled?
    from rich.text import Text
    from rich.markup import render
    rendered = render("[bold red]EVIL[/]")
    # If markup was active, the rendered Text has style applied to "EVIL".
    has_style = bool(rendered.spans) or "bold" in str(rendered)

    reproduced = bool(suspicious_vars) and not has_escape and has_style
    return report("BUG-039", reproduced=reproduced,
                  evidence=f"raw-interpolation _log sites: {len(suspicious_vars)} "
                           f"(vars={sorted(set(suspicious_vars))}); "
                           f"any markup.escape import={has_escape}; "
                           f"Rich applies style on injected markup={has_style}")


if __name__ == "__main__":
    sys.exit(main())
