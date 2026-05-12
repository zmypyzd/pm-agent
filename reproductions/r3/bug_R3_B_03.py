#!/usr/bin/env python3
"""R3-B-03: auto_merge mis-classifies gh's "auto-merge is already enabled"
stderr (which gh emits when a previous --auto queue is still valid) as a
hard failure, returning action="failed".

Pre-fix → action == "failed" (REPRODUCED, exit 0)
Post-fix → action == "auto-merge-queued" (NOT REPRODUCED, exit 1)

Strategy: drive auto_merge through tests/_fixtures/gh_shim with a "pr merge"
response that writes "auto-merge is already enabled for PR #42" to stderr
and exits non-zero — the exact behavior real gh exhibits in this case.
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import shlex
import stat
import sys
import tempfile

# Make sure imports resolve to this repo.
REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))


from pm_agent.github import auto_merge  # noqa: E402
from pm_agent.scanner import Finding  # noqa: E402


def _build_shim(tmp: pathlib.Path) -> None:
    """Bash shim that:
      - 'pr list' → echo JSON [] (no existing PR)
      - 'pr create' → echo JSON {number:42,url:...}
      - 'pr merge' → echo to stderr "auto-merge is already enabled for PR 42"
                     and exit 1
      - anything else → empty body
    """
    create_payload = json.dumps({"number": 42, "url": "https://gh/x/y/pull/42"})
    shim = tmp / "gh"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        'KEY="$1 $2"\n'
        'case "$KEY" in\n'
        '  "pr list") echo "[]" ;;\n'
        f'  "pr create") echo {shlex.quote(create_payload)} ;;\n'
        '  "pr merge")\n'
        '    echo "auto-merge is already enabled for PR #42" >&2\n'
        '    exit 1 ;;\n'
        '  *) echo "" ;;\n'
        "esac\n"
    )
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def main() -> int:
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="r3-b-03-"))
    _build_shim(tmp)
    old_path = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{tmp}:{old_path}"
    try:
        finding = Finding(
            bug_id="ab12cd34", title="t", severity="High",
            paths=["a.py"], acceptance=["ok"], evidence="e", kind="bug",
        )
        result = asyncio.run(auto_merge("ai/T-x", finding))
    finally:
        os.environ["PATH"] = old_path

    if result.action == "failed":
        print(f"[R3-B-03] REPRODUCED — auto_merge returned action='failed' "
              f"for already-enabled stderr (number={result.number})")
        return 0
    if result.action == "auto-merge-queued":
        print(f"[R3-B-03] NOT-REPRODUCED — auto_merge correctly returned "
              f"action='auto-merge-queued' (number={result.number})")
        return 1
    print(f"[R3-B-03] NOT-REPRODUCED — action={result.action!r} "
          f"(neither failed nor auto-merge-queued)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
