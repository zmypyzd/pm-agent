"""Git worktree management for parallel Coder agents.

Each task gets its own worktree at <repo>/.pm-agent-worktrees/<task_id>
on a branch ai/<task_id>. Coders run claude -p with cwd set to that
worktree so concurrent edits never collide.

Day 8 split: cleanup_worktree() removes the directory but KEEPS the
branch so we can later merge it into ai/integration/<run-id>.
delete_branch() runs at end-of-session.
"""
from __future__ import annotations

import asyncio
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


WORKTREES_DIRNAME = ".pm-agent-worktrees"


class WorktreeError(RuntimeError):
    pass


@dataclass
class IntegrationResult:
    branch: str
    worktree_path: Path
    merged_tasks: list[str] = field(default_factory=list)
    conflicts: list[dict] = field(default_factory=list)
    test_result: dict | None = None
    diff_against_base: str = ""


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

    def cleanup_worktree(self, task_id: str) -> None:
        """Remove worktree directory only. Keeps the branch so it can be
        integrated later. Idempotent."""
        path = self._path_for(task_id)
        _run(
            ["git", "worktree", "remove", "--force", str(path)],
            self.repo_root,
            check=False,
        )
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)

    def delete_branch(self, branch: str | None = None, task_id: str | None = None) -> None:
        """Delete a branch. Pass either a branch name or a task_id."""
        target = branch or (self._branch_for(task_id) if task_id else None)
        if not target:
            return
        _run(["git", "branch", "-D", target], self.repo_root, check=False)

    def cleanup(self, task_id: str, _quiet: bool = False) -> None:
        """Backwards-compatible: remove worktree AND delete branch."""
        self.cleanup_worktree(task_id)
        self.delete_branch(task_id=task_id)

    def list_active(self) -> list[str]:
        """Return list of task_ids that currently have a worktree."""
        if not self.worktrees_dir.exists():
            return []
        return sorted(p.name for p in self.worktrees_dir.iterdir() if p.is_dir())

    def integrate(
        self,
        run_id: str,
        task_ids: list[str],
        test_cmd: str | None = None,
        test_timeout: float = 120.0,
    ) -> IntegrationResult:
        """Merge each ai/<task_id> branch into ai/integration/<run_id> in a
        fresh worktree. On clean merges, optionally run a test command and
        capture its result. Always returns; conflicts are reported, not raised.

        The integration worktree is left in place — caller is responsible for
        cleanup_integration() after capturing the diff/test info."""
        int_branch = f"ai/integration/{run_id}"
        int_path = self.worktrees_dir / f"integration-{run_id}"
        # Pre-clean stale integration from a crashed run.
        if int_path.exists():
            self.cleanup_worktree(f"integration-{run_id}")
            self.delete_branch(branch=int_branch)

        result = IntegrationResult(branch=int_branch, worktree_path=int_path)

        try:
            _run(
                [
                    "git", "worktree", "add", "-b", int_branch,
                    str(int_path), self.base_branch,
                ],
                self.repo_root,
            )
        except subprocess.CalledProcessError as e:
            raise WorktreeError(
                f"create integration worktree failed: {e.stderr.strip()}"
            ) from e

        for tid in task_ids:
            r = _run(
                [
                    "git", "merge", "--no-ff", "--no-edit",
                    "-m", f"integrate {tid}",
                    self._branch_for(tid),
                ],
                int_path,
                check=False,
            )
            if r.returncode == 0:
                result.merged_tasks.append(tid)
            else:
                result.conflicts.append(
                    {"task_id": tid, "output": (r.stdout + r.stderr).strip()[:2000]}
                )
                _run(["git", "merge", "--abort"], int_path, check=False)

        # Tests run only if we have a clean merge of all branches.
        if test_cmd and not result.conflicts and result.merged_tasks:
            try:
                tr = subprocess.run(
                    test_cmd,
                    cwd=str(int_path),
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=test_timeout,
                )
                result.test_result = {
                    "command": test_cmd,
                    "exit_code": tr.returncode,
                    "stdout": tr.stdout[-2000:],
                    "stderr": tr.stderr[-2000:],
                }
            except subprocess.TimeoutExpired:
                result.test_result = {
                    "command": test_cmd,
                    "exit_code": -1,
                    "stdout": "",
                    "stderr": f"timed out after {test_timeout}s",
                }

        # Diff captured BEFORE cleanup so summary stays useful even after
        # integration worktree+branch are removed.
        result.diff_against_base = _run(
            ["git", "diff", f"{self.base_branch}..{int_branch}"],
            self.repo_root,
            check=False,
        ).stdout
        return result

    def cleanup_integration(self, integration: IntegrationResult) -> None:
        self.cleanup_worktree(integration.worktree_path.name)
        self.delete_branch(branch=integration.branch)

    # async wrappers — git operations block, so we offload to a thread.
    async def acreate(self, task_id: str) -> Path:
        return await asyncio.to_thread(self.create, task_id)

    async def acleanup(self, task_id: str) -> None:
        await asyncio.to_thread(self.cleanup, task_id)

    async def acleanup_worktree(self, task_id: str) -> None:
        await asyncio.to_thread(self.cleanup_worktree, task_id)

    async def adelete_branch(self, task_id: str) -> None:
        await asyncio.to_thread(self.delete_branch, None, task_id)

    async def aintegrate(
        self,
        run_id: str,
        task_ids: list[str],
        test_cmd: str | None = None,
    ) -> IntegrationResult:
        return await asyncio.to_thread(self.integrate, run_id, task_ids, test_cmd)

    async def acleanup_integration(self, integration: IntegrationResult) -> None:
        await asyncio.to_thread(self.cleanup_integration, integration)
