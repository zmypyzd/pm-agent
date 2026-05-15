"""Unit tests for pm_agent.cli — CLI subcommand router."""
from __future__ import annotations

import sys
import types
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

from pm_agent.cli import (
    _build_parser,
    _coder_timeout_type,
    _interval_s_type,
    _nonneg_int_type,
    _path_expanduser,
    cmd_dashboard_serve,
    cmd_demo,
    cmd_loop_run,
    main,
)


def _run_cli(argv: list[str]) -> tuple[int, str, str]:
    """Run main() with patched argv; capture stdout/stderr/exit."""
    out = StringIO()
    err = StringIO()
    with patch.object(sys, "argv", ["pm-agent"] + argv):
        with patch.object(sys, "stdout", out), patch.object(sys, "stderr", err):
            try:
                rc = main()
            except SystemExit as e:
                rc = int(e.code) if e.code is not None else 0
    return rc, out.getvalue(), err.getvalue()


def test_help_lists_subcommands():
    """pm-agent --help shows loop / dashboard / tui subcommands."""
    rc, out, err = _run_cli(["--help"])
    text = out + err
    assert "loop" in text
    assert "dashboard" in text


def test_loop_status_no_running_cycle(tmp_path, monkeypatch):
    """pm-agent loop status with no running cycle prints 'no running cycle'."""
    monkeypatch.setenv("HOME", str(tmp_path))
    rc, out, _err = _run_cli(["loop", "status"])
    assert rc == 0
    assert "no running cycle" in out.lower() or "no running" in out.lower()


def test_unknown_subcommand_exits_nonzero():
    """Unknown subcommand should exit 2 (argparse convention)."""
    rc, _out, _err = _run_cli(["bogus-cmd"])
    assert rc != 0


def test_loop_bare_shows_loop_help_exits_2():
    """pm-agent loop (no nested cmd) prints loop's help + exits 2."""
    rc, out, err = _run_cli(["loop"])
    text = out + err
    assert rc == 2
    # loop's help should mention 'run' or 'status' subcommands
    assert "run" in text or "status" in text


# ---- R3-A-01 / R3-C-07: positive-int validators ----

def test_interval_s_rejects_zero():
    """--interval-s 0 must be rejected (hot-loops API otherwise)."""
    import argparse
    with pytest.raises(argparse.ArgumentTypeError, match="must be"):
        _interval_s_type("0")


def test_interval_s_rejects_below_60():
    import argparse
    with pytest.raises(argparse.ArgumentTypeError):
        _interval_s_type("59")


def test_interval_s_accepts_60_and_above():
    assert _interval_s_type("60") == 60
    assert _interval_s_type("1800") == 1800


def test_coder_timeout_rejects_negative():
    import argparse
    with pytest.raises(argparse.ArgumentTypeError):
        _coder_timeout_type("-5")


def test_coder_timeout_rejects_below_30():
    import argparse
    with pytest.raises(argparse.ArgumentTypeError):
        _coder_timeout_type("29")


def test_max_retries_rejects_negative():
    import argparse
    with pytest.raises(argparse.ArgumentTypeError):
        _nonneg_int_type("-1")


def test_max_retries_allows_zero():
    """0 = never retry is valid."""
    assert _nonneg_int_type("0") == 0


# ---- R3-C-05: path expansion ----

def test_path_expanduser_expands_tilde(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    result = _path_expanduser("~/foo.db")
    assert result.is_absolute()
    assert str(result).startswith(str(tmp_path))


def test_db_flag_expands_tilde(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    ap, _, _ = _build_parser()
    args = ap.parse_args(["loop", "report", "--db", "~/state.db"])
    assert args.db.is_absolute()
    assert not str(args.db).startswith("~")


def test_repo_flag_expands_tilde(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    ap, _, _ = _build_parser()
    args = ap.parse_args(["loop", "run", "--repo", "~/myrepo"])
    assert args.repo.is_absolute()
    assert not str(args.repo).startswith("~")


# ---- R3-C-04: clean shutdown returns 0, not 130 ----

def test_cmd_loop_run_clean_shutdown_returns_zero():
    """Graceful run_forever() exit → rc=0, not 130."""

    async def _clean_run_forever(repo, cfg):
        return None

    fake_loop_module = types.SimpleNamespace(
        run_forever=_clean_run_forever,
        LoopConfig=lambda **kw: object(),
    )
    args = types.SimpleNamespace(
        repo=Path.cwd(),
        interval_s=1800,
        max_retries=2,
        coder_timeout=180.0,
        test_timeout=120.0,
    )
    with patch.dict(sys.modules, {"pm_agent.loop": fake_loop_module}):
        rc = cmd_loop_run(args)
    assert rc == 0


def test_cmd_loop_run_keyboard_interrupt_returns_130():
    """SIGINT → rc=130 (preserves existing contract)."""

    async def _interrupted(repo, cfg):
        raise KeyboardInterrupt()

    fake_loop_module = types.SimpleNamespace(
        run_forever=_interrupted,
        LoopConfig=lambda **kw: object(),
    )
    args = types.SimpleNamespace(
        repo=Path.cwd(),
        interval_s=1800,
        max_retries=2,
        coder_timeout=180.0,
        test_timeout=120.0,
    )
    with patch.dict(sys.modules, {"pm_agent.loop": fake_loop_module}):
        rc = cmd_loop_run(args)
    assert rc == 130


# ---- `pm-agent demo` zero-arg entrypoint ----

def test_demo_listed_in_help():
    """`pm-agent --help` mentions the demo subcommand."""
    rc, out, err = _run_cli(["--help"])
    text = out + err
    assert "demo" in text


def test_cmd_demo_dispatches_to_tui_with_canonical_argv(monkeypatch, tmp_path):
    """cmd_demo: setup_demo_target then invoke tui_main with built argv.

    Patches both setup + tui_main so the test doesn't touch /tmp/ or spawn
    a real claude subprocess.
    """
    fake_target = tmp_path / "demo-target"
    fake_target.mkdir()

    setup_calls: list[bool] = []
    captured_argv: list[list[str]] = []

    def fake_setup(target: Path = fake_target) -> Path:
        setup_calls.append(True)
        return fake_target

    def fake_tui_main() -> None:
        captured_argv.append(list(sys.argv))

    monkeypatch.setattr("pm_agent.demo.setup_demo_target", fake_setup)
    monkeypatch.setattr("pm_agent.tui.main", fake_tui_main)

    rc = cmd_demo(types.SimpleNamespace())
    assert rc == 0
    assert setup_calls == [True]
    assert len(captured_argv) == 1
    argv = captured_argv[0]
    # Sanity: --repo points at the fake target; --coder-timeout + --test-cmd
    # + the goal sentence are all forwarded.
    assert "--repo" in argv
    assert str(fake_target) in argv
    assert "--coder-timeout" in argv
    assert "--test-cmd" in argv
    assert any("/health" in arg for arg in argv)


def test_setup_demo_target_creates_repo(tmp_path):
    """setup_demo_target writes server.py + test + inits git on master."""
    from pm_agent import demo

    target = tmp_path / "t"
    result = demo.setup_demo_target(target)
    assert result == target
    assert (target / "server.py").exists()
    assert (target / "tests" / "test_server.py").exists()
    assert (target / "README.md").exists()
    assert (target / ".git").is_dir()
    # Branch is master (matches WorktreeManager default-branch detection).
    import subprocess
    branch = subprocess.run(
        ["git", "-C", str(target), "rev-parse", "--abbrev-ref", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert branch == "master"


def test_setup_demo_target_is_idempotent(tmp_path):
    """Re-running setup wipes and recreates cleanly."""
    from pm_agent import demo

    target = tmp_path / "t"
    demo.setup_demo_target(target)
    (target / "leftover").write_text("stale")
    demo.setup_demo_target(target)
    assert not (target / "leftover").exists()
    assert (target / "server.py").exists()


# ---- R3-C-06: dashboard serve calls init_db ----

def test_cmd_dashboard_serve_calls_init_db(tmp_path, monkeypatch):
    """dashboard subcommand must init the DB so the API doesn't read 'idle'."""
    from pm_agent import persistence

    fake_state_db = tmp_path / "state.db"
    fake_uvicorn = types.SimpleNamespace(run=lambda *a, **kw: None)
    # Clear any prior init.
    persistence._DB_PATH = None
    persistence._LOCAL.__dict__.clear()

    args = types.SimpleNamespace(host="127.0.0.1", port=8000)
    with patch.dict(sys.modules, {"uvicorn": fake_uvicorn}), \
            patch("pm_agent.loop.STATE_DB", fake_state_db):
        cmd_dashboard_serve(args)

    assert persistence._DB_PATH == fake_state_db
    # Confirm idempotency: a second call must not raise.
    with patch.dict(sys.modules, {"uvicorn": fake_uvicorn}), \
            patch("pm_agent.loop.STATE_DB", fake_state_db):
        cmd_dashboard_serve(args)
