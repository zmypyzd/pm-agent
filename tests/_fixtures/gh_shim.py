"""Fake `gh` CLI on PATH; returns scripted JSON; records argv."""
from __future__ import annotations

import json
import os
import shlex
import shutil
import stat
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def gh_shim(
    responses: dict[str, str] | None = None,
    auth_failure: bool = False,
    record_calls: bool = False,
):
    """Routes on first 2 argv tokens (e.g. 'pr create', 'pr list').

    responses: maps "<verb> <subverb>" → response string (JSON or plain text).
    auth_failure: stderr 'authentication failed' + exit 4 — for daemon halt path.
    record_calls: append argv to a log file callers can read.
    """
    if responses is None:
        responses = {}
    tmp = Path(tempfile.mkdtemp(prefix="gh-shim-"))
    record_file = tmp / "calls.log"
    payload_file = tmp / "responses.json"
    payload_file.write_text(json.dumps(responses))
    record_q = shlex.quote(str(record_file))
    payload_q = shlex.quote(str(payload_file))
    python_q = shlex.quote(sys.executable)
    body_lines = ["#!/usr/bin/env bash"]
    if record_calls:
        body_lines.append(f'printf "%s\\n" "$@" >> {record_q}')
    if auth_failure:
        body_lines.append("echo 'authentication failed' >&2")
        body_lines.append("exit 4")
    else:
        # Pass key and payload path via env vars to avoid shell injection.
        body_lines.append('KEY="$1 $2"')
        body_lines.append(
            f'RESP=$(GH_SHIM_KEY="$KEY" GH_SHIM_PAYLOAD={payload_q} {python_q} -c '
            "'import json,os; d=json.load(open(os.environ[\"GH_SHIM_PAYLOAD\"])); "
            'print(d.get(os.environ["GH_SHIM_KEY"], "{}"))\')'
        )
        body_lines.append('echo "$RESP"')
    shim = tmp / "gh"
    shim.write_text("\n".join(body_lines) + "\n")
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    old_path = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{tmp}:{old_path}"
    try:
        yield {"dir": tmp, "record": record_file}
    finally:
        os.environ["PATH"] = old_path
        shutil.rmtree(tmp, ignore_errors=True)
