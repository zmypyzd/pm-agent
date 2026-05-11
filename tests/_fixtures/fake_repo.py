"""Throwaway git repo factory for tests."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def fake_repo(seed_files: dict[str, str] | None = None, branch: str = "master"):
    """Yield a fresh git repo at tmp_path with seed_files committed.

    Env injects pm-agent fallback identity so commits don't need user.email.
    """
    seed = seed_files or {".gitkeep": ""}
    root = Path(tempfile.mkdtemp(prefix="pm-agent-fake-"))
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@local",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@local",
    }
    try:
        subprocess.run(["git", "init", "-q", "-b", branch], cwd=root, check=True, env=env)
        for rel, content in seed.items():
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        subprocess.run(["git", "add", "."], cwd=root, check=True, env=env)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=root, check=True, env=env)
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)
