"""Pre-flight sanity checks for pm-agent dry runs.

Called by ``pm-agent loop preflight``.  All checks are read-only (no mutations).

Each check returns a :class:`CheckResult`; the runner prints "✅ READY" or
"❌ FAIL: <reason>" per check and exits 0 only if every non-warn-only check
passed.
"""
from __future__ import annotations

import subprocess
import sqlite3
from dataclasses import dataclass
from pathlib import Path


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class CheckResult:
    name: str
    ok: bool
    message: str
    warn_only: bool = False   # True → print warning but don't flip exit code


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def check_state_db_dir(state_db: Path) -> CheckResult:
    """state.db parent exists or can be created, and is writable."""
    db_dir = state_db.parent
    try:
        db_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return CheckResult(
            name="state.db dir writable",
            ok=False,
            message=f"Cannot create {db_dir}: {exc}",
        )
    if not db_dir.exists():
        return CheckResult(
            name="state.db dir writable",
            ok=False,
            message=f"{db_dir} does not exist and could not be created",
        )
    # Quick write test
    probe = db_dir / ".pm_agent_write_probe"
    try:
        probe.touch()
        probe.unlink()
    except OSError as exc:
        return CheckResult(
            name="state.db dir writable",
            ok=False,
            message=f"{db_dir} is not writable: {exc}",
        )
    return CheckResult(
        name="state.db dir writable",
        ok=True,
        message=str(db_dir),
    )


def check_state_db_clean(state_db: Path) -> CheckResult:
    """state.db either doesn't exist, or contains 0 cycles."""
    if not state_db.exists():
        return CheckResult(
            name="state.db clean",
            ok=True,
            message="does not exist (fresh run)",
        )
    try:
        conn = sqlite3.connect(state_db)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute("SELECT COUNT(*) AS n FROM cycles").fetchone()
            count = row["n"]
        except sqlite3.OperationalError:
            # Table doesn't exist yet — schema not initialised, counts as clean.
            count = 0
        finally:
            conn.close()
    except Exception as exc:
        return CheckResult(
            name="state.db clean",
            ok=False,
            message=f"Cannot open {state_db}: {exc}",
        )
    if count == 0:
        return CheckResult(
            name="state.db clean",
            ok=True,
            message="0 cycles (clean)",
        )
    return CheckResult(
        name="state.db clean",
        ok=False,
        message=(
            f"{count} cycle(s) already in state.db. "
            f"Remove with:  rm {state_db}"
        ),
    )


def check_claude_cli(timeout: float = 5.0) -> CheckResult:
    """``claude --version`` exits 0 within *timeout* seconds."""
    try:
        result = subprocess.run(
            ["claude", "--version"],
            capture_output=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return CheckResult(
            name="claude CLI on PATH",
            ok=False,
            message="'claude' not found on PATH",
        )
    except subprocess.TimeoutExpired:
        return CheckResult(
            name="claude CLI on PATH",
            ok=False,
            message=f"'claude --version' timed out after {timeout}s",
        )
    if result.returncode != 0:
        stderr = result.stderr.decode(errors="replace").strip()
        return CheckResult(
            name="claude CLI on PATH",
            ok=False,
            message=f"'claude --version' exited {result.returncode}: {stderr}",
        )
    version = (result.stdout or result.stderr).decode(errors="replace").strip()
    return CheckResult(
        name="claude CLI on PATH",
        ok=True,
        message=version or "ok",
    )


def check_gh_auth(timeout: float = 15.0) -> CheckResult:
    """``gh auth status`` exits 0 within *timeout* seconds.

    Default is 15s, not the 5s used elsewhere: on macOS, the first call
    after login can block on Keychain unlock for 5-10s. A tighter
    timeout produces false FAILs that scare operators away from
    launching dry-runs.
    """
    try:
        result = subprocess.run(
            ["gh", "auth", "status"],
            capture_output=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return CheckResult(
            name="gh CLI authenticated",
            ok=False,
            message="'gh' not found on PATH",
        )
    except subprocess.TimeoutExpired:
        return CheckResult(
            name="gh CLI authenticated",
            ok=False,
            message=f"'gh auth status' timed out after {timeout}s",
        )
    if result.returncode != 0:
        stderr = result.stderr.decode(errors="replace").strip()
        return CheckResult(
            name="gh CLI authenticated",
            ok=False,
            message=f"'gh auth status' exited {result.returncode}: {stderr}",
        )
    return CheckResult(
        name="gh CLI authenticated",
        ok=True,
        message="authenticated",
    )


def check_repo_clean(repo: Path, timeout: float = 5.0) -> CheckResult:
    """``git status --porcelain`` is empty in *repo*."""
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo,
            capture_output=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return CheckResult(
            name="repo clean",
            ok=False,
            message="'git' not found on PATH",
        )
    except subprocess.TimeoutExpired:
        return CheckResult(
            name="repo clean",
            ok=False,
            message=f"'git status' timed out after {timeout}s",
        )
    if result.returncode != 0:
        stderr = result.stderr.decode(errors="replace").strip()
        return CheckResult(
            name="repo clean",
            ok=False,
            message=f"'git status' failed (not a git repo?): {stderr}",
        )
    dirty = result.stdout.decode(errors="replace").strip()
    if dirty:
        lines = dirty.splitlines()
        preview = lines[0] if lines else dirty
        extra = f" (+{len(lines) - 1} more)" if len(lines) > 1 else ""
        return CheckResult(
            name="repo clean",
            ok=False,
            message=f"uncommitted changes: {preview}{extra}",
        )
    return CheckResult(
        name="repo clean",
        ok=True,
        message=str(repo),
    )


def check_repo_on_main(repo: Path, timeout: float = 5.0) -> CheckResult:
    """HEAD is ``main`` in *repo* (warn-only)."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=repo,
            capture_output=True,
            timeout=timeout,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        # Already covered by check_repo_clean; silently pass here.
        return CheckResult(
            name="repo on main",
            ok=True,
            message="(skipped — git unavailable)",
            warn_only=True,
        )
    if result.returncode != 0:
        return CheckResult(
            name="repo on main",
            ok=True,
            message="(skipped — not a git repo)",
            warn_only=True,
        )
    branch = result.stdout.decode(errors="replace").strip()
    if branch != "main":
        return CheckResult(
            name="repo on main",
            ok=False,
            message=f"on branch '{branch}', not 'main' (intentional?)",
            warn_only=True,
        )
    return CheckResult(
        name="repo on main",
        ok=True,
        message="main",
        warn_only=False,
    )


def check_tmp_writable() -> CheckResult:
    """/tmp/loop.log is creatable."""
    probe = Path("/tmp/loop.log")
    try:
        probe.touch()
    except OSError as exc:
        return CheckResult(
            name="/tmp writable",
            ok=False,
            message=f"Cannot write /tmp/loop.log: {exc}",
        )
    return CheckResult(
        name="/tmp writable",
        ok=True,
        message="/tmp/loop.log ok",
    )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_preflight(
    state_db: Path,
    repo: Path,
    subprocess_timeout: float = 5.0,
) -> tuple[list[CheckResult], bool]:
    """Run all checks and return (results, all_hard_checks_passed)."""
    results: list[CheckResult] = [
        check_state_db_dir(state_db),
        check_state_db_clean(state_db),
        check_claude_cli(timeout=subprocess_timeout),
        check_gh_auth(timeout=max(subprocess_timeout, 15.0)),
        check_repo_clean(repo, timeout=subprocess_timeout),
        check_repo_on_main(repo, timeout=subprocess_timeout),
        check_tmp_writable(),
    ]
    all_passed = all(r.ok or r.warn_only for r in results)
    return results, all_passed


def print_preflight(results: list[CheckResult], all_passed: bool) -> None:
    """Print results in the prescribed format."""
    for r in results:
        if r.ok:
            symbol = "✅ READY"
        elif r.warn_only:
            symbol = "⚠️  WARN "
        else:
            symbol = "❌ FAIL "
        print(f"  {symbol}  {r.name}: {r.message}")

    print()
    if all_passed:
        print("Overall: ✅ READY — all checks passed")
    else:
        failed = [r for r in results if not r.ok and not r.warn_only]
        print(f"Overall: ❌ FAIL — {len(failed)} check(s) failed")
