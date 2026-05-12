"""Unit tests for pm_agent.github — gh CLI wrapper."""
from __future__ import annotations

import asyncio
import json

import pytest

from pm_agent.github import open_pr, auto_merge, sync_pr_states, PRResult, PRState, GhAuthError
from pm_agent.scanner import Finding
from tests._fixtures.gh_shim import gh_shim


def _finding() -> Finding:
    return Finding(
        bug_id="ab12cd34", title="t", severity="Low",
        paths=["a.py"], acceptance=["ok"], evidence="e", kind="bug",
    )


def test_open_pr_records_argv():
    """open_pr calls 'gh pr create' with title/body args."""
    responses = {
        "pr list": json.dumps([]),  # no existing PR
        "pr create": json.dumps({"number": 42, "url": "https://gh/x/y/pull/42"}),
    }
    with gh_shim(responses=responses, record_calls=True) as shim:
        result = asyncio.run(open_pr("ai/T-1", _finding()))
        calls = shim["record"].read_text()
    assert isinstance(result, PRResult)
    assert result.number == 42
    assert result.action == "opened"
    # shim records one arg per line; normalise to space-joined to check sub-command
    calls_joined = " ".join(calls.split())
    assert "pr create" in calls_joined


def test_auto_merge_returns_action_signal():
    """auto_merge wraps open_pr + 'gh pr merge --auto'; returns PRResult with action."""
    responses = {
        "pr list": json.dumps([]),
        "pr create": json.dumps({"number": 7, "url": "https://gh/x/y/pull/7"}),
        "pr merge": "✓ Pull request set to merge automatically",
    }
    with gh_shim(responses=responses):
        result = asyncio.run(auto_merge("ai/T-2", _finding()))
    assert isinstance(result, PRResult)
    assert result.action in ("auto-merge-queued", "merged-now")


def test_sync_pr_states_returns_states():
    """sync_pr_states pulls 'gh pr list --state all --search head:ai/' and maps to PRState."""
    responses = {
        "pr list": json.dumps([
            {"number": 1, "state": "MERGED", "headRefName": "ai/T-1", "mergedAt": "2026-05-12T00:00:00Z"},
            {"number": 2, "state": "OPEN", "headRefName": "ai/T-3", "mergedAt": None},
        ]),
    }
    with gh_shim(responses=responses):
        states = asyncio.run(sync_pr_states())
    assert len(states) == 2
    assert all(isinstance(s, PRState) for s in states)
    merged = [s for s in states if s.state == "merged"]
    assert len(merged) == 1


def test_gh_auth_error_raised_on_auth_failure():
    """gh_shim auth_failure=True → GhAuthError raised, not silently consumed."""
    from pm_agent.github import GhAuthError
    with gh_shim(auth_failure=True):
        with pytest.raises(GhAuthError):
            asyncio.run(open_pr("ai/T-X", _finding()))


def test_auto_merge_distinguishes_queued_from_merged_now():
    """gh queued message contains 'merged' (in 'automatically merged') —
    must NOT label as merged-now. This is the spec-driven correctness check."""
    queued_msg = "✓ Pull request #5 will be automatically merged after meeting the required conditions"
    responses = {
        "pr list": json.dumps([]),
        "pr create": json.dumps({"number": 5, "url": "https://gh/x/y/pull/5"}),
        "pr merge": queued_msg,
    }
    with gh_shim(responses=responses):
        result = asyncio.run(auto_merge("ai/T-3", _finding()))
    assert result.action == "auto-merge-queued"


def test_auto_merge_detects_immediate_merge():
    """gh's immediate-merge message: 'Pull request #N merged' (no 'automatically')."""
    merged_msg = "✓ Pull request #6 merged"
    responses = {
        "pr list": json.dumps([]),
        "pr create": json.dumps({"number": 6, "url": "https://gh/x/y/pull/6"}),
        "pr merge": merged_msg,
    }
    with gh_shim(responses=responses):
        result = asyncio.run(auto_merge("ai/T-4", _finding()))
    assert result.action == "merged-now"
