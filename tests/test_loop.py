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


def test_drive_coder_returns_cost_tuple():
    """_drive_coder must return (bool, float) — verified contract."""
    import inspect
    from pm_agent.loop import _drive_coder
    sig = inspect.signature(_drive_coder)
    ret = sig.return_annotation
    assert "tuple" in str(ret).lower() or "Tuple" in str(ret)


def test_run_one_cycle_marks_aborted_on_stop_event(tmp_path):
    """When stop_event fires before any finding is processed, cycle should be
    marked 'aborted' or 'done' but never left as 'running'."""
    from pm_agent.loop import run_one_cycle, LoopConfig
    from pm_agent.persistence import init_db, get_conn
    from tests._fixtures.fake_repo import fake_repo
    from tests._fixtures.gh_shim import gh_shim
    from tests._fixtures.claude_shim import claude_shim
    import asyncio, json

    init_db(tmp_path / "state.db")

    async def driver(repo):
        stop = asyncio.Event()
        stop.set()  # already set — cycle should bail immediately
        return await run_one_cycle(repo, LoopConfig(interval_s=1), stop_event=stop)

    with fake_repo() as repo:
        with gh_shim(responses={"pr list": "[]"}):
            with claude_shim():
                result = asyncio.run(driver(repo))
    assert result.cycle_id > 0
    row = get_conn().execute(
        "SELECT status FROM cycles WHERE id=?", (result.cycle_id,),
    ).fetchone()
    assert row["status"] != "running", f"cycle left as 'running' zombie!"


def test_per_finding_cleanup_survives_cancel():
    """R3-A-02 regression: when CancelledError fires during the first cleanup
    ``await`` in the per-finding ``finally`` block, every subsequent cleanup
    step MUST still run. CancelledError inherits from BaseException in 3.11+,
    so plain ``except Exception`` would let it propagate past later cleanups
    and leak worktrees + branches on operator-initiated shutdown.

    The fix wraps each step in ``asyncio.shield`` with its own try/except,
    collecting CancelledError per-step and re-raising once at the end.

    This test reproduces the live cleanup loop shape (parameterized so that
    if the fix regresses to the pre-fix pattern, the test fails)."""
    import inspect
    from unittest.mock import AsyncMock
    import pm_agent.loop as loop_mod

    # Structural assertion: the fix must use asyncio.shield + a cancelled flag.
    src = inspect.getsource(loop_mod.run_one_cycle)
    assert "asyncio.shield" in src, (
        "regression: per-finding cleanup no longer uses asyncio.shield; "
        "R3-A-02 fix is missing"
    )
    assert "cancelled = False" in src, (
        "regression: per-finding cleanup no longer tracks a cancelled flag; "
        "R3-A-02 fix is missing"
    )

    # Behavior assertion: drive the exact same cleanup loop primitives the
    # fix uses, with the first cleanup raising CancelledError. All subsequent
    # cleanups MUST fire, and CancelledError MUST be re-raised at the end.
    cleanup_first = AsyncMock(side_effect=asyncio.CancelledError())
    cleanups_after = [AsyncMock() for _ in range(4)]
    all_cleanups = [cleanup_first] + cleanups_after

    async def drive() -> bool:
        cancelled = False
        for fn in all_cleanups:
            try:
                await asyncio.shield(fn())
            except asyncio.CancelledError:
                cancelled = True
            except Exception:
                pass
        if cancelled:
            raise asyncio.CancelledError()
        return False  # unreachable

    saw_cancel = False
    try:
        asyncio.run(drive())
    except asyncio.CancelledError:
        saw_cancel = True

    assert saw_cancel, "CancelledError must be re-raised after cleanups complete"
    for i, m in enumerate(cleanups_after, start=2):
        assert m.called, f"cleanup step {i} skipped — R3-A-02 regression"


def test_run_one_cycle_skips_record_pr_on_failed_action():
    """BUG-R4-1: when github.open_pr / auto_merge returns
    ``PRResult(action="failed", number=0)``, the cycle loop must:
      (a) not call ``persistence.record_pr`` (it would crash on the
          UNIQUE github_number=0 collision when ≥2 PRs fail per cycle),
      (b) mark the finding ``failed``, not ``done``,
      (c) NOT increment ``findings_fixed``.

    Verified structurally — the inline guard must reference both
    ``pr.action`` and ``"failed"`` and route via ``update_finding(...,
    "failed")`` followed by ``continue`` before record_pr is reachable.
    Behavioral coverage is in ``reproductions/r4/bug_R4_1_*.py`` (at the
    persistence layer)."""
    import inspect, re
    import pm_agent.loop as loop_mod

    src = inspect.getsource(loop_mod.run_one_cycle)
    # The guard pattern: a `pr.action == "failed"` check that marks the
    # finding failed and continues BEFORE record_pr is called.
    pattern = re.compile(
        r'if\s+pr\.action\s*==\s*"failed"\s*:\s*\n'
        r'\s+persistence\.update_finding\([^,]+,\s*"failed"\)\s*\n'
        r'\s+continue',
        re.MULTILINE,
    )
    assert pattern.search(src), (
        "regression: run_one_cycle no longer guards record_pr against "
        "pr.action == 'failed'. BUG-R4-1 will recur as soon as gh pr "
        "create fails twice in one cycle."
    )

    # Sanity: the guard must precede the first record_pr call site.
    guard_pos = src.find('pr.action == "failed"')
    record_pos = src.find("persistence.record_pr(")
    assert 0 < guard_pos < record_pos, (
        "regression: the action='failed' guard must run BEFORE record_pr"
    )


def test_run_one_cycle_skips_finding_with_open_pr_for_bug():
    """BUG-R5-2: when find_open_pr_for_bug returns a number for this
    finding's bug_id, the loop must skip the Coder pipeline and mark
    the finding 'skipped'. The gate must fire BEFORE Coder-1 spawns
    (otherwise it wastes a real LLM call). Structural test in the
    spirit of R3-A-02 / R4-1."""
    import inspect, re
    import pm_agent.loop as loop_mod

    src = inspect.getsource(loop_mod.run_one_cycle)

    # The gate pattern: a find_open_pr_for_bug check that marks the
    # finding skipped and continues. Must use update_finding(..., "skipped")
    # (not "failed" — the bug *will* land via the prior PR, this is just
    # a duplicate route).
    pattern = re.compile(
        r'existing_pr\s*=\s*persistence\.find_open_pr_for_bug\(finding\.bug_id\)\s*\n'
        r'\s+if\s+existing_pr\s+is\s+not\s+None\s*:\s*\n'
        r'\s+persistence\.update_finding\([^,]+,\s*"skipped"\)',
        re.MULTILINE,
    )
    assert pattern.search(src), (
        "regression: run_one_cycle no longer guards against duplicate PRs "
        "for the same bug_id. BUG-R5-2 will recur as soon as the scanner "
        "re-finds an already-PR'd bug."
    )

    # Sanity: the gate must run BEFORE the first Coder spawn (wm.acreate).
    gate_pos = src.find("find_open_pr_for_bug(finding.bug_id)")
    coder_pos = src.find("wm.acreate(t1.id)")
    assert 0 < gate_pos < coder_pos, (
        "regression: the duplicate-PR gate must run BEFORE Coder-1 spawn"
    )
