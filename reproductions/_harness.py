"""Shared utilities for bug reproduction scripts.

Each repro under reproductions/{critical,high,medium,low}/bug_NNN_*.py exits 0
when the bug is still present (the failure pattern was observed) and exits 1
when the failure pattern is NOT observed (i.e., the bug has been fixed).

No real claude API tokens are spent. Where claude is needed we install a shim
script onto PATH that mimics the stream-json output we care about.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
PM_AGENT_ROOT = PROJECT_ROOT / "pm_agent"

assert PM_AGENT_ROOT.exists(), f"could not find pm_agent at {PM_AGENT_ROOT}"


def ensure_pm_agent_on_path() -> None:
    """Add project root to sys.path so `import pm_agent.*` works without uv."""
    p = str(PROJECT_ROOT)
    if p not in sys.path:
        sys.path.insert(0, p)


@contextmanager
def temp_git_repo(initial_files: dict[str, str] | None = None,
                  branch: str = "master"):
    """Create a throwaway git repo with optional seed files.

    Yields the repo path. Cleans up on exit.
    """
    initial_files = initial_files or {".gitkeep": ""}
    tmp = Path(tempfile.mkdtemp(prefix="bug-repro-repo-"))
    try:
        for rel, content in initial_files.items():
            target = tmp / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "bug-repro",
            "GIT_AUTHOR_EMAIL": "repro@local",
            "GIT_COMMITTER_NAME": "bug-repro",
            "GIT_COMMITTER_EMAIL": "repro@local",
        }
        subprocess.run(["git", "init", "-q", "-b", branch],
                       cwd=tmp, check=True, env=env)
        subprocess.run(["git", "add", "."], cwd=tmp, check=True, env=env)
        subprocess.run(["git", "commit", "-q", "-m", "init"],
                       cwd=tmp, check=True, env=env)
        yield tmp
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@contextmanager
def claude_shim(behavior: str = "ok",
                stderr_payload: str = "",
                events: list[dict] | None = None):
    """Install a fake `claude` script onto PATH.

    behaviors:
      "ok"        — minimal valid stream-json: init + result(is_error=False)
      "stderr_flood" — write stderr_payload to stderr, then exit 0 (no stdout)
      "stream"    — emit `events` as stream-json lines, in order
      "hang"      — read but never write; sleep forever
    Yields the shim directory; caller should prepend to PATH.
    """
    import json
    tmp = Path(tempfile.mkdtemp(prefix="claude-shim-"))
    try:
        shim = tmp / "claude"
        if behavior == "ok":
            body = (
                '#!/usr/bin/env bash\n'
                'echo \'{"type":"system","subtype":"init",'
                '"session_id":"shim-1","model":"shim"}\'\n'
                'echo \'{"type":"result","is_error":false,'
                '"total_cost_usd":0.0,"duration_ms":1}\'\n'
            )
        elif behavior == "stderr_flood":
            # Pure bash: write a big chunk to stderr, then exit.
            # Buggy runner: stderr=PIPE never drained → python blocks on
            # write once 64 KB fills the pipe → shim never reaches exit.
            # Fixed runner: stderr=DEVNULL → write succeeds → shim exits
            # quickly → parent sees stdout EOF → returns clean.
            body = (
                '#!/usr/bin/env bash\n'
                f'python3 -c "import sys; sys.stderr.write({json.dumps(stderr_payload)} * 4096); sys.stderr.flush()"\n'
                'exit 0\n'
            )
        elif behavior == "stream":
            ev_json = "\n".join(json.dumps(e) for e in (events or []))
            body = (
                '#!/usr/bin/env bash\n'
                f'cat <<\'EOF\'\n{ev_json}\nEOF\n'
            )
        elif behavior == "hang":
            body = '#!/usr/bin/env bash\nsleep 600\n'
        else:
            raise ValueError(f"unknown behavior: {behavior}")
        shim.write_text(body)
        shim.chmod(0o755)
        yield tmp
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@contextmanager
def prepend_path(directory: Path):
    """Temporarily prepend a directory to PATH (process-local)."""
    old = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{directory}:{old}"
    try:
        yield
    finally:
        os.environ["PATH"] = old


def report(bug_id: str, reproduced: bool, evidence: str) -> int:
    """Standard pass/fail line for run_all.sh aggregation.

    Returns the exit code the script should use:
      0 when bug IS reproduced (the failure was observed)
      1 when bug NOT reproduced (likely fixed)
    """
    label = "REPRODUCED" if reproduced else "NOT-REPRODUCED"
    print(f"[{bug_id}] {label} — {evidence}")
    return 0 if reproduced else 1
