"""Unit tests for pm_agent.preflight (pm-agent loop preflight subcommand)."""
from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from pm_agent import preflight as pf


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_git_repo(path: Path) -> None:
    """Initialise a bare-minimum git repo with one commit on main."""
    subprocess.run(["git", "init", "-b", "main", str(path)],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t.com"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "T"],
                   check=True, capture_output=True)
    readme = path / "README.md"
    readme.write_text("hello")
    subprocess.run(["git", "-C", str(path), "add", "README.md"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "commit", "-m", "init"],
                   check=True, capture_output=True)


def _seed_cycles(db_path: Path, n: int = 1) -> None:
    """Create state.db with *n* dummy cycle rows."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS cycles "
        "(id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT, "
        "status TEXT NOT NULL, cost_usd REAL NOT NULL DEFAULT 0)"
    )
    for i in range(n):
        conn.execute(
            "INSERT INTO cycles (started_at, status) VALUES (?, ?)",
            (f"2024-01-0{i+1}T00:00:00+00:00", "done"),
        )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# check_state_db_dir
# ---------------------------------------------------------------------------

class TestCheckStateDbDir:
    def test_creates_missing_dir(self, tmp_path: Path) -> None:
        db = tmp_path / "new" / "nested" / "state.db"
        r = pf.check_state_db_dir(db)
        assert r.ok
        assert db.parent.exists()

    def test_existing_writable_dir(self, tmp_path: Path) -> None:
        db = tmp_path / "state.db"
        r = pf.check_state_db_dir(db)
        assert r.ok

    def test_unwritable_dir_fails(self, tmp_path: Path) -> None:
        locked = tmp_path / "locked"
        locked.mkdir()
        locked.chmod(0o444)
        db = locked / "sub" / "state.db"
        try:
            r = pf.check_state_db_dir(db)
            # Either fails at mkdir or at write probe — either way not ok
            assert not r.ok
        finally:
            locked.chmod(0o755)


# ---------------------------------------------------------------------------
# check_state_db_clean
# ---------------------------------------------------------------------------

class TestCheckStateDbClean:
    def test_missing_db_is_clean(self, tmp_path: Path) -> None:
        db = tmp_path / "state.db"
        r = pf.check_state_db_clean(db)
        assert r.ok

    def test_empty_db_is_clean(self, tmp_path: Path) -> None:
        """DB exists but cycles table has 0 rows."""
        db = tmp_path / "state.db"
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE cycles "
            "(id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT, "
            "status TEXT NOT NULL, cost_usd REAL NOT NULL DEFAULT 0)"
        )
        conn.commit()
        conn.close()
        r = pf.check_state_db_clean(db)
        assert r.ok

    def test_db_with_cycles_fails(self, tmp_path: Path) -> None:
        db = tmp_path / "state.db"
        _seed_cycles(db, n=3)
        r = pf.check_state_db_clean(db)
        assert not r.ok
        assert "3" in r.message
        assert "rm" in r.message

    def test_db_without_schema_is_clean(self, tmp_path: Path) -> None:
        """A raw sqlite file with no cycles table counts as clean."""
        db = tmp_path / "state.db"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE other (x INTEGER)")
        conn.commit()
        conn.close()
        r = pf.check_state_db_clean(db)
        assert r.ok


# ---------------------------------------------------------------------------
# check_claude_cli
# ---------------------------------------------------------------------------

class TestCheckClaudeCli:
    def test_success_when_exits_zero(self) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = b"claude 1.2.3"
        mock_result.stderr = b""
        with patch("subprocess.run", return_value=mock_result) as m:
            r = pf.check_claude_cli()
        assert r.ok
        m.assert_called_once()
        assert m.call_args[0][0] == ["claude", "--version"]

    def test_not_on_path_fails(self) -> None:
        with patch("subprocess.run", side_effect=FileNotFoundError):
            r = pf.check_claude_cli()
        assert not r.ok
        assert "PATH" in r.message

    def test_nonzero_exit_fails(self) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = b""
        mock_result.stderr = b"some error"
        with patch("subprocess.run", return_value=mock_result):
            r = pf.check_claude_cli()
        assert not r.ok
        assert "1" in r.message

    def test_timeout_fails(self) -> None:
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("claude", 5)):
            r = pf.check_claude_cli()
        assert not r.ok
        assert "timed out" in r.message


# ---------------------------------------------------------------------------
# check_gh_auth
# ---------------------------------------------------------------------------

class TestCheckGhAuth:
    def test_success_when_exits_zero(self) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = b"Logged in"
        mock_result.stderr = b""
        with patch("subprocess.run", return_value=mock_result):
            r = pf.check_gh_auth()
        assert r.ok

    def test_not_on_path_fails(self) -> None:
        with patch("subprocess.run", side_effect=FileNotFoundError):
            r = pf.check_gh_auth()
        assert not r.ok
        assert "PATH" in r.message

    def test_not_authenticated_fails(self) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = b""
        mock_result.stderr = b"You are not logged into any GitHub hosts."
        with patch("subprocess.run", return_value=mock_result):
            r = pf.check_gh_auth()
        assert not r.ok


# ---------------------------------------------------------------------------
# check_repo_clean
# ---------------------------------------------------------------------------

class TestCheckRepoClean:
    def test_clean_repo_passes(self, tmp_path: Path) -> None:
        _make_git_repo(tmp_path)
        r = pf.check_repo_clean(tmp_path)
        assert r.ok

    def test_dirty_repo_fails(self, tmp_path: Path) -> None:
        _make_git_repo(tmp_path)
        (tmp_path / "dirty.txt").write_text("new file")
        r = pf.check_repo_clean(tmp_path)
        assert not r.ok
        assert "uncommitted" in r.message

    def test_not_a_git_repo_fails(self, tmp_path: Path) -> None:
        r = pf.check_repo_clean(tmp_path)
        assert not r.ok

    def test_git_not_found_fails(self, tmp_path: Path) -> None:
        with patch("subprocess.run", side_effect=FileNotFoundError):
            r = pf.check_repo_clean(tmp_path)
        assert not r.ok
        assert "PATH" in r.message


# ---------------------------------------------------------------------------
# check_repo_on_main
# ---------------------------------------------------------------------------

class TestCheckRepoOnMain:
    def test_on_main_passes(self, tmp_path: Path) -> None:
        _make_git_repo(tmp_path)
        r = pf.check_repo_on_main(tmp_path)
        assert r.ok
        assert r.warn_only is False

    def test_not_on_main_is_warn_only(self, tmp_path: Path) -> None:
        _make_git_repo(tmp_path)
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", "-b", "feature-x"],
            check=True, capture_output=True,
        )
        r = pf.check_repo_on_main(tmp_path)
        assert not r.ok
        assert r.warn_only  # must NOT cause overall failure


# ---------------------------------------------------------------------------
# check_tmp_writable
# ---------------------------------------------------------------------------

class TestCheckTmpWritable:
    def test_tmp_writable(self) -> None:
        r = pf.check_tmp_writable()
        assert r.ok


# ---------------------------------------------------------------------------
# run_preflight (integration)
# ---------------------------------------------------------------------------

class TestRunPreflight:
    """Happy-path + key failure mode integration tests."""

    def _mock_subprocess_ok(self) -> MagicMock:
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = b"ok"
        mock_result.stderr = b""
        return mock_result

    def test_happy_path_all_pass(self, tmp_path: Path) -> None:
        """All checks pass: new dir, no db, mocked CLIs, clean repo."""
        state_db = tmp_path / ".pm-agent" / "state.db"
        repo = tmp_path / "repo"
        _make_git_repo(repo)

        mock_result = self._mock_subprocess_ok()
        # Only mock the claude/gh calls; git calls use real subprocess.
        orig_run = subprocess.run

        def selective_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
            if cmd[0] in ("claude", "gh"):
                return mock_result
            return orig_run(cmd, **kwargs)

        with patch("subprocess.run", side_effect=selective_run):
            results, all_passed = pf.run_preflight(state_db, repo)

        assert all_passed, [r for r in results if not r.ok and not r.warn_only]

    def test_fail_when_db_has_prior_cycles(self, tmp_path: Path) -> None:
        state_db = tmp_path / ".pm-agent" / "state.db"
        _seed_cycles(state_db, n=2)
        repo = tmp_path / "repo"
        _make_git_repo(repo)

        mock_result = self._mock_subprocess_ok()
        orig_run = subprocess.run

        def selective_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
            if cmd[0] in ("claude", "gh"):
                return mock_result
            return orig_run(cmd, **kwargs)

        with patch("subprocess.run", side_effect=selective_run):
            results, all_passed = pf.run_preflight(state_db, repo)

        assert not all_passed
        failed = [r for r in results if not r.ok and not r.warn_only]
        assert any("cycle" in r.message.lower() for r in failed)

    def test_fail_when_repo_dirty(self, tmp_path: Path) -> None:
        state_db = tmp_path / ".pm-agent" / "state.db"
        repo = tmp_path / "repo"
        _make_git_repo(repo)
        (repo / "untracked.py").write_text("dirty")  # untracked = dirty

        mock_result = self._mock_subprocess_ok()
        orig_run = subprocess.run

        def selective_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
            if cmd[0] in ("claude", "gh"):
                return mock_result
            return orig_run(cmd, **kwargs)

        with patch("subprocess.run", side_effect=selective_run):
            results, all_passed = pf.run_preflight(state_db, repo)

        assert not all_passed
        failed = [r for r in results if not r.ok and not r.warn_only]
        assert any("uncommitted" in r.message for r in failed)

    def test_warn_only_branch_does_not_fail(self, tmp_path: Path) -> None:
        """Non-main branch produces a warning but overall still passes."""
        state_db = tmp_path / ".pm-agent" / "state.db"
        repo = tmp_path / "repo"
        _make_git_repo(repo)
        subprocess.run(
            ["git", "-C", str(repo), "checkout", "-b", "my-feature"],
            check=True, capture_output=True,
        )

        mock_result = self._mock_subprocess_ok()
        orig_run = subprocess.run

        def selective_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
            if cmd[0] in ("claude", "gh"):
                return mock_result
            return orig_run(cmd, **kwargs)

        with patch("subprocess.run", side_effect=selective_run):
            results, all_passed = pf.run_preflight(state_db, repo)

        # Branch check is warn-only → should not cause overall failure
        branch_check = next(r for r in results if r.name == "repo on main")
        assert not branch_check.ok
        assert branch_check.warn_only
        # all_passed should still be True (no hard failures)
        assert all_passed

    def test_fail_when_claude_not_on_path(self, tmp_path: Path) -> None:
        state_db = tmp_path / ".pm-agent" / "state.db"
        repo = tmp_path / "repo"
        _make_git_repo(repo)
        orig_run = subprocess.run

        def selective_run(cmd, **kwargs):  # type: ignore[no-untyped-def]
            if cmd[0] == "claude":
                raise FileNotFoundError
            if cmd[0] == "gh":
                mock = MagicMock()
                mock.returncode = 0
                mock.stdout = b""
                mock.stderr = b""
                return mock
            return orig_run(cmd, **kwargs)

        with patch("subprocess.run", side_effect=selective_run):
            results, all_passed = pf.run_preflight(state_db, repo)

        assert not all_passed
        claude_check = next(r for r in results if r.name == "claude CLI on PATH")
        assert not claude_check.ok


# ---------------------------------------------------------------------------
# print_preflight output format
# ---------------------------------------------------------------------------

class TestPrintPreflight:
    def test_all_pass_output(self, capsys: pytest.CaptureFixture) -> None:
        results = [
            pf.CheckResult("check A", ok=True, message="good"),
            pf.CheckResult("check B", ok=True, message="also good"),
        ]
        pf.print_preflight(results, all_passed=True)
        out = capsys.readouterr().out
        assert "✅" in out
        assert "READY" in out

    def test_fail_output(self, capsys: pytest.CaptureFixture) -> None:
        results = [
            pf.CheckResult("check A", ok=False, message="bad thing"),
            pf.CheckResult("check B", ok=True, message="fine"),
        ]
        pf.print_preflight(results, all_passed=False)
        out = capsys.readouterr().out
        assert "❌" in out
        assert "FAIL" in out

    def test_warn_output(self, capsys: pytest.CaptureFixture) -> None:
        results = [
            pf.CheckResult("check A", ok=False, message="maybe bad", warn_only=True),
        ]
        pf.print_preflight(results, all_passed=True)
        out = capsys.readouterr().out
        assert "WARN" in out
