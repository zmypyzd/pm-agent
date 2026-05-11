"""Fake `claude` binary on PATH; records argv; replays scripted stream-json events."""
from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def claude_shim(
    events: list[dict] | None = None,
    error_at: str | None = None,
    record_prompts: bool = False,
):
    """Install fake claude on PATH for the duration of the with-block.

    events: list of stream-json dicts to emit on stdout (one per line).
    error_at: when set to 'init', the shim exits 1 (simulates spawn failure).
    record_prompts: if True, write argv to a file callers can read.
    """
    tmp = Path(tempfile.mkdtemp(prefix="claude-shim-"))
    record_file = tmp / "argv.log"
    events_json = "\n".join(json.dumps(e) for e in (events or [
        {"type": "system", "subtype": "init", "session_id": "shim", "model": "shim"},
        {"type": "result", "is_error": False, "total_cost_usd": 0.0, "duration_ms": 1},
    ]))
    body_lines = ["#!/usr/bin/env bash"]
    if record_prompts:
        body_lines.append(f'printf "%s\\n" "$@" >> {record_file}')
    if error_at == "init":
        body_lines.append('exit 1')
    else:
        body_lines.append(f"cat <<'STREAM_EOF'\n{events_json}\nSTREAM_EOF")
    shim = tmp / "claude"
    shim.write_text("\n".join(body_lines))
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    old_path = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{tmp}:{old_path}"
    try:
        yield {"dir": tmp, "record": record_file}
    finally:
        os.environ["PATH"] = old_path
        shutil.rmtree(tmp, ignore_errors=True)
