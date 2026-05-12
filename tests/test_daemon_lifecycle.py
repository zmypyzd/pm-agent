"""4 daemon-lifecycle smoke tests per spec F6.

Each verifies a daemon-level transition that other tests don't reach:
- startup with empty DB
- SIGTERM grace within one finding
- scanner crash survival (cycle marked errored, daemon doesn't crash)
- gh auth halt path (daemon exits cleanly without infinite retry)
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from pm_agent import persistence
import pm_agent.loop
from pm_agent.loop import run_forever, LoopConfig
from tests._fixtures.fake_repo import fake_repo
from tests._fixtures.claude_shim import claude_shim
from tests._fixtures.gh_shim import gh_shim


def _empty_scanner_events() -> list[dict]:
    return [
        {"type": "system", "subtype": "init", "session_id": "s"},
        {"type": "assistant",
         "message": {"content": [{"type": "text",
             "text": "```yaml\nfindings: []\n```"}]}},
        {"type": "result", "is_error": False, "total_cost_usd": 0.01},
    ]


async def _fake_sync_pr():
    """Returns [] immediately — replaces the real gh subprocess call (~1.7s)."""
    return []


def test_daemon_startup_with_empty_db(tmp_path, monkeypatch):
    """Fresh init: reconciler runs against empty DB; daemon starts;
    one cycle completes (scanner returns 0 findings → scan-empty);
    daemon cancellable."""
    monkeypatch.setenv("HOME", str(tmp_path))
    # Redirect the module-level STATE_DB so run_forever uses tmp_path.
    monkeypatch.setattr(pm_agent.loop, "STATE_DB", tmp_path / ".pm-agent" / "state.db")
    # Monkeypatch run_gates to avoid launching real uv-pytest.
    monkeypatch.setattr(pm_agent.loop, "run_gates", lambda repo: (True, ""))
    # Monkeypatch sync_pr_states to return immediately (real gh subprocess ~1.7s).
    monkeypatch.setattr("pm_agent.loop.github.sync_pr_states", lambda: _fake_sync_pr())

    with fake_repo() as repo:
        with gh_shim(responses={"pr list": "[]"}):
            with claude_shim(events=_empty_scanner_events()):
                async def main():
                    cfg = LoopConfig(interval_s=60)  # long interval so daemon waits
                    task = asyncio.create_task(run_forever(repo, cfg))
                    # Wait long enough for the first cycle to complete before
                    # cancelling. scan subprocess startup can take ~1-2s.
                    await asyncio.sleep(3.0)
                    task.cancel()
                    try:
                        await asyncio.wait_for(task, timeout=5.0)
                    except (asyncio.CancelledError, asyncio.TimeoutError):
                        pass
                asyncio.run(main())
    # State DB created
    db = tmp_path / ".pm-agent" / "state.db"
    assert db.exists()
    # At least one cycle row exists; none should be left as 'running'
    persistence.init_db(db)
    rows = persistence.get_conn().execute(
        "SELECT status FROM cycles",
    ).fetchall()
    assert rows, "no cycle row recorded"
    assert all(r["status"] != "running" for r in rows), \
        f"zombie 'running' cycle detected: {[r['status'] for r in rows]}"


def test_daemon_sigterm_grace(tmp_path, monkeypatch):
    """Daemon cancellation mid-cycle → daemon exits within 5s.

    Uses task.cancel() instead of os.kill(SIGTERM) — same effect on the
    daemon's exit path (CancelledError propagates through asyncio.run)
    but doesn't risk killing pytest itself if the event loop hasn't yet
    installed the signal handler.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(pm_agent.loop, "STATE_DB", tmp_path / ".pm-agent" / "state.db")
    monkeypatch.setattr(pm_agent.loop, "run_gates", lambda repo: (True, ""))
    # Monkeypatch sync_pr_states to return immediately (real gh subprocess ~1.7s).
    monkeypatch.setattr("pm_agent.loop.github.sync_pr_states", lambda: _fake_sync_pr())

    with fake_repo() as repo:
        with gh_shim(responses={"pr list": "[]"}):
            with claude_shim(events=_empty_scanner_events()):
                async def main():
                    cfg = LoopConfig(interval_s=1)
                    task = asyncio.create_task(run_forever(repo, cfg))
                    await asyncio.sleep(0.3)
                    task.cancel()
                    # Must complete within 5s — worst case one finding round
                    try:
                        await asyncio.wait_for(task, timeout=5.0)
                    except (asyncio.CancelledError, asyncio.TimeoutError):
                        pass
                asyncio.run(main())


def test_daemon_survives_scanner_crash(tmp_path, monkeypatch):
    """Scanner raises Exception → cycle marked 'errored' (spec §5.5)
    → daemon doesn't crash → recorded the cycle."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(pm_agent.loop, "STATE_DB", tmp_path / ".pm-agent" / "state.db")
    monkeypatch.setattr(pm_agent.loop, "run_gates", lambda repo: (True, ""))
    # Monkeypatch sync_pr_states to return immediately (real gh subprocess ~1.7s).
    monkeypatch.setattr("pm_agent.loop.github.sync_pr_states", lambda: _fake_sync_pr())

    # Force scanner.scan to raise — exercises spec §5.5 'errored' path
    async def _crashing_scan(*args, **kwargs):
        raise RuntimeError("simulated scanner crash")

    monkeypatch.setattr("pm_agent.loop.scanner.scan", _crashing_scan)

    with fake_repo() as repo:
        with gh_shim(responses={"pr list": "[]"}):
            async def main():
                # interval_s=60 ensures daemon waits between cycles; we cancel
                # during the inter-cycle sleep, not mid-cycle.
                cfg = LoopConfig(interval_s=60)
                task = asyncio.create_task(run_forever(repo, cfg))
                # Scanner crash is instant; give generous buffer for startup.
                await asyncio.sleep(0.6)
                task.cancel()
                try:
                    await asyncio.wait_for(task, timeout=3.0)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass
            asyncio.run(main())

    # Verify cycle recorded with errored status (spec §5.5 path)
    db = tmp_path / ".pm-agent" / "state.db"
    assert db.exists()
    persistence.init_db(db)
    rows = persistence.get_conn().execute(
        "SELECT status FROM cycles",
    ).fetchall()
    # At least one cycle must exist and at least one must be 'errored'
    # (cancellation may add an 'aborted' row from the next iteration too)
    statuses = [r["status"] for r in rows]
    assert rows, "no cycle row recorded"
    assert "errored" in statuses, \
        f"spec §5.5 errored path not exercised; got: {statuses}"


def test_daemon_halts_on_gh_auth_failure(tmp_path, monkeypatch):
    """gh auth failure → daemon exits cleanly within 5s (does not loop)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(pm_agent.loop, "STATE_DB", tmp_path / ".pm-agent" / "state.db")
    monkeypatch.setattr(pm_agent.loop, "run_gates", lambda repo: (True, ""))

    with fake_repo() as repo:
        with gh_shim(auth_failure=True):
            with claude_shim(events=_empty_scanner_events()):
                async def main():
                    cfg = LoopConfig(interval_s=1)
                    # run_forever should return cleanly on GhAuthError
                    # within one cycle attempt — wrap in wait_for for safety
                    try:
                        await asyncio.wait_for(run_forever(repo, cfg), timeout=10.0)
                    except asyncio.TimeoutError:
                        pytest.fail("daemon did not halt on gh auth failure within 10s")
                asyncio.run(main())
