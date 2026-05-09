"""Git worktree management for parallel Coder agents.

Each task gets its own worktree at <repo>/.pm-agent-worktrees/<task_id>
on a branch ai/<task_id>. Coders run claude -p with cwd set to that
worktree so concurrent edits never collide.

Cleanup is best-effort and idempotent — re-creating the same task_id
removes the previous worktree first, and shutdown cleanup tolerates
already-gone worktrees.
"""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path


WORKTREES_DIRNAME = ".pm-agent-worktrees"


class WorktreeError(RuntimeError):
    pass


def _run(cmd: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=str(cwd), capture_output=True, text=True, check=check
    )


class WorktreeManager:
    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root.resolve()
        if not (self.repo_root / ".git").exists():
            raise WorktreeError(f"{self.repo_root} is not a git repo")
        self.worktrees_dir = self.repo_root / WORKTREES_DIRNAME
        self.base_branch = self._detect_base_branch()

    def _detect_base_branch(self) -> str:
        try:
            out = _run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"], self.repo_root
            ).stdout.strip()
            return out or "main"
        except subprocess.CalledProcessError:
            return "main"

    def diff_against_base(self, task_id: str) -> str:
        """Return `git diff <base>..<task_branch>` as a unified diff string.
        Empty string when the branch has no diverging commits."""
        try:
            return _run(
                [
                    "git",
                    "diff",
                    f"{self.base_branch}..{self._branch_for(task_id)}",
                ],
                self.repo_root,
                check=False,
            ).stdout
        except subprocess.CalledProcessError:
            return ""

    def _path_for(self, task_id: str) -> Path:
        return self.worktrees_dir / task_id

    def _branch_for(self, task_id: str) -> str:
        return f"ai/{task_id}"

    def create(self, task_id: str) -> Path:
        """Create a fresh worktree + branch for task_id. Replaces any existing."""
        path = self._path_for(task_id)
        # Pre-clean: if path or branch already exists from a prior crashed run.
        self.cleanup(task_id, _quiet=True)
        self.worktrees_dir.mkdir(parents=True, exist_ok=True)
        try:
            _run(
                ["git", "worktree", "add", "-b", self._branch_for(task_id), str(path)],
                self.repo_root,
            )
        except subprocess.CalledProcessError as e:
            raise WorktreeError(
                f"git worktree add failed for {task_id}: {e.stderr.strip()}"
            ) from e
        return path

    def cleanup(self, task_id: str, _quiet: bool = False) -> None:
        """Remove worktree and its branch. Idempotent."""
        path = self._path_for(task_id)
        # `git worktree remove --force` handles both the dir and the metadata.
        _run(
            ["git", "worktree", "remove", "--force", str(path)],
            self.repo_root,
            check=False,
        )
        _run(
            ["git", "branch", "-D", self._branch_for(task_id)],
            self.repo_root,
            check=False,
        )
        # Stragglers (e.g. dir exists but worktree metadata gone)
        if path.exists():
            import shutil
            shutil.rmtree(path, ignore_errors=True)

    def list_active(self) -> list[str]:
        """Return list of task_ids that currently have a worktree."""
        if not self.worktrees_dir.exists():
            return []
        return sorted(p.name for p in self.worktrees_dir.iterdir() if p.is_dir())

    # async wrappers — git operations block, so we offload to a thread.
    async def acreate(self, task_id: str) -> Path:
        return await asyncio.to_thread(self.create, task_id)

    async def acleanup(self, task_id: str) -> None:
        await asyncio.to_thread(self.cleanup, task_id)
