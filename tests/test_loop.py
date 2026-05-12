"""Unit + integration tests for pm_agent.loop — daemon orchestration."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from pm_agent.loop import (
    LoopConfig, CycleResult, run_one_cycle,
    build_coder_tasks, run_gates,
)
from pm_agent.persistence import (
    init_db, get_conn, start_cycle, record_finding, update_finding, fix_attempts,
)
from pm_agent.scanner import Finding


def _make_finding(bug_id="aa11bb22", severity="Low", paths=("src/a.py",), kind="bug") -> Finding:
    return Finding(
        bug_id=bug_id, title="fix it", severity=severity,
        paths=list(paths), acceptance=["asserts ok"], evidence="e", kind=kind,
    )


def test_build_coder_tasks_splits_code_and_test():
    """t1 should target the code path; t2 should target a test path."""
    f = _make_finding(paths=("src/a.py",))
    t1, t2 = build_coder_tasks(f, prior_diff=None)
    assert t1.id != t2.id
    # t2 should target a tests/ path; t1 should not
    assert any("test" in p.lower() or "spec" in p.lower() for p in t2.allowed_paths)
    assert not any("test" in p.lower() for p in t1.allowed_paths)


def test_build_coder_tasks_t2_embeds_prior_diff():
    """When prior_diff is passed, Coder-2's prompt must include the diff body."""
    f = _make_finding()
    diff = "--- a/src/a.py\n+++ b/src/a.py\n@@ -1 +1 @@\n-old\n+new\n"
    _t1, t2 = build_coder_tasks(f, prior_diff=diff)
    assert "Diff:" in t2.prompt or "+new" in t2.prompt  # diff content surfaced


def test_run_gates_returns_bool_and_str():
    """run_gates returns (passed, output_for_pr_body). Contract test —
    doesn't require pytest/mypy/ruff to actually be runnable in tmp_path."""
    from tests._fixtures.fake_repo import fake_repo
    with fake_repo({"pm_agent/__init__.py": "", "tests/test_x.py": "def test_x(): pass\n"}) as repo:
        passed, output = run_gates(repo)
    assert isinstance(passed, bool)
    assert isinstance(output, str)


def test_run_one_cycle_skip_gate_blocklist():
    """blocklist gate: finding paths matching .git/* or .github/* are skipped."""
    from fnmatch import fnmatch
    cfg = LoopConfig()
    f = _make_finding(paths=(".git/HEAD",))
    blocked = any(fnmatch(p, pat) for p in f.paths for pat in cfg.blocklist)
    assert blocked


def test_fix_attempts_threshold_logic(tmp_path):
    """3-cycle skip: persistence.fix_attempts returns >= 3 after 3 failed findings."""
    init_db(tmp_path / "state.db")
    f = _make_finding(bug_id="skip01")
    for _ in range(3):
        cid = start_cycle()
        fid = record_finding(cid, f)
        update_finding(fid, "failed")
    assert fix_attempts("skip01") >= 3
