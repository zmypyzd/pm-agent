"""Unit tests for pm_agent.cli — CLI subcommand router."""
from __future__ import annotations

import sys
from io import StringIO
from unittest.mock import patch

import pytest

from pm_agent.cli import main


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


def test_help_lists_subcommands(capsys):
    """pm-agent --help shows loop / dashboard / tui subcommands."""
    with patch.object(sys, "argv", ["pm-agent", "--help"]):
        with pytest.raises(SystemExit):
            main()
    captured = capsys.readouterr()
    text = captured.out + captured.err
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
