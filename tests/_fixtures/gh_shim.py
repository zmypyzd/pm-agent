"""Fake `gh` CLI on PATH; returns scripted JSON; records argv."""
from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def gh_shim(
    responses: dict[str, str] | None = None,   # subcommand key like 'pr create' → response string
    auth_failure: bool = False,
    record_calls: bool = False,
):
    """Routes on first 2 argv tokens (e.g. 'pr create', 'pr list')."""
    tmp = Path(tempfile.mkdtemp(prefix="gh-shim-"))
    record_file = tmp / "calls.log"
    responses = responses or {}
    payload_json = json.dumps(responses)
    auth_block = "echo 'authentication failed' >&2; exit 4" if auth_failure else ""
    record_block = f"printf '%s\\n' \"$@\" >> {record_file}" if record_calls else ""
    body = f"""#!/usr/bin/env bash
{record_block}
{auth_block}
KEY="$1 $2"
RESPONSES='{payload_json}'
RESP=$(printf '%s' "$RESPONSES" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('$KEY', '{{}}'))")
echo "$RESP"
"""
    shim = tmp / "gh"
    shim.write_text(body)
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    old_path = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{tmp}:{old_path}"
    try:
        yield {"dir": tmp, "record": record_file}
    finally:
        os.environ["PATH"] = old_path
        shutil.rmtree(tmp, ignore_errors=True)
