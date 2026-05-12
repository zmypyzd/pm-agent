"""End-to-end integration test for the autonomous loop.

Shimmed scanner returns 1 finding; pipeline runs scan → record →
gate checks → Coder simulations → integrate → route → cleanup.
Verifies cycle is recorded with correct status and the finding row
makes it to persistence."""
from __future__ import annotations

import asyncio
import json

import pytest

import pm_agent.loop
from pm_agent.loop import run_one_cycle, LoopConfig
from pm_agent.persistence import init_db, get_conn
from tests._fixtures.fake_repo import fake_repo
from tests._fixtures.claude_shim import claude_shim
from tests._fixtures.gh_shim import gh_shim


def _scanner_events(yaml_block: str) -> list[dict]:
    """Stream-json events for a Scanner LLM response."""
    return [
        {"type": "system", "subtype": "init", "session_id": "s", "model": "shim"},
        {"type": "assistant",
         "message": {"content": [{"type": "text", "text": yaml_block}]}},
        {"type": "result", "is_error": False,
         "total_cost_usd": 0.02, "duration_ms": 1},
    ]


def test_full_cycle_records_finding_and_finishes_cycle(tmp_path, monkeypatch):
    """Scanner shim returns 1 finding; cycle records it; finish_cycle sets a
    valid status. Does not assert PR creation (Coder shim produces no diff,
    so finding will fail at NO_CHANGES — but cycle itself must complete cleanly)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    yaml_block = """```yaml
findings:
  - title: Add type hints to scan
    severity: Low
    paths:
      - pm_agent/scanner.py
    acceptance:
      - scan signature has return type annotation
    evidence: pm_agent/scanner.py — return type missing
    kind: tech-debt
```"""
    db_path = tmp_path / ".pm-agent" / "state.db"
    init_db(db_path)

    # Monkeypatch run_gates so it doesn't try to launch real uv-pytest
    # in the fake repo (which lacks a pyproject.toml).
    monkeypatch.setattr(pm_agent.loop, "run_gates", lambda repo: (True, ""))

    with fake_repo({"pm_agent/scanner.py": "def scan(): pass\n"}) as repo:
        with claude_shim(events=_scanner_events(yaml_block)):
            with gh_shim(responses={
                "pr list": json.dumps([]),
                "pr create": json.dumps({"number": 1, "url": "https://gh/x/y/pull/1"}),
                "pr merge": "queued",
            }):
                cfg = LoopConfig(interval_s=1, coder_timeout=15)
                result = asyncio.run(run_one_cycle(repo, cfg))

    # E2E pipeline ran cleanly (no exception). The shim Coder produces no
    # diff so the finding will end as 'failed' — but the cycle itself
    # must be properly recorded and finished.
    assert result.cycle_id > 0
    assert result.findings_total == 1
    # Cycle status was set (not left as 'running' zombie)
    row = get_conn().execute(
        "SELECT status FROM cycles WHERE id=?", (result.cycle_id,),
    ).fetchone()
    assert row is not None
    assert row["status"] in ("done", "scan-empty", "aborted", "errored")
    # Finding was recorded
    found = get_conn().execute(
        "SELECT bug_id FROM findings WHERE cycle_id=?", (result.cycle_id,),
    ).fetchall()
    assert len(found) == 1
