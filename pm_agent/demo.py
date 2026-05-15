"""Zero-arg demo entrypoint for `pm-agent demo`.

Mirrors `docs/demo-commands.sh::reset_target + full_e2e_dry_run` in pure
Python so the command works after `uv tool install` without the repo on
disk. New users run a single command and see the canonical /health goal
flow end-to-end.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


DEMO_TARGET: Path = Path("/tmp/pm-agent-day7-target")
DEMO_GOAL: str = (
    "Add a /health endpoint returning a dict with key status equal to ok, "
    "plus a test"
)
DEMO_TEST_CMD: str = (
    "python3 -c 'import tests.test_server as t; "
    '[getattr(t,n)() for n in dir(t) if n.startswith("test_")]; '
    "print(\"PASS\")'"
)
DEMO_CODER_TIMEOUT: str = "180"


_SERVER_PY = '''"""Tiny request handler used as a target repo for pm-agent demos."""


def handle_request(path: str) -> dict:
    if path == "/":
        return {"hello": "world"}
    return {"error": "not found", "path": path}
'''

_TEST_SERVER_PY = '''from server import handle_request


def test_root():
    result = handle_request("/")
    assert result["hello"] == "world"


def test_unknown_path():
    result = handle_request("/nope")
    assert result["error"] == "not found"
    assert result["path"] == "/nope"
'''


def setup_demo_target(target: Path = DEMO_TARGET) -> Path:
    """Wipe and recreate the canonical demo target repo.

    Returns the absolute path. Idempotent — safe to re-run between demos.
    """
    if target.exists():
        shutil.rmtree(target)
    (target / "tests").mkdir(parents=True)
    (target / "server.py").write_text(_SERVER_PY)
    (target / "tests" / "test_server.py").write_text(_TEST_SERVER_PY)
    (target / "README.md").write_text("init\n")

    # `init -b master` matches the rest of the demo tooling; default branch
    # detection in WorktreeManager keys on `master` here.
    subprocess.run(
        ["git", "init", "-q", "-b", "master", str(target)],
        check=True, capture_output=True,
    )
    # Inline identity avoids requiring a globally configured git user on a
    # fresh box — important for first-run `pm-agent demo` after install.
    git_id = ["-c", "user.name=pm-agent", "-c", "user.email=pm@local"]
    subprocess.run(
        ["git", "-C", str(target), *git_id, "add", "."],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(target), *git_id, "commit", "-q", "-m", "init"],
        check=True, capture_output=True,
    )
    return target


def build_demo_tui_argv(target: Path) -> list[str]:
    """The argv `cmd_demo` injects before delegating to `pm_agent.tui.main`."""
    return [
        "pm-agent",
        "--repo", str(target),
        "--coder-timeout", DEMO_CODER_TIMEOUT,
        "--test-cmd", DEMO_TEST_CMD,
        DEMO_GOAL,
    ]
