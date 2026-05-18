"""Test argv parsing in pm_agent.tui.main()."""
from __future__ import annotations

import sys
import pytest


def test_main_accepts_daemon_flag(monkeypatch, tmp_path):
    """--daemon is a recognized argparse flag and sets open_daemon=True."""
    from pm_agent import tui

    captured = {}
    real_app = tui.PMAgentTUI
    class _Capture(real_app):
        def __init__(self, *a, **kw):
            captured.update(kw)
            captured["args"] = a
        def run(self):
            pass

    monkeypatch.setattr(tui, "PMAgentTUI", _Capture)
    monkeypatch.setattr(sys, "argv",
                        ["pm-agent", "--daemon", "--repo", str(tmp_path)])
    tui.main()
    assert captured["open_daemon"] is True
    assert str(captured["repo"]) == str(tmp_path)


def test_main_loop_cfg_picks_up_coder_timeout(monkeypatch, tmp_path):
    from pm_agent import tui
    captured = {}
    class _Capture(tui.PMAgentTUI):
        def __init__(self, *a, **kw):
            captured.update(kw)
        def run(self):
            pass
    monkeypatch.setattr(tui, "PMAgentTUI", _Capture)
    monkeypatch.setattr(sys, "argv",
                        ["pm-agent", "--repo", str(tmp_path),
                         "--coder-timeout", "42"])
    tui.main()
    assert captured["loop_cfg"].coder_timeout == 42.0


def test_main_without_daemon_defaults_to_goal(monkeypatch, tmp_path):
    from pm_agent import tui
    captured = {}
    class _Capture(tui.PMAgentTUI):
        def __init__(self, *a, **kw):
            captured.update(kw)
        def run(self):
            pass
    monkeypatch.setattr(tui, "PMAgentTUI", _Capture)
    monkeypatch.setattr(sys, "argv",
                        ["pm-agent", "--repo", str(tmp_path)])
    tui.main()
    assert captured["open_daemon"] is False
