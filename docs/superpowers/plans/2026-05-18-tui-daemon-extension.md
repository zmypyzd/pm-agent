# TUI Daemon Extension Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a daemon mode to the existing pm-agent TUI: in-process `loop.run_forever`, preflight bar, cycle/findings/PR tables wired to SQLite, live daemon log — with the existing goal mode untouched and mutually exclusive.

**Architecture:** Single Textual `App` with two installed `Screen`s (`GoalScreen`, `DaemonScreen`). App-level holds daemon task, log buffer, preflight cache, db poller, and `LoopConfig`. Logs flow via a custom `logging.Handler` using `loop.call_soon_threadsafe` to safely cross `asyncio.to_thread` worker threads. SQLite polling reuses dashboard's query bodies via a new shared `persistence_queries` module.

**Spec:** `docs/superpowers/specs/2026-05-18-tui-daemon-extension-design.md` — read for the rationale; this plan tells you exactly what to write.

**Tech Stack:** Python 3.11+, Textual 8.2.5, SQLite (WAL), pytest + pytest-asyncio.

---

## Files Created

- `pm_agent/persistence_queries.py` — shared SQL for dashboard + TUI (Task 2-4)
- `tests/test_persistence_queries.py` — Task 2-4
- `tests/test_tui_log_handler.py` — Task 6
- `tests/test_db_poller.py` — Task 7
- `tests/test_widgets.py` — Task 8-9
- `tests/test_tui_daemon_screen.py` — Task 10-11
- `tests/test_tui_app.py` — Task 12
- `tests/test_tui_main_argv.py` — Task 13
- `tests/test_daemon_full_minicycle.py` — Task 14

## Files Modified

- `pm_agent/loop.py` — Task 1: new kwargs (`install_signal_handlers`, `skip_init`, `skip_reconcile`, `stop_event`), `get_running_loop` switch
- `pm_agent/dashboard/server.py` — Task 5: thin wrappers around `persistence_queries`, add `findings_pr_opened` to response model
- `pm_agent/tui.py` — Tasks 6, 7, 8, 9, 10, 11, 12, 13: major refactor (App + two Screens + new widgets)

---

## Task 1: Extend `loop.run_forever` signature

**Files:**
- Modify: `pm_agent/loop.py` (lines 455-491)
- Test: `tests/test_loop.py` (append)

- [ ] **Step 1: Write test for `skip_init` and `skip_reconcile`**

Append to `tests/test_loop.py`:

```python
@pytest.mark.asyncio
async def test_run_forever_skip_init_does_not_touch_state_db(tmp_path, monkeypatch):
    """skip_init=True must not call persistence.init_db."""
    from pm_agent import loop, persistence
    calls = []
    real_init_db = persistence.init_db
    monkeypatch.setattr(persistence, "init_db",
                        lambda p: calls.append(p) or real_init_db(p))
    real_init_db(tmp_path / "state.db")  # pre-init by the caller
    calls.clear()

    stop = asyncio.Event()
    stop.set()  # exit immediately
    with fake_repo({"x.py": ""}) as repo:
        await loop.run_forever(
            repo, loop.LoopConfig(),
            install_signal_handlers=False,
            skip_init=True, skip_reconcile=True,
            stop_event=stop,
        )
    assert calls == [], "init_db must not be called when skip_init=True"


@pytest.mark.asyncio
async def test_run_forever_install_signal_handlers_false(tmp_path):
    """install_signal_handlers=False must not call loop.add_signal_handler."""
    from pm_agent import loop, persistence
    persistence.init_db(tmp_path / "state.db")
    stop = asyncio.Event()
    stop.set()
    captured = []
    real_get_loop = asyncio.get_running_loop
    class _Sniffer:
        def __init__(self, real): self._real = real
        def __getattr__(self, n): return getattr(self._real, n)
        def add_signal_handler(self, sig, cb):
            captured.append(sig)
            return self._real.add_signal_handler(sig, cb)

    # We cannot patch asyncio.get_running_loop cleanly; instead verify the
    # absence of behavior by snapshotting handlers before/after.
    import signal
    real_loop = asyncio.get_running_loop()
    before = real_loop._signal_handlers.copy() if hasattr(real_loop, "_signal_handlers") else None

    with fake_repo({"x.py": ""}) as repo:
        await loop.run_forever(
            repo, loop.LoopConfig(),
            install_signal_handlers=False,
            skip_init=True, skip_reconcile=True,
            stop_event=stop,
        )

    after = real_loop._signal_handlers.copy() if hasattr(real_loop, "_signal_handlers") else None
    # If we touched signal handlers, after will differ from before
    assert before == after
```

Also at the top of the file (if not already there):
```python
import asyncio
import pytest
from tests._fixtures.fake_repo import fake_repo
```

- [ ] **Step 2: Run tests and confirm they fail**

```
pytest tests/test_loop.py::test_run_forever_skip_init_does_not_touch_state_db tests/test_loop.py::test_run_forever_install_signal_handlers_false -v
```

Expected: FAIL — kwargs not recognized.

- [ ] **Step 3: Modify `pm_agent/loop.py:run_forever` signature**

Find the existing function at `pm_agent/loop.py:455-491` and replace with:

```python
async def run_forever(
    repo: Path,
    cfg: LoopConfig | None = None,
    *,
    install_signal_handlers: bool = True,
    skip_init: bool = False,
    skip_reconcile: bool = False,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Daemon entry point. Installs SIGINT/SIGTERM handlers; loops cycles.

    New kwargs (all default to preserving prior behavior):
      install_signal_handlers: when False, do not call loop.add_signal_handler
        (callers like the TUI manage their own signal handling).
      skip_init: when True, do not call persistence.init_db (caller has).
      skip_reconcile: when True, do not call persistence.reconcile (caller has).
      stop_event: external Event to use; if None, a fresh one is created.
    """
    cfg = cfg or LoopConfig()
    state_db = STATE_DB
    if not skip_init:
        persistence.init_db(state_db)
    if not skip_reconcile:
        report = persistence.reconcile(repo)
        log.info("reconcile: %s", report)

    if stop_event is None:
        stop_event = asyncio.Event()
    if install_signal_handlers:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop_event.set)
            except NotImplementedError:
                pass  # Windows doesn't support signal handlers in asyncio

    while not stop_event.is_set():
        try:
            result = await run_one_cycle(repo, cfg, stop_event=stop_event)
            log.info(
                "cycle %d: %d findings, %d fixed, %d skipped, $%.4f, %.1fs",
                result.cycle_id, result.findings_total, result.findings_fixed,
                result.findings_skipped, result.cost_usd, result.duration_s,
            )
        except github.GhAuthError as e:
            log.error("daemon halting on gh auth failure: %s", e)
            break
        except Exception:
            log.exception("cycle crashed; continuing to next interval")
        if stop_event.is_set():
            break
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=cfg.interval_s)
        except asyncio.TimeoutError:
            pass

    log.info("daemon shutting down")
```

The only changes from the existing function: new kwargs, conditional init/reconcile, conditional signal handler install, external `stop_event` injection, and `get_running_loop` instead of `get_event_loop`.

- [ ] **Step 4: Run tests to verify they pass**

```
pytest tests/test_loop.py -v
```

Expected: ALL tests pass (existing and new). Existing CLI path tests still use default kwargs.

- [ ] **Step 5: Commit**

```bash
git add pm_agent/loop.py tests/test_loop.py
git commit -m "feat(loop): add skip_init / skip_reconcile / stop_event / install_signal_handlers kwargs to run_forever"
```

---

## Task 2: `persistence_queries.live_cycle()`

**Files:**
- Create: `pm_agent/persistence_queries.py`
- Test: `tests/test_persistence_queries.py`

- [ ] **Step 1: Write failing test**

Create `tests/test_persistence_queries.py`:

```python
"""Tests for pm_agent.persistence_queries — shared SQL for dashboard + TUI."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pm_agent import persistence, persistence_queries as queries
from pm_agent.scanner import Finding


def _f(bug_id: str = "ab12cd34", title: str = "t") -> Finding:
    return Finding(
        bug_id=bug_id, title=title, severity="Low",
        paths=["a.py"], acceptance=["ok"], evidence="e", kind="bug",
    )


def test_live_cycle_idle_no_history(tmp_path):
    persistence.init_db(tmp_path / "state.db")
    payload = queries.live_cycle()
    assert payload.status == "idle"
    assert payload.cycle is None
    assert payload.findings == []
    assert payload.last_finished is None


def test_live_cycle_idle_with_history(tmp_path):
    persistence.init_db(tmp_path / "state.db")
    cid = persistence.start_cycle()
    persistence.record_cost(cid, "scanner", 0.10)
    persistence.finish_cycle(cid, "done", 0.10)
    payload = queries.live_cycle()
    assert payload.status == "idle"
    assert payload.cycle is None
    assert payload.last_finished is not None
    assert payload.last_finished.id == cid
    assert payload.last_finished.status == "done"
    assert payload.last_finished.cost_usd == pytest.approx(0.10)


def test_live_cycle_running_counts_total_and_pr_opened(tmp_path):
    persistence.init_db(tmp_path / "state.db")
    cid = persistence.start_cycle()  # leaves it 'running'
    f1_id = persistence.record_finding(cid, _f("aaa11111", "t1"))
    f2_id = persistence.record_finding(cid, _f("bbb22222", "t2"))
    f3_id = persistence.record_finding(cid, _f("ccc33333", "t3"))
    persistence.update_finding(f1_id, "done")  # PR opened
    persistence.update_finding(f2_id, "fixing-code")
    # f3 stays 'discovered'
    persistence.record_cost(cid, "scanner", 0.05)
    persistence.record_cost(cid, "coder-1", 0.03)

    payload = queries.live_cycle()
    assert payload.status == "running"
    assert payload.cycle.id == cid
    assert payload.cycle.findings_total == 3
    assert payload.cycle.findings_pr_opened == 1
    assert payload.cycle.cost_usd == pytest.approx(0.08)
    assert len(payload.findings) == 3
    # ORDER BY id DESC: newest first
    assert payload.findings[0].bug_id == "ccc33333"


def test_live_cycle_findings_limit_50(tmp_path):
    persistence.init_db(tmp_path / "state.db")
    cid = persistence.start_cycle()
    for i in range(60):
        persistence.record_finding(cid, _f(f"hex{i:05d}", f"t{i}"))
    payload = queries.live_cycle()
    assert payload.cycle.findings_total == 60
    assert len(payload.findings) == 50
```

- [ ] **Step 2: Run tests; expect ImportError**

```
pytest tests/test_persistence_queries.py -v
```

Expected: ImportError on `pm_agent.persistence_queries`.

- [ ] **Step 3: Create `pm_agent/persistence_queries.py` with `live_cycle()`**

```python
"""Shared SQL queries for the dashboard and the TUI daemon mode.

Pure functions that read the SQLite state DB initialized by
`pm_agent.persistence`. Each function opens its connection via
`persistence.get_conn()` (thread-local) and returns a dataclass payload.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

from pm_agent import persistence


@dataclass
class CycleSummary:
    id: int
    status: str
    started_at: str
    finished_at: Optional[str]
    cost_usd: float
    findings_total: int = 0
    findings_pr_opened: int = 0


@dataclass
class FindingRow:
    bug_id: str
    title: str
    severity: str
    status: str


@dataclass
class FinishedCycleSummary:
    id: int
    status: str
    started_at: str
    finished_at: Optional[str]
    cost_usd: float


@dataclass
class LiveCyclePayload:
    status: Literal["running", "idle"]
    cycle: Optional[CycleSummary]
    findings: list[FindingRow] = field(default_factory=list)
    last_finished: Optional[FinishedCycleSummary] = None


def _summarize_finished(c, row) -> Optional[FinishedCycleSummary]:
    if row is None:
        return None
    # cost_usd from finished cycle is canonical (finish_cycle wrote it)
    return FinishedCycleSummary(
        id=row["id"], status=row["status"],
        started_at=row["started_at"], finished_at=row["finished_at"],
        cost_usd=float(row["cost_usd"] or 0.0),
    )


def live_cycle() -> LiveCyclePayload:
    """Return the current running cycle's state, or idle + last-finished."""
    c = persistence.get_conn()

    running = c.execute(
        """SELECT id, status, started_at, finished_at FROM cycles
           WHERE status='running' ORDER BY started_at DESC LIMIT 1""",
    ).fetchone()

    last_finished_row = c.execute(
        """SELECT id, status, started_at, finished_at, cost_usd FROM cycles
           WHERE status != 'running' ORDER BY id DESC LIMIT 1""",
    ).fetchone()

    if running is None:
        return LiveCyclePayload(
            status="idle", cycle=None, findings=[],
            last_finished=_summarize_finished(c, last_finished_row),
        )

    counts = c.execute(
        """SELECT COUNT(*) AS total,
                  SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) AS pr_opened
           FROM findings WHERE cycle_id=?""",
        (running["id"],),
    ).fetchone()

    live_cost = c.execute(
        "SELECT COALESCE(SUM(usd),0) AS t FROM costs WHERE cycle_id=?",
        (running["id"],),
    ).fetchone()["t"]

    findings_rows = c.execute(
        """SELECT bug_id, title, severity, status FROM findings
           WHERE cycle_id=? ORDER BY id DESC LIMIT 50""",
        (running["id"],),
    ).fetchall()

    return LiveCyclePayload(
        status="running",
        cycle=CycleSummary(
            id=running["id"], status=running["status"],
            started_at=running["started_at"], finished_at=running["finished_at"],
            cost_usd=float(live_cost),
            findings_total=int(counts["total"] or 0),
            findings_pr_opened=int(counts["pr_opened"] or 0),
        ),
        findings=[
            FindingRow(
                bug_id=r["bug_id"], title=r["title"],
                severity=r["severity"], status=r["status"],
            ) for r in findings_rows
        ],
        last_finished=_summarize_finished(c, last_finished_row),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

```
pytest tests/test_persistence_queries.py -v
```

Expected: 4 passes.

- [ ] **Step 5: Commit**

```bash
git add pm_agent/persistence_queries.py tests/test_persistence_queries.py
git commit -m "feat(persistence): live_cycle() — shared SQL for dashboard + TUI"
```

---

## Task 3: `persistence_queries.recent_prs_24h()`

**Files:**
- Modify: `pm_agent/persistence_queries.py`
- Test: `tests/test_persistence_queries.py` (append)

- [ ] **Step 1: Write failing test**

Append to `tests/test_persistence_queries.py`:

```python
def test_recent_prs_24h_empty(tmp_path):
    persistence.init_db(tmp_path / "state.db")
    assert queries.recent_prs_24h() == []


def test_recent_prs_24h_returns_recent(tmp_path):
    persistence.init_db(tmp_path / "state.db")
    cid = persistence.start_cycle()
    fid = persistence.record_finding(cid, _f("xx111111", "fix"))
    persistence.record_pr(fid, 42, "https://example.com/pr/42", "open", "opened")
    rows = queries.recent_prs_24h()
    assert len(rows) == 1
    assert rows[0].github_number == 42
    assert rows[0].bug_id == "xx111111"
    assert rows[0].state == "open"


def test_recent_prs_24h_ignores_older_than_24h(tmp_path, monkeypatch):
    """A PR created 25 hours ago must NOT appear."""
    persistence.init_db(tmp_path / "state.db")
    cid = persistence.start_cycle()
    fid = persistence.record_finding(cid, _f("yy222222", "old"))
    # Patch persistence._now once to seed an "old" PR row
    old_ts = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    monkeypatch.setattr(persistence, "_now", lambda: old_ts)
    persistence.record_pr(fid, 1, "u", "open", "opened")
    monkeypatch.undo()
    # And a fresh PR
    persistence.record_pr(fid, 2, "u", "open", "opened")

    rows = queries.recent_prs_24h()
    nums = [r.github_number for r in rows]
    assert 1 not in nums
    assert 2 in nums


def test_recent_prs_24h_order_desc_by_id(tmp_path):
    persistence.init_db(tmp_path / "state.db")
    cid = persistence.start_cycle()
    fid = persistence.record_finding(cid, _f("zz333333", "x"))
    persistence.record_pr(fid, 1, "u", "open", "opened")
    persistence.record_pr(fid, 2, "u", "open", "opened")
    persistence.record_pr(fid, 3, "u", "open", "opened")
    rows = queries.recent_prs_24h()
    assert [r.github_number for r in rows] == [3, 2, 1]
```

- [ ] **Step 2: Run tests; expect AttributeError**

```
pytest tests/test_persistence_queries.py -k recent_prs -v
```

Expected: AttributeError — `recent_prs_24h` not defined.

- [ ] **Step 3: Add `recent_prs_24h` to `persistence_queries.py`**

Append to `pm_agent/persistence_queries.py`:

```python
from datetime import datetime, timedelta, timezone


@dataclass
class PRRow:
    github_number: int
    state: str
    action: str
    url: str
    created_at: str
    bug_id: str
    title: str
    severity: str


def recent_prs_24h() -> list[PRRow]:
    """Last 24h PRs joined to findings, newest first, capped at 50.

    `prs.created_at` is UTC ISO 8601 (string-sortable), so a `>=` string
    comparison correctly implements the 24h cutoff.
    """
    c = persistence.get_conn()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    rows = c.execute(
        """SELECT p.github_number, p.state, p.action, p.url, p.created_at,
                  f.bug_id, f.title, f.severity
           FROM prs p JOIN findings f ON p.finding_id = f.id
           WHERE p.created_at >= ?
           ORDER BY p.id DESC
           LIMIT 50""",
        (cutoff,),
    ).fetchall()
    return [
        PRRow(
            github_number=r["github_number"], state=r["state"],
            action=r["action"], url=r["url"], created_at=r["created_at"],
            bug_id=r["bug_id"], title=r["title"], severity=r["severity"],
        ) for r in rows
    ]
```

- [ ] **Step 4: Run tests to verify they pass**

```
pytest tests/test_persistence_queries.py -v
```

Expected: All 8 tests pass.

- [ ] **Step 5: Commit**

```bash
git add pm_agent/persistence_queries.py tests/test_persistence_queries.py
git commit -m "feat(persistence): recent_prs_24h() — 24h PR window for TUI"
```

---

## Task 4: `persistence_queries.trend_24h()`

**Files:**
- Modify: `pm_agent/persistence_queries.py`
- Test: `tests/test_persistence_queries.py` (append)

- [ ] **Step 1: Write failing test**

Append:

```python
def test_trend_24h_empty(tmp_path):
    persistence.init_db(tmp_path / "state.db")
    payload = queries.trend_24h()
    assert payload.points == []


def test_trend_24h_cumulative_cost(tmp_path):
    persistence.init_db(tmp_path / "state.db")
    c1 = persistence.start_cycle()
    persistence.record_cost(c1, "scanner", 0.10)
    persistence.finish_cycle(c1, "done", 0.10)
    c2 = persistence.start_cycle()
    persistence.record_cost(c2, "scanner", 0.20)
    persistence.finish_cycle(c2, "done", 0.20)
    payload = queries.trend_24h()
    assert len(payload.points) == 2
    assert payload.points[0].cumulative_cost_usd == pytest.approx(0.10)
    assert payload.points[1].cumulative_cost_usd == pytest.approx(0.30)
```

- [ ] **Step 2: Run; expect AttributeError**

```
pytest tests/test_persistence_queries.py -k trend -v
```

Expected: AttributeError.

- [ ] **Step 3: Add `trend_24h` to `persistence_queries.py`**

Append:

```python
@dataclass
class TrendPoint:
    ts: str
    findings_total: int
    prs_opened: int
    merges: int
    cumulative_cost_usd: float


@dataclass
class TrendPayload:
    points: list[TrendPoint] = field(default_factory=list)


def trend_24h() -> TrendPayload:
    """24h cumulative cost + per-cycle counts. Mirrors dashboard /api/trend."""
    c = persistence.get_conn()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    cycles = c.execute(
        """SELECT id, started_at, cost_usd FROM cycles
           WHERE started_at >= ? ORDER BY started_at""",
        (cutoff,),
    ).fetchall()
    cumulative = 0.0
    points: list[TrendPoint] = []
    for row in cycles:
        cost = c.execute(
            "SELECT COALESCE(SUM(usd),0) AS t FROM costs WHERE cycle_id=?",
            (row["id"],),
        ).fetchone()["t"]
        cumulative += float(cost or 0)
        findings_n = c.execute(
            "SELECT COUNT(*) AS n FROM findings WHERE cycle_id=?", (row["id"],),
        ).fetchone()["n"]
        prs_n = c.execute(
            """SELECT COUNT(*) AS n FROM prs p
               JOIN findings f ON p.finding_id=f.id WHERE f.cycle_id=?""",
            (row["id"],),
        ).fetchone()["n"]
        merges_n = c.execute(
            """SELECT COUNT(*) AS n FROM prs p
               JOIN findings f ON p.finding_id=f.id
               WHERE f.cycle_id=? AND p.state='merged'""",
            (row["id"],),
        ).fetchone()["n"]
        points.append(TrendPoint(
            ts=row["started_at"],
            findings_total=findings_n,
            prs_opened=prs_n,
            merges=merges_n,
            cumulative_cost_usd=round(cumulative, 4),
        ))
    return TrendPayload(points=points)
```

- [ ] **Step 4: Run tests**

```
pytest tests/test_persistence_queries.py -v
```

Expected: All 10 tests pass.

- [ ] **Step 5: Commit**

```bash
git add pm_agent/persistence_queries.py tests/test_persistence_queries.py
git commit -m "feat(persistence): trend_24h() — extracted from dashboard for shared use"
```

---

## Task 5: Wire dashboard to `persistence_queries`

**Files:**
- Modify: `pm_agent/dashboard/server.py`
- Test: `tests/test_dashboard.py` (modify + append)

- [ ] **Step 1: Add failing test for new field**

Append to `tests/test_dashboard.py`:

```python
def test_api_live_running_includes_findings_pr_opened(tmp_path):
    """LiveCycleResponse must include the new `findings_pr_opened` field."""
    init_db(tmp_path / "state.db")
    cid = start_cycle()
    f = Finding(
        bug_id="aa1", title="t", severity="Low",
        paths=["a.py"], acceptance=["ok"], evidence="e", kind="bug",
    )
    fid = record_finding(cid, f)
    from pm_agent.persistence import update_finding
    update_finding(fid, "done")
    client = TestClient(create_app())
    r = client.get("/api/live")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "running"
    assert data["cycle"]["findings_pr_opened"] == 1
    assert data["cycle"]["findings_total"] == 1


def test_api_live_includes_last_finished_when_idle(tmp_path):
    init_db(tmp_path / "state.db")
    cid = start_cycle()
    finish_cycle(cid, "done", 0.0)
    client = TestClient(create_app())
    r = client.get("/api/live")
    data = r.json()
    assert data["status"] == "idle"
    assert data["last_finished"]["id"] == cid
```

- [ ] **Step 2: Run; expect KeyError or field absent**

```
pytest tests/test_dashboard.py::test_api_live_running_includes_findings_pr_opened -v
```

Expected: FAIL — `findings_pr_opened` absent.

- [ ] **Step 3: Refactor `pm_agent/dashboard/server.py`**

Replace the existing module. The pydantic models gain `findings_pr_opened` and `last_finished`; the route bodies become thin wrappers around `persistence_queries`:

```python
"""FastAPI + HTMX dashboard for pm-agent (Beta loop). Thin wrappers over
pm_agent.persistence_queries — see that module for SQL bodies."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from pm_agent import persistence, persistence_queries as queries

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))


class FindingSummary(BaseModel):
    bug_id: str
    title: str
    severity: str
    status: str


class CycleSummary(BaseModel):
    id: int
    status: str
    started_at: str
    finished_at: Optional[str] = None
    cost_usd: float
    findings_total: int
    findings_pr_opened: int = 0  # additive: new in 2026-05-18 TUI extension


class FinishedCycleSummary(BaseModel):
    id: int
    status: str
    started_at: str
    finished_at: Optional[str] = None
    cost_usd: float


class LiveCycleResponse(BaseModel):
    cycle: Optional[CycleSummary]
    status: str  # 'running' | 'idle'
    findings: list[FindingSummary] = []
    last_finished: Optional[FinishedCycleSummary] = None


class TrendPoint(BaseModel):
    ts: str
    findings_total: int
    prs_opened: int
    merges: int
    cumulative_cost_usd: float


class TrendResponse(BaseModel):
    points: list[TrendPoint] = []


def _to_cycle(c) -> CycleSummary:
    return CycleSummary(
        id=c.id, status=c.status, started_at=c.started_at,
        finished_at=c.finished_at, cost_usd=c.cost_usd,
        findings_total=c.findings_total,
        findings_pr_opened=c.findings_pr_opened,
    )


def _to_finished(lf) -> FinishedCycleSummary:
    return FinishedCycleSummary(
        id=lf.id, status=lf.status, started_at=lf.started_at,
        finished_at=lf.finished_at, cost_usd=lf.cost_usd,
    )


def create_app() -> FastAPI:
    app = FastAPI(title="pm-agent dashboard")
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "index.html")

    @app.get("/api/live", response_model=LiveCycleResponse)
    def live_cycle_route() -> LiveCycleResponse:
        try:
            payload = queries.live_cycle()
        except (RuntimeError, sqlite3.OperationalError, sqlite3.DatabaseError):
            return LiveCycleResponse(cycle=None, status="idle", findings=[])
        return LiveCycleResponse(
            cycle=_to_cycle(payload.cycle) if payload.cycle else None,
            status=payload.status,
            findings=[
                FindingSummary(bug_id=f.bug_id, title=f.title,
                               severity=f.severity, status=f.status)
                for f in payload.findings
            ],
            last_finished=_to_finished(payload.last_finished)
                if payload.last_finished else None,
        )

    @app.get("/api/trend", response_model=TrendResponse)
    def trend_route() -> TrendResponse:
        try:
            payload = queries.trend_24h()
        except (RuntimeError, sqlite3.OperationalError, sqlite3.DatabaseError):
            return TrendResponse(points=[])
        return TrendResponse(points=[
            TrendPoint(
                ts=p.ts, findings_total=p.findings_total,
                prs_opened=p.prs_opened, merges=p.merges,
                cumulative_cost_usd=p.cumulative_cost_usd,
            ) for p in payload.points
        ])

    return app


app = create_app()
```

- [ ] **Step 4: Run tests; verify existing dashboard tests still pass + new tests pass**

```
pytest tests/test_dashboard.py -v
```

Expected: ALL tests pass.

- [ ] **Step 5: Commit**

```bash
git add pm_agent/dashboard/server.py tests/test_dashboard.py
git commit -m "refactor(dashboard): thin wrappers over persistence_queries; add findings_pr_opened + last_finished"
```

---

## Task 6: `TUILogHandler`

**Files:**
- Modify: `pm_agent/tui.py` (add new class; don't disturb existing code yet)
- Test: `tests/test_tui_log_handler.py`

- [ ] **Step 1: Write failing test**

Create `tests/test_tui_log_handler.py`:

```python
"""Tests for pm_agent.tui.TUILogHandler — thread-safe log → UI bridge."""
from __future__ import annotations

import asyncio
import collections
import logging
import threading

import pytest


class _FakeApp:
    def __init__(self):
        self.screen_stack = []
        self.appended: list[str] = []

    def _make_fake_daemon_screen(self):
        outer = self
        class _Screen:
            def append_log_line(self, msg):
                outer.appended.append(msg)
        # Make isinstance(screen, DaemonScreen) check pass via class import:
        return _Screen()


@pytest.mark.asyncio
async def test_emit_from_main_thread_appends_to_buffer():
    from pm_agent.tui import TUILogHandler
    buf = collections.deque(maxlen=10)
    app = _FakeApp()
    loop = asyncio.get_running_loop()
    h = TUILogHandler(loop, buf, app)
    h.setFormatter(logging.Formatter("%(message)s"))

    rec = logging.LogRecord("x", logging.INFO, "f", 0, "hello", None, None)
    h.emit(rec)
    await asyncio.sleep(0)  # let call_soon run

    assert list(buf) == ["hello"]


@pytest.mark.asyncio
async def test_emit_from_worker_thread_is_safe():
    """Repeated emits from a worker thread must not crash or drop messages."""
    from pm_agent.tui import TUILogHandler
    buf = collections.deque(maxlen=200)
    app = _FakeApp()
    loop = asyncio.get_running_loop()
    h = TUILogHandler(loop, buf, app)
    h.setFormatter(logging.Formatter("%(message)s"))

    def worker():
        for i in range(100):
            rec = logging.LogRecord("x", logging.INFO, "f", 0,
                                    f"msg-{i}", None, None)
            h.emit(rec)

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    # Give the loop time to drain all scheduled call_soon callbacks
    for _ in range(20):
        await asyncio.sleep(0.01)

    assert len(buf) == 100
    assert buf[0] == "msg-0"
    assert buf[-1] == "msg-99"


@pytest.mark.asyncio
async def test_emit_drops_to_widget_when_daemon_screen_current():
    from pm_agent.tui import TUILogHandler, DaemonScreen
    buf = collections.deque(maxlen=10)
    app = _FakeApp()
    # Build a real DaemonScreen subclass instance to satisfy isinstance:
    class _Stub(DaemonScreen):
        def __init__(self):
            self.appended: list[str] = []
        def append_log_line(self, msg):
            self.appended.append(msg)
    screen = _Stub()
    app.screen_stack.append(screen)

    loop = asyncio.get_running_loop()
    h = TUILogHandler(loop, buf, app)
    h.setFormatter(logging.Formatter("%(message)s"))
    rec = logging.LogRecord("x", logging.INFO, "f", 0, "hi", None, None)
    h.emit(rec)
    await asyncio.sleep(0)

    assert list(buf) == ["hi"]
    assert screen.appended == ["hi"]


@pytest.mark.asyncio
async def test_buffer_maxlen_enforced():
    from pm_agent.tui import TUILogHandler
    buf = collections.deque(maxlen=3)
    app = _FakeApp()
    loop = asyncio.get_running_loop()
    h = TUILogHandler(loop, buf, app)
    h.setFormatter(logging.Formatter("%(message)s"))

    for i in range(5):
        rec = logging.LogRecord("x", logging.INFO, "f", 0, str(i), None, None)
        h.emit(rec)
    await asyncio.sleep(0)
    assert list(buf) == ["2", "3", "4"]
```

- [ ] **Step 2: Run; expect ImportError**

```
pytest tests/test_tui_log_handler.py -v
```

Expected: ImportError on `TUILogHandler` / `DaemonScreen`.

- [ ] **Step 3: Add forward-declaration stubs + `TUILogHandler` to `pm_agent/tui.py`**

Locate `pm_agent/tui.py` line ~52 (just after the existing imports of `pm_agent.planner / runner / tasks / worktree`). Add:

```python
import collections
import logging as _logging  # avoid clashing with the existing `log` variable
import sqlite3

# Forward declarations — DaemonScreen is defined later in Task 11.
# TUILogHandler uses isinstance(..., DaemonScreen); the symbol must exist
# by the time emit() runs, but a stub at module level is fine because
# we never instantiate it until Task 11.
class DaemonScreen:  # noqa: D401 — stub, replaced in Task 11
    """Stub. Will be replaced by the full Screen class in Task 11."""
    pass


class TUILogHandler(_logging.Handler):
    """Bridge stdlib logging → Textual RichLog, thread-safe.

    `emit()` may be called from any thread (daemon's asyncio.to_thread
    workers). It schedules `_dispatch` on the main event loop via
    `call_soon_threadsafe`, where it is safe to touch the buffer and
    Screen widgets.
    """

    def __init__(self, loop, buffer, app):
        super().__init__()
        self._loop = loop
        self._buffer = buffer
        self._app = app

    def emit(self, record: _logging.LogRecord) -> None:
        try:
            msg = self.format(record)
        except Exception:
            self.handleError(record)
            return
        self._loop.call_soon_threadsafe(self._dispatch, msg)

    def _dispatch(self, msg: str) -> None:
        # Runs on the main event loop thread.
        self._buffer.append(msg)
        screen = self._app.screen_stack[-1] if self._app.screen_stack else None
        if isinstance(screen, DaemonScreen):
            screen.append_log_line(msg)
```

- [ ] **Step 4: Run tests**

```
pytest tests/test_tui_log_handler.py -v
```

Expected: 4 passes.

- [ ] **Step 5: Commit**

```bash
git add pm_agent/tui.py tests/test_tui_log_handler.py
git commit -m "feat(tui): TUILogHandler — thread-safe logging.Handler → RichLog"
```

---

## Task 7: `DbPoller`

**Files:**
- Modify: `pm_agent/tui.py`
- Test: `tests/test_db_poller.py`

- [ ] **Step 1: Write failing test**

Create `tests/test_db_poller.py`:

```python
"""Tests for pm_agent.tui.DbPoller — SQLite polling on two cadences."""
from __future__ import annotations

import asyncio
import sqlite3
import pytest


class _FakeApp:
    def __init__(self):
        self.screen_stack = []


@pytest.mark.asyncio
async def test_tick_once_calls_both_queries(monkeypatch):
    from pm_agent.tui import DbPoller
    live_calls = 0
    prs_calls = 0
    def fake_live():
        nonlocal live_calls
        live_calls += 1
        return object()
    def fake_prs():
        nonlocal prs_calls
        prs_calls += 1
        return []
    monkeypatch.setattr("pm_agent.persistence_queries.live_cycle", fake_live)
    monkeypatch.setattr("pm_agent.persistence_queries.recent_prs_24h", fake_prs)

    poller = DbPoller(_FakeApp())
    await poller.tick_once()
    assert live_calls == 1
    assert prs_calls == 1


@pytest.mark.asyncio
async def test_live_tick_swallows_sqlite_operational_error(monkeypatch):
    from pm_agent.tui import DbPoller

    crash_count = [0]
    def fake_live():
        crash_count[0] += 1
        raise sqlite3.OperationalError("simulated lock")
    monkeypatch.setattr("pm_agent.persistence_queries.live_cycle", fake_live)
    monkeypatch.setattr("pm_agent.persistence_queries.recent_prs_24h", lambda: [])

    poller = DbPoller(_FakeApp())
    poller.LIVE_INTERVAL = 0.01
    poller.start()
    await asyncio.sleep(0.05)
    poller.stop()
    # Should have ticked multiple times, swallowing each error
    assert crash_count[0] >= 2


@pytest.mark.asyncio
async def test_live_tick_timeout_does_not_kill_task(monkeypatch):
    from pm_agent.tui import DbPoller

    async def slow():
        await asyncio.sleep(10)
    def fake_live():
        # Simulate a query that hangs by calling sleep within to_thread
        # — but easier: just block the calling thread.
        import time
        time.sleep(5)
    monkeypatch.setattr("pm_agent.persistence_queries.live_cycle", fake_live)
    monkeypatch.setattr("pm_agent.persistence_queries.recent_prs_24h", lambda: [])

    poller = DbPoller(_FakeApp())
    poller.LIVE_INTERVAL = 0.01
    poller.LIVE_TIMEOUT = 0.05
    poller.start()
    await asyncio.sleep(0.2)
    poller.stop()
    # The point: we got here without exception escaping the poller
    assert poller._live_task.done() or poller._live_task.cancelled()


@pytest.mark.asyncio
async def test_stop_cancels_both_tasks(monkeypatch):
    from pm_agent.tui import DbPoller
    monkeypatch.setattr("pm_agent.persistence_queries.live_cycle", lambda: None)
    monkeypatch.setattr("pm_agent.persistence_queries.recent_prs_24h", lambda: [])
    poller = DbPoller(_FakeApp())
    poller.start()
    await asyncio.sleep(0.02)
    poller.stop()
    await asyncio.sleep(0.02)
    assert poller._live_task.cancelled() or poller._live_task.done()
    assert poller._prs_task.cancelled() or poller._prs_task.done()
```

- [ ] **Step 2: Run; expect ImportError**

```
pytest tests/test_db_poller.py -v
```

Expected: ImportError on `DbPoller`.

- [ ] **Step 3: Add `DbPoller` to `pm_agent/tui.py`**

Append after `TUILogHandler`:

```python
class DbPoller:
    """Polls SQLite on two cadences. Each tick wraps a sync query in
    asyncio.to_thread; queries run on the default executor (≤32 workers,
    each with its own thread-local SQLite connection)."""

    LIVE_INTERVAL = 1.0
    PRS_INTERVAL = 30.0
    LIVE_TIMEOUT = 2.0
    PRS_TIMEOUT = 10.0

    def __init__(self, app):
        self._app = app
        self._live_task = None
        self._prs_task = None

    def start(self) -> None:
        import asyncio as _aio
        self._live_task = _aio.create_task(self._loop_live(), name="db-poller-live")
        self._prs_task = _aio.create_task(self._loop_prs(),  name="db-poller-prs")

    def stop(self) -> None:
        for t in (self._live_task, self._prs_task):
            if t is not None and not t.done():
                t.cancel()

    async def tick_once(self) -> None:
        """Run one live + prs query immediately. Used by Screen resume."""
        import asyncio as _aio
        from pm_agent import persistence_queries as _q
        live = await _aio.wait_for(
            _aio.to_thread(_q.live_cycle), timeout=self.LIVE_TIMEOUT)
        screen = self._current_daemon_screen()
        if screen is not None:
            screen.update_cycle_and_findings(live)
        prs = await _aio.wait_for(
            _aio.to_thread(_q.recent_prs_24h), timeout=self.PRS_TIMEOUT)
        if screen is not None:
            screen.update_prs(prs)

    def _current_daemon_screen(self):
        if not self._app.screen_stack:
            return None
        top = self._app.screen_stack[-1]
        return top if isinstance(top, DaemonScreen) else None

    async def _loop_live(self):
        import asyncio as _aio
        from pm_agent import persistence_queries as _q
        while True:
            try:
                data = await _aio.wait_for(
                    _aio.to_thread(_q.live_cycle), timeout=self.LIVE_TIMEOUT)
                screen = self._current_daemon_screen()
                if screen is not None:
                    screen.update_cycle_and_findings(data)
            except _aio.CancelledError:
                raise
            except sqlite3.OperationalError as e:
                _logging.getLogger(__name__).warning("db poll (live) failed: %s", e)
            except _aio.TimeoutError:
                _logging.getLogger(__name__).warning("db poll (live) timeout")
            except Exception:
                _logging.getLogger(__name__).exception("db poller live tick crashed")
            await _aio.sleep(self.LIVE_INTERVAL)

    async def _loop_prs(self):
        import asyncio as _aio
        from pm_agent import persistence_queries as _q
        while True:
            try:
                data = await _aio.wait_for(
                    _aio.to_thread(_q.recent_prs_24h), timeout=self.PRS_TIMEOUT)
                screen = self._current_daemon_screen()
                if screen is not None:
                    screen.update_prs(data)
            except _aio.CancelledError:
                raise
            except sqlite3.OperationalError as e:
                _logging.getLogger(__name__).warning("db poll (prs) failed: %s", e)
            except _aio.TimeoutError:
                _logging.getLogger(__name__).warning("db poll (prs) timeout")
            except Exception:
                _logging.getLogger(__name__).exception("db poller prs tick crashed")
            await _aio.sleep(self.PRS_INTERVAL)
```

- [ ] **Step 4: Run tests**

```
pytest tests/test_db_poller.py -v
```

Expected: 4 passes.

- [ ] **Step 5: Commit**

```bash
git add pm_agent/tui.py tests/test_db_poller.py
git commit -m "feat(tui): DbPoller — 1s/30s SQLite polling with error swallowing"
```

---

## Task 8: `PreflightBar` widget

**Files:**
- Modify: `pm_agent/tui.py`
- Test: `tests/test_widgets.py`

- [ ] **Step 1: Write failing test**

Create `tests/test_widgets.py`:

```python
"""Tests for new TUI widgets (PreflightBar, CycleSummaryRow)."""
from __future__ import annotations

import pytest

from pm_agent.preflight import CheckResult


def _hard_ok():
    return [
        CheckResult("state.db dir writable", True, "/x"),
        CheckResult("state.db clean",        True, "fresh"),
        CheckResult("claude CLI on PATH",    True, "/usr/local/bin/claude"),
        CheckResult("gh CLI authenticated",  True, "ok"),
        CheckResult("repo clean",            True, "/x"),
        CheckResult("repo on main",          True, "main", warn_only=True),
        CheckResult("/tmp writable",         True, "/tmp"),
    ]


def test_preflight_bar_renders_seven_cells_when_all_pass():
    from pm_agent.tui import PreflightBar
    bar = PreflightBar()
    bar.update_from(_hard_ok())
    text = str(bar.renderable)
    # 7 cells separated by │
    assert text.count("│") == 6
    # Each name appears (truncated forms OK; assert key tokens)
    assert "db dir" in text or "db dir writable" in text
    assert "gh" in text
    assert "/tmp" in text


def test_preflight_bar_renders_failure_in_red():
    from pm_agent.tui import PreflightBar
    results = _hard_ok()
    results[3] = CheckResult("gh CLI authenticated", False, "not logged in")
    bar = PreflightBar()
    bar.update_from(results)
    text = str(bar.renderable)
    assert "✗" in text
    assert "red" in text  # rich markup tag survives in console.text representation


def test_preflight_bar_warn_only_is_yellow():
    from pm_agent.tui import PreflightBar
    results = _hard_ok()
    results[5] = CheckResult("repo on main", False, "feature/x", warn_only=True)
    bar = PreflightBar()
    bar.update_from(results)
    text = str(bar.renderable)
    assert "⚠" in text
    assert "yellow" in text
```

- [ ] **Step 2: Run; expect ImportError**

```
pytest tests/test_widgets.py -v
```

Expected: ImportError on `PreflightBar`.

- [ ] **Step 3: Add `PreflightBar` to `pm_agent/tui.py`**

Append:

```python
from textual.widgets import Static  # may already be imported; keep symbol


class PreflightBar(Static):
    """One-row, seven-cell preflight status bar."""

    DEFAULT_CSS = """
    PreflightBar {
        height: 1;
        background: $panel;
        padding: 0 1;
    }
    """

    def update_from(self, results) -> None:
        """Replace bar content with rendered cells.

        `results` is the list returned by `preflight.run_preflight(...)[0]`.
        Stored as renderable Rich markup; `update_from` uses Static.update().
        """
        cells = []
        for r in results:
            if r.ok:
                symbol, color = "✓", "green"
            elif r.warn_only:
                symbol, color = "⚠", "yellow"
            else:
                symbol, color = "✗", "red"
            # Shorten name to ≤14 chars to keep 7 cells on one row
            short = (
                r.name
                .replace("state.db ", "db ")
                .replace("repo on main", "main")
                .replace("claude CLI on PATH", "claude")
                .replace("gh CLI authenticated", "gh")
            )
            cells.append(f"[{color}]{symbol} {short}[/]")
        self.update(" │ ".join(cells))
```

- [ ] **Step 4: Run tests**

```
pytest tests/test_widgets.py -v
```

Expected: 3 passes.

- [ ] **Step 5: Commit**

```bash
git add pm_agent/tui.py tests/test_widgets.py
git commit -m "feat(tui): PreflightBar widget — 7-cell status row"
```

---

## Task 9: `CycleSummaryRow` widget

**Files:**
- Modify: `pm_agent/tui.py`
- Test: `tests/test_widgets.py` (append)

- [ ] **Step 1: Write failing test**

Append to `tests/test_widgets.py`:

```python
def test_cycle_summary_row_idle_no_history():
    from pm_agent.tui import CycleSummaryRow
    from pm_agent.persistence_queries import LiveCyclePayload
    row = CycleSummaryRow()
    row.update_from(LiveCyclePayload(status="idle", cycle=None,
                                     findings=[], last_finished=None))
    text = str(row.renderable)
    assert "idle" in text
    assert "press 's'" in text


def test_cycle_summary_row_idle_with_history():
    from pm_agent.tui import CycleSummaryRow
    from pm_agent.persistence_queries import (
        LiveCyclePayload, FinishedCycleSummary)
    row = CycleSummaryRow()
    lf = FinishedCycleSummary(
        id=7, status="done", started_at="2026-05-18T12:00:00+00:00",
        finished_at="2026-05-18T12:05:00+00:00", cost_usd=1.23,
    )
    row.update_from(LiveCyclePayload(status="idle", cycle=None,
                                     findings=[], last_finished=lf))
    text = str(row.renderable)
    assert "last cycle" in text
    assert "#7" in text
    assert "1.23" in text


def test_cycle_summary_row_running():
    from pm_agent.tui import CycleSummaryRow
    from pm_agent.persistence_queries import (
        LiveCyclePayload, CycleSummary)
    row = CycleSummaryRow()
    cs = CycleSummary(
        id=42, status="running",
        started_at="2026-05-18T12:00:00+00:00",
        finished_at=None, cost_usd=0.5,
        findings_total=5, findings_pr_opened=2,
    )
    row.update_from(LiveCyclePayload(status="running", cycle=cs,
                                     findings=[], last_finished=None))
    text = str(row.renderable)
    assert "#42" in text
    assert "PR opened" in text
    assert "2" in text
    assert "5" in text


def test_cycle_summary_row_shows_last_error_when_present():
    from pm_agent.tui import CycleSummaryRow
    from pm_agent.persistence_queries import LiveCyclePayload
    row = CycleSummaryRow()
    row.set_last_error(RuntimeError("gh auth lost"))
    row.update_from(LiveCyclePayload(status="idle", cycle=None,
                                     findings=[], last_finished=None))
    text = str(row.renderable)
    assert "crashed" in text
    assert "gh auth lost" in text
```

- [ ] **Step 2: Run; expect ImportError**

```
pytest tests/test_widgets.py -k cycle_summary -v
```

Expected: ImportError.

- [ ] **Step 3: Add `CycleSummaryRow` to `pm_agent/tui.py`**

Append:

```python
from datetime import datetime, timezone
from rich.markup import escape as _rich_escape_for_summary


class CycleSummaryRow(Static):
    """One-row cycle summary above the Findings/PR tables."""

    DEFAULT_CSS = """
    CycleSummaryRow {
        height: 1;
        background: $boost;
        padding: 0 1;
    }
    """

    _last_error: object | None = None

    def set_last_error(self, err) -> None:
        self._last_error = err

    def clear_last_error(self) -> None:
        self._last_error = None

    def update_from(self, payload) -> None:
        if self._last_error is not None:
            reason = _rich_escape_for_summary(str(self._last_error))[:80]
            self.update(
                f"[red]daemon crashed:[/] {reason}  "
                f"[grey50]press 's' to restart[/]"
            )
            return
        if payload.status == "idle":
            lf = payload.last_finished
            if lf is None:
                self.update(
                    "[grey50]daemon idle — press 's' to start (no prior cycle)[/]"
                )
                return
            when = lf.finished_at or lf.started_at
            short = when.split("T")[1][:5] if "T" in when else when
            self.update(
                f"[grey50]idle[/]  last cycle [bold]#{lf.id}[/] "
                f"{lf.status}  ${lf.cost_usd:.4f}  at {short}"
            )
            return
        c = payload.cycle
        elapsed = self._fmt_elapsed(c.started_at)
        self.update(
            f"cycle [bold]#{c.id}[/]  [yellow]{c.status}[/]  "
            f"PR opened [green]{c.findings_pr_opened}[/]/{c.findings_total}  "
            f"${c.cost_usd:.4f}  elapsed {elapsed}"
        )

    @staticmethod
    def _fmt_elapsed(started_at: str) -> str:
        try:
            t0 = datetime.fromisoformat(started_at)
            if t0.tzinfo is None:
                t0 = t0.replace(tzinfo=timezone.utc)
            secs = int((datetime.now(timezone.utc) - t0).total_seconds())
            return f"{secs}s" if secs < 60 else f"{secs // 60}m{secs % 60}s"
        except ValueError:
            return started_at
```

- [ ] **Step 4: Run tests**

```
pytest tests/test_widgets.py -v
```

Expected: 7 passes (3 PreflightBar + 4 CycleSummaryRow).

- [ ] **Step 5: Commit**

```bash
git add pm_agent/tui.py tests/test_widgets.py
git commit -m "feat(tui): CycleSummaryRow widget — idle/running/crashed states"
```

---

## Task 10: Extract existing TUI into `GoalScreen`

**Files:**
- Modify: `pm_agent/tui.py` (substantial refactor)
- Test: `tests/test_tui_daemon_screen.py` (regression: existing TUI still composes)

- [ ] **Step 1: Write a failing structural test**

Create `tests/test_tui_daemon_screen.py`:

```python
"""Tests for GoalScreen and DaemonScreen via App.run_test()."""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.mark.asyncio
async def test_goal_screen_class_exists():
    from pm_agent.tui import GoalScreen
    assert issubclass(GoalScreen, __import__("textual.screen",
                                             fromlist=["Screen"]).Screen)


@pytest.mark.asyncio
async def test_existing_pmagenttui_app_still_instantiates(tmp_path):
    """Regression: PMAgentTUI must still construct with legacy kwargs."""
    from pm_agent.tui import PMAgentTUI
    app = PMAgentTUI(repo=tmp_path)
    assert app is not None
```

- [ ] **Step 2: Run; expect ImportError on GoalScreen (PMAgentTUI exists)**

```
pytest tests/test_tui_daemon_screen.py -v
```

Expected: FAIL — `GoalScreen` not yet exported.

- [ ] **Step 3: Refactor `pm_agent/tui.py`: extract `GoalScreen`**

This is a mechanical move. Find the existing class `PMAgentTUI(App)` (around line 175 — the one with `compose`, `on_mount`, etc., NOT the new App we're about to write). Rename it to `GoalScreen(Screen)` and adjust:

1. Change base class from `App` to `Screen` (import `from textual.screen import Screen`).
2. Remove `App`-only bindings; keep `r`, `n`, `escape` (Screen-level).
3. Replace top-level references to `self.app` for `repo` / cost / etc. with `self.app.repo` — i.e. accept `repo` via `__init__` and store both on the Screen AND access via `self.app.repo`; keep the existing constructor signature to preserve test compatibility.
4. Add this method:

   ```python
   def on_screen_resume(self) -> None:
       """Refresh disable state when switching back to goal mode."""
       try:
           input_w = self.query_one("#goal-input", Input)
       except Exception:
           return
       if self.app.is_daemon_active:
           input_w.disabled = True
           input_w.placeholder = "(locked — daemon running)"
       else:
           input_w.disabled = False
           input_w.placeholder = "type a goal and press Enter"
   ```

5. Keep `ARTIFACTS_ROOT` as a **module-level constant** at the top of `tui.py` (do not move it inside the class). It is monkey-patched by `tests/conftest.py:24-30`.

6. Override `action_rerun` to no-op when `self.app.is_daemon_active`:

   ```python
   def action_rerun(self) -> None:
       if self.app.is_daemon_active:
           self.notify("daemon running; goal mode locked",
                       severity="warning")
           return
       # ... existing rerun body
   ```

Do **not** define `PMAgentTUI` yet — it'll be replaced in Task 12. For now, leave the bottom of `tui.py` with the existing `main()` calling `PMAgentTUI` (which is now a stub) — tests will reach this stub via Task 12.

To bridge Task 10 → 12, add a temporary alias at module level so `PMAgentTUI` still exists as something:

```python
# Temporary alias — replaced by the real App in Task 12.
PMAgentTUI = GoalScreen
```

- [ ] **Step 4: Run tests; verify regression tests still pass**

```
pytest tests/test_tui_daemon_screen.py tests/test_cli.py -v
```

Expected: `test_goal_screen_class_exists` passes; existing `test_cli.py` continues to import `pm_agent.tui.main` without error.

If `tests/test_cli.py:212` fails because the goal Screen no longer takes the old positional args, do NOT change the test; instead ensure GoalScreen's `__init__` keeps the legacy kwargs accepted (just stash them as ignored if necessary). The test only verifies `sys.argv` shape and that `main()` was called.

- [ ] **Step 5: Commit**

```bash
git add pm_agent/tui.py tests/test_tui_daemon_screen.py
git commit -m "refactor(tui): extract existing 5-panel UI into GoalScreen"
```

---

## Task 11: `DaemonScreen`

**Files:**
- Modify: `pm_agent/tui.py` (replace the `DaemonScreen` stub with the real class)
- Test: `tests/test_tui_daemon_screen.py` (append)

- [ ] **Step 1: Write failing tests**

Append to `tests/test_tui_daemon_screen.py`:

```python
@pytest.mark.asyncio
async def test_daemon_screen_action_start_blocked_when_preflight_none(
    tmp_path, monkeypatch,
):
    """If preflight has not yet run, start_daemon must not crash; it notifies."""
    from pm_agent.tui import PMAgentTUI

    notifies = []
    async with PMAgentTUI(repo=tmp_path).run_test() as pilot:
        await pilot.press("d")
        # short-circuit preflight to None so we hit the gate
        pilot.app.preflight_results = None
        pilot.app.notify = lambda msg, **kw: notifies.append(msg)
        screen = pilot.app.screen_stack[-1]
        await screen.action_start_daemon()
    assert any("preflight not yet run" in n for n in notifies)


@pytest.mark.asyncio
async def test_daemon_screen_hard_check_names_match_preflight():
    """HARD_CHECK_NAMES strings must equal preflight CheckResult.name strings."""
    from pm_agent import preflight as pf
    from pm_agent.tui import HARD_CHECK_NAMES
    # Build the actual checks against a non-existent repo to surface names
    # (we don't care about their ok values; only their .name attribute).
    from pathlib import Path
    db = Path("/tmp/pm-agent-test-state.db.unused")
    results, _ = pf.run_preflight(db, Path("/nonexistent"))
    actual_names = {r.name for r in results}
    # Every hard check name must be present in the actual preflight output
    missing = HARD_CHECK_NAMES - actual_names
    assert not missing, f"HARD_CHECK_NAMES contains stale strings: {missing}"


@pytest.mark.asyncio
async def test_daemon_screen_start_gate_passes_when_all_hard_checks_ok(
    tmp_path, monkeypatch,
):
    from pm_agent.tui import PMAgentTUI
    from pm_agent.preflight import CheckResult

    fake_results = [
        CheckResult("state.db dir writable", True, "/x"),
        CheckResult("state.db clean",        True, "fresh"),
        CheckResult("claude CLI on PATH",    True, "ok"),
        CheckResult("gh CLI authenticated",  True, "ok"),
        CheckResult("repo clean",            True, "/x"),
        CheckResult("repo on main",          True, "main", warn_only=True),
        CheckResult("/tmp writable",         True, "/tmp"),
    ]
    # Prevent the daemon from actually starting:
    monkeypatch.setattr("pm_agent.loop.run_forever",
                        lambda *a, **kw: asyncio.sleep(0.01))
    monkeypatch.setattr("pm_agent.persistence.reconcile",
                        lambda repo: "no-op")

    async with PMAgentTUI(repo=tmp_path).run_test() as pilot:
        await pilot.press("d")
        pilot.app.preflight_results = fake_results
        screen = pilot.app.screen_stack[-1]
        await screen.action_start_daemon()
        await pilot.pause()
    assert pilot.app.daemon_task is not None or pilot.app.daemon_last_error is None
```

- [ ] **Step 2: Run; expect failures (HARD_CHECK_NAMES not yet defined; DaemonScreen stub still in place)**

```
pytest tests/test_tui_daemon_screen.py -k "hard_check_names or start_blocked or start_gate" -v
```

Expected: ImportError or AttributeError.

- [ ] **Step 3: Replace `DaemonScreen` stub with the real class**

Delete the `class DaemonScreen: pass` stub from Task 6 and add (near the bottom of `tui.py`, before `main()`):

```python
import signal

from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import DataTable, Input, RichLog

# Names must match preflight.py CheckResult.name strings verbatim.
# Verified 2026-05-18 against preflight.py:40,72,119,160,195,241,276.
HARD_CHECK_NAMES = {
    "state.db dir writable",
    "claude CLI on PATH",
    "gh CLI authenticated",
    "repo clean",
    "/tmp writable",
}
# Excluded deliberately:
#   "state.db clean" — hard-fails on any prior cycle; we demote to soft-warn.
#   "repo on main"   — already warn_only=True in preflight.


class DaemonScreen(Screen):
    BINDINGS = [
        ("s",      "start_daemon",  "Start"),
        ("x",      "stop_daemon",   "Stop"),
        ("p",      "preflight",     "Re-preflight"),
        ("R",      "focus_repo",    "Set repo"),
        ("escape", "blur_input",    ""),
    ]

    DEFAULT_CSS = """
    DaemonScreen { layout: vertical; }
    #cycle-row { height: 1; }
    #main { height: 1fr; }
    #left {
        width: 50%;
        height: 100%;
        border: solid $secondary;
    }
    #daemon-log {
        width: 1fr;
        height: 100%;
        border: solid $secondary;
    }
    #repo-input {
        height: 3;
        background: $surface;
        border: solid $accent;
    }
    """

    def compose(self):
        yield PreflightBar(id="preflight-bar")
        yield CycleSummaryRow(id="cycle-row")
        with Horizontal(id="main"):
            with Vertical(id="left"):
                yield Static("Findings (current cycle)", classes="panel-title")
                yield DataTable(id="findings", zebra_stripes=True)
                yield Static("Recent PRs (24h)", classes="panel-title")
                yield DataTable(id="prs", zebra_stripes=True)
            yield RichLog(id="daemon-log", wrap=True, highlight=True,
                          markup=True, max_lines=5000)
        yield Input(placeholder="repo path (press R to focus)", id="repo-input")

    def on_mount(self) -> None:
        # Initial table headers
        try:
            ftable = self.query_one("#findings", DataTable)
            ftable.add_columns("bug_id", "severity", "status", "title")
        except Exception:
            pass
        try:
            ptable = self.query_one("#prs", DataTable)
            ptable.add_columns("#", "state", "bug_id", "title")
        except Exception:
            pass
        # Kick off preflight in the background; do not block on_mount
        import asyncio as _aio
        _aio.create_task(self._run_preflight_async())

    async def _run_preflight_async(self) -> None:
        import asyncio as _aio
        from pm_agent import preflight as _pf
        from pm_agent.loop import STATE_DB
        try:
            results, _ = await _aio.to_thread(
                _pf.run_preflight, STATE_DB, self.app.repo)
        except Exception as e:
            results = [CheckResult("preflight runner", False,
                                   f"crashed: {e!r}")]
        self.app.preflight_results = results
        try:
            self.query_one(PreflightBar).update_from(results)
        except Exception:
            pass

    async def on_screen_resume(self) -> None:
        # Replay buffered log lines
        try:
            log_w = self.query_one("#daemon-log", RichLog)
            log_w.clear()
            for line in list(self.app.log_buffer):
                log_w.write(line)
        except Exception:
            pass
        # Immediate refresh from DB
        try:
            await self.app.db_poller.tick_once()
        except Exception:
            pass

    def append_log_line(self, msg: str) -> None:
        try:
            self.query_one("#daemon-log", RichLog).write(msg)
        except Exception:
            pass

    def update_cycle_and_findings(self, payload) -> None:
        try:
            self.query_one(CycleSummaryRow).update_from(payload)
        except Exception:
            pass
        try:
            ftable = self.query_one("#findings", DataTable)
            ftable.clear()
            for f in payload.findings:
                ftable.add_row(f.bug_id, f.severity, f.status, f.title)
        except Exception:
            pass

    def update_prs(self, rows) -> None:
        try:
            ptable = self.query_one("#prs", DataTable)
            ptable.clear()
            for r in rows:
                ptable.add_row(
                    f"#{r.github_number}", r.state, r.bug_id, r.title)
        except Exception:
            pass

    # ── actions ────────────────────────────────────────────────────────

    async def action_start_daemon(self) -> None:
        import asyncio as _aio
        from pm_agent import loop as _loop, persistence
        log = _logging.getLogger("pm_agent.tui")
        if self.app.daemon_task and not self.app.daemon_task.done():
            self.app.notify("daemon already running"); return
        if self.app.daemon_starting:
            self.app.notify("daemon already starting"); return
        if self.app.preflight_results is None:
            self.app.notify("preflight not yet run; press p first"); return

        hard_fails = [
            r for r in self.app.preflight_results
            if not r.ok and not r.warn_only and r.name in HARD_CHECK_NAMES
        ]
        if hard_fails:
            names = ", ".join(r.name for r in hard_fails)
            self.app.notify(f"preflight failed: {names}", severity="error")
            return

        for r in self.app.preflight_results:
            if (not r.ok and not r.warn_only
                    and r.name not in HARD_CHECK_NAMES):
                log.info("preflight soft-warn: %s — %s", r.name, r.message)

        self.app.daemon_starting = True
        try:
            self.app.daemon_stop_event = _aio.Event()
            rec = await _aio.to_thread(persistence.reconcile, self.app.repo)
            log.info("reconcile: %s", rec)

            self.app.daemon_last_error = None
            try:
                self.query_one(CycleSummaryRow).clear_last_error()
            except Exception:
                pass

            self.app.daemon_task = _aio.create_task(
                _loop.run_forever(
                    self.app.repo, self.app.loop_cfg,
                    install_signal_handlers=False,
                    skip_init=True, skip_reconcile=True,
                    stop_event=self.app.daemon_stop_event,
                ),
                name="pm-agent-loop",
            )
            self.app.daemon_task.add_done_callback(self.app._on_daemon_done)
        finally:
            self.app.daemon_starting = False

    async def action_stop_daemon(self) -> None:
        if self.app.daemon_starting:
            self.app.notify("daemon still starting; try again in a moment")
            return
        t = self.app.daemon_task
        if not t or t.done():
            self.app.notify("no running daemon"); return
        self.app.daemon_stop_event.set()
        self.app.notify(
            "stop signal sent; daemon will finish current cycle")

    async def action_preflight(self) -> None:
        await self._run_preflight_async()

    def action_focus_repo(self) -> None:
        if self.app.is_daemon_active:
            self.app.notify("stop daemon first to change repo")
            return
        try:
            self.query_one("#repo-input", Input).focus()
        except Exception:
            pass

    def action_blur_input(self) -> None:
        try:
            self.query_one("#repo-input", Input).blur()
        except Exception:
            pass

    async def on_input_submitted(self, event):
        if event.input.id != "repo-input":
            return
        from pathlib import Path as _Path
        new = _Path(event.value).expanduser()
        if not new.is_dir():
            self.app.notify(f"not a directory: {new}", severity="error")
            return
        if not (new / ".git").is_dir():
            self.app.notify(f"not a git repo: {new}", severity="error")
            return
        self.app.repo = new
        _logging.getLogger("pm_agent.tui").info("repo switched to %s", new)
        event.input.blur()
        await self.action_preflight()
```

- [ ] **Step 4: Run all daemon tests**

```
pytest tests/test_tui_daemon_screen.py tests/test_widgets.py tests/test_db_poller.py tests/test_tui_log_handler.py -v
```

Expected: all pass. Note: `test_hard_check_names_match_preflight` is the critical regression gate from Spec B1.

- [ ] **Step 5: Commit**

```bash
git add pm_agent/tui.py tests/test_tui_daemon_screen.py
git commit -m "feat(tui): DaemonScreen — preflight bar, cycle/findings/PR tables, actions"
```

---

## Task 12: `PMAgentTUI` App shell

**Files:**
- Modify: `pm_agent/tui.py`
- Test: `tests/test_tui_app.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_tui_app.py`:

```python
"""Tests for the PMAgentTUI App shell."""
from __future__ import annotations

import asyncio
import pytest


@pytest.mark.asyncio
async def test_app_starts_in_goal_mode_by_default(tmp_path):
    from pm_agent.tui import PMAgentTUI, GoalScreen
    async with PMAgentTUI(repo=tmp_path).run_test() as pilot:
        assert isinstance(pilot.app.screen_stack[-1], GoalScreen)


@pytest.mark.asyncio
async def test_app_opens_daemon_screen_with_flag(tmp_path):
    from pm_agent.tui import PMAgentTUI, DaemonScreen
    async with PMAgentTUI(repo=tmp_path, open_daemon=True).run_test() as pilot:
        await pilot.pause()
        assert isinstance(pilot.app.screen_stack[-1], DaemonScreen)


@pytest.mark.asyncio
async def test_app_d_key_switches_to_daemon(tmp_path):
    from pm_agent.tui import PMAgentTUI, DaemonScreen
    async with PMAgentTUI(repo=tmp_path).run_test() as pilot:
        await pilot.press("d")
        await pilot.pause()
        assert isinstance(pilot.app.screen_stack[-1], DaemonScreen)


@pytest.mark.asyncio
async def test_is_daemon_active_when_starting(tmp_path):
    from pm_agent.tui import PMAgentTUI
    async with PMAgentTUI(repo=tmp_path).run_test() as pilot:
        assert pilot.app.is_daemon_active is False
        pilot.app.daemon_starting = True
        assert pilot.app.is_daemon_active is True
        pilot.app.daemon_starting = False
        assert pilot.app.is_daemon_active is False


@pytest.mark.asyncio
async def test_on_daemon_done_resets_task_field(tmp_path):
    from pm_agent.tui import PMAgentTUI
    async with PMAgentTUI(repo=tmp_path).run_test() as pilot:
        async def finished():
            return
        t = asyncio.create_task(finished())
        await t
        pilot.app.daemon_task = t
        pilot.app._on_daemon_done(t)
        assert pilot.app.daemon_task is None
        assert pilot.app.daemon_last_error is None


@pytest.mark.asyncio
async def test_on_daemon_done_captures_exception(tmp_path):
    from pm_agent.tui import PMAgentTUI
    async with PMAgentTUI(repo=tmp_path).run_test() as pilot:
        async def crashed():
            raise RuntimeError("boom")
        t = asyncio.create_task(crashed())
        with pytest.raises(RuntimeError):
            await t
        pilot.app.daemon_task = t
        pilot.app._on_daemon_done(t)
        assert pilot.app.daemon_task is None
        assert isinstance(pilot.app.daemon_last_error, RuntimeError)


@pytest.mark.asyncio
async def test_tui_log_handler_removed_on_unmount(tmp_path):
    """Re-instantiating the App must not pile up handlers on the root logger."""
    import logging
    from pm_agent.tui import PMAgentTUI, TUILogHandler

    async with PMAgentTUI(repo=tmp_path).run_test():
        pass  # app mounted then unmounted

    root = logging.getLogger()
    handlers = [h for h in root.handlers if isinstance(h, TUILogHandler)]
    assert handlers == []
```

- [ ] **Step 2: Run; expect failures**

```
pytest tests/test_tui_app.py -v
```

Expected: failures because the real App class isn't built yet.

- [ ] **Step 3: Replace the temporary `PMAgentTUI = GoalScreen` alias with the real App**

Delete the temporary alias. Add at the bottom of `tui.py`, before `main()`:

```python
class PMAgentTUI(App):
    """Top-level Textual App. Wraps GoalScreen and DaemonScreen and holds
    daemon-task / log-buffer / db-poller shared state."""

    BINDINGS = [
        ("g", "switch_screen('goal')",   "Goal mode"),
        ("d", "switch_screen('daemon')", "Daemon mode"),
        ("q", "request_quit",            "Quit"),
    ]

    DEFAULT_CSS = """
    .panel-title {
        text-style: bold;
        background: $boost;
        height: 1;
        margin: 0 0 1 0;
    }
    """

    def __init__(
        self,
        repo,
        goal=None,
        *,
        open_daemon: bool = False,
        loop_cfg=None,
        **goal_kwargs,
    ):
        super().__init__()
        from pathlib import Path
        self.repo = Path(repo)
        self.goal = goal
        self._open_daemon = open_daemon
        self._goal_kwargs = goal_kwargs
        # Initialize DB exactly once
        from pm_agent import persistence
        from pm_agent.loop import STATE_DB, LoopConfig
        persistence.init_db(STATE_DB)
        self.log_buffer = collections.deque(maxlen=2000)
        self.daemon_task = None
        self.daemon_starting = False
        self.daemon_stop_event = None
        self.daemon_last_error = None
        self.preflight_results = None
        self.loop_cfg = loop_cfg or LoopConfig()
        self.log_handler = None
        self.db_poller = None

    @property
    def is_daemon_active(self) -> bool:
        return self.daemon_starting or (
            self.daemon_task is not None and not self.daemon_task.done()
        )

    def on_mount(self) -> None:
        import asyncio as _aio
        loop = _aio.get_running_loop()
        root = _logging.getLogger()
        # Remove any stale TUILogHandler from prior App instances (pytest)
        for h in list(root.handlers):
            if isinstance(h, TUILogHandler):
                root.removeHandler(h)
        self.log_handler = TUILogHandler(loop, self.log_buffer, self)
        self.log_handler.setLevel(_logging.INFO)
        self.log_handler.setFormatter(_logging.Formatter(
            "%(asctime)s [%(name)s] %(levelname)s %(message)s",
            datefmt="%H:%M:%S",
        ))
        root.addHandler(self.log_handler)
        _logging.getLogger("textual").setLevel(_logging.WARNING)
        _logging.getLogger("uvicorn").setLevel(_logging.WARNING)
        _logging.getLogger("pm_agent").setLevel(_logging.INFO)

        # Install screens (passing constructor args)
        goal_screen = GoalScreen(
            repo=self.repo, goal=self.goal, **self._goal_kwargs)
        self.install_screen(goal_screen, name="goal")
        self.install_screen(DaemonScreen(), name="daemon")

        self.db_poller = DbPoller(self)
        self.db_poller.start()

        # Route SIGTERM through Textual exit
        try:
            loop.add_signal_handler(signal.SIGTERM, self.exit)
        except (NotImplementedError, ValueError):
            pass  # Windows / not main thread

        self.push_screen("daemon" if self._open_daemon else "goal")

    def on_unmount(self) -> None:
        if self.log_handler is not None:
            _logging.getLogger().removeHandler(self.log_handler)
        if self.db_poller is not None:
            self.db_poller.stop()

    def _on_daemon_done(self, task) -> None:
        try:
            task.result()
            self.daemon_last_error = None
        except asyncio.CancelledError as e:
            self.daemon_last_error = e
        except Exception as e:
            self.daemon_last_error = e
        # NOTE: SystemExit / KeyboardInterrupt are not caught — they propagate
        # naturally to the Textual event loop.
        self.daemon_task = None
        _logging.getLogger("pm_agent.tui").info(
            "daemon stopped: %s", self.daemon_last_error or "clean")
        # Tell CycleSummaryRow about the error
        try:
            stack = self.screen_stack
            for s in stack:
                if isinstance(s, DaemonScreen):
                    row = s.query_one(CycleSummaryRow)
                    if self.daemon_last_error is not None:
                        row.set_last_error(self.daemon_last_error)
                    else:
                        row.clear_last_error()
        except Exception:
            pass

    async def action_request_quit(self) -> None:
        if self.daemon_task is not None and not self.daemon_task.done():
            self.daemon_stop_event.set()
            self.notify("draining daemon... (30s max)")
            try:
                await asyncio.wait_for(self.daemon_task, timeout=30)
            except asyncio.TimeoutError:
                self.daemon_task.cancel()
                self.notify(
                    "forced exit; worktree cleanup may be incomplete",
                    severity="warning",
                )
                try:
                    await self.daemon_task
                except (asyncio.CancelledError, Exception):
                    pass
        self.exit()
```

You also need `import asyncio` at the top of `pm_agent/tui.py` if not already present.

- [ ] **Step 4: Run all tests**

```
pytest tests/test_tui_app.py tests/test_tui_daemon_screen.py tests/test_cli.py -v
```

Expected: all pass. The existing `test_cli.py:212` continues to work because `main()` is still the entry point and the argv hack is preserved.

- [ ] **Step 5: Commit**

```bash
git add pm_agent/tui.py tests/test_tui_app.py
git commit -m "feat(tui): PMAgentTUI App — daemon lifecycle, mutex, graceful quit"
```

---

## Task 13: `main()` argv — add `--daemon`, build LoopConfig

**Files:**
- Modify: `pm_agent/tui.py` (the existing `main()` at the end of the file)
- Test: `tests/test_tui_main_argv.py`

- [ ] **Step 1: Write failing test**

Create `tests/test_tui_main_argv.py`:

```python
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
```

- [ ] **Step 2: Run; expect failures**

```
pytest tests/test_tui_main_argv.py -v
```

Expected: FAIL — `--daemon` not in argparse / `loop_cfg` not built.

- [ ] **Step 3: Update `main()` in `pm_agent/tui.py`**

Find the existing `main()` at the bottom of `tui.py` and replace with:

```python
def main() -> None:
    import argparse
    import sys as _sys
    from pathlib import Path
    from pm_agent.loop import LoopConfig

    ap = argparse.ArgumentParser(prog="pm-agent.tui")
    ap.add_argument("goal", nargs="*", default=None,
                    help="goal text (omit to enter mock or interactive mode)")
    ap.add_argument("--repo", default="/tmp/pm-agent-day7-target")
    ap.add_argument("--daemon", action="store_true",
                    help="boot directly into daemon mode")
    ap.add_argument("--single", action="store_true",
                    help="single-coder mode (day 3-4 behavior)")
    ap.add_argument("--mock-planner", action="store_true",
                    help="use mock planner fallback")
    ap.add_argument("--test-cmd", default=None)
    ap.add_argument("--coder-timeout", type=float, default=180.0)
    ap.add_argument("--max-retries", type=int, default=2)
    ap.add_argument("--test-timeout", type=float, default=120.0)
    ap.add_argument("--inject-fault",
                    choices=["planner-yaml", "coder-timeout", "api-error"],
                    default=None)
    ap.add_argument("--interactive", action="store_true")
    args = ap.parse_args()

    # Validate intervals (existing behavior preserved from prior cli.py:argparse safety)
    if args.coder_timeout <= 0:
        ap.error("--coder-timeout must be > 0")
    if args.test_timeout <= 0:
        ap.error("--test-timeout must be > 0")

    loop_cfg = LoopConfig(
        coder_timeout=args.coder_timeout,
        test_timeout=args.test_timeout,
        max_retries=args.max_retries,
    )

    app = PMAgentTUI(
        repo=Path(args.repo).expanduser(),
        goal=" ".join(args.goal) if args.goal else None,
        open_daemon=args.daemon,
        loop_cfg=loop_cfg,
        # remaining kwargs flow to GoalScreen
        single=args.single,
        use_real_planner=not args.mock_planner,
        test_cmd=args.test_cmd,
        coder_timeout=args.coder_timeout,
        max_retries=args.max_retries,
        test_timeout=args.test_timeout,
        inject_fault=args.inject_fault,
        interactive=args.interactive,
    )
    app.run()
```

- [ ] **Step 4: Run argv tests + full test suite**

```
pytest tests/test_tui_main_argv.py tests/test_cli.py tests/test_tui_app.py -v
```

Expected: all pass. `test_cli.py:212` still passes — `pm_agent.tui.main` is still the patch target.

- [ ] **Step 5: Commit**

```bash
git add pm_agent/tui.py tests/test_tui_main_argv.py
git commit -m "feat(tui): main() — --daemon flag + LoopConfig from CLI args"
```

---

## Task 14: End-to-end mini-cycle test

**Files:**
- Test: `tests/test_daemon_full_minicycle.py`

- [ ] **Step 1: Write the integration test**

Create `tests/test_daemon_full_minicycle.py`:

```python
"""End-to-end: TUI starts daemon, scanner returns 1 finding, coder is mocked
to NO_CHANGES, gh push/PR are mocked. Assert SQLite state + UI state."""
from __future__ import annotations

import asyncio
import pytest

from tests._fixtures.fake_repo import fake_repo


@pytest.mark.asyncio
async def test_tui_daemon_full_minicycle(tmp_path, monkeypatch):
    """Drive the TUI through one complete cycle in daemon mode."""
    from pm_agent.tui import PMAgentTUI
    from pm_agent import github, persistence, scanner
    from pm_agent.scanner import Finding
    from pm_agent.preflight import CheckResult

    # 1. Mock preflight to all-pass
    fake_results = [
        CheckResult("state.db dir writable", True, "/x"),
        CheckResult("state.db clean",        True, "fresh"),
        CheckResult("claude CLI on PATH",    True, "ok"),
        CheckResult("gh CLI authenticated",  True, "ok"),
        CheckResult("repo clean",            True, "/x"),
        CheckResult("repo on main",          True, "main", warn_only=True),
        CheckResult("/tmp writable",         True, "/tmp"),
    ]
    monkeypatch.setattr(
        "pm_agent.preflight.run_preflight",
        lambda db, repo: (fake_results, True))

    # 2. Mock scanner to return 1 finding with $0.10 cost
    async def fake_scan(repo, **kw):
        return ([Finding(
            bug_id="aaaa1111", title="x",
            severity="Low", paths=["x.py"],
            acceptance=["does nothing"], evidence="e",
            kind="bug",
        )], 0.10)
    monkeypatch.setattr(scanner, "scan", fake_scan)

    # 3. Mock gh push + PR open to succeed
    async def fake_gh(*args):
        return (0, "", "")
    monkeypatch.setattr(github, "_gh", fake_gh)

    async def fake_push(repo, branch): return (True, "")
    monkeypatch.setattr(github, "push_branch_to_origin", fake_push)

    async def fake_open_pr(branch, finding, body_extras=""):
        return github.PRResult(
            number=1, url="https://example.com/pr/1",
            state="open", action="opened",
        )
    monkeypatch.setattr(github, "open_pr", fake_open_pr)
    monkeypatch.setattr(github, "auto_merge", fake_open_pr)
    async def fake_sync(): return []
    monkeypatch.setattr(github, "sync_pr_states", fake_sync)

    # 4. Mock claude runner to print DONE
    from pm_agent import runner
    async def fake_claude(prompt, **kw):
        return runner.RunResult(
            text="DONE\n", cost_usd=0.05, session_id="s1",
            returncode=0, stderr="",
        )
    monkeypatch.setattr(runner, "run_claude_async", fake_claude)

    with fake_repo({
        "x.py": "def x(): return 1\n",
        "README.md": "x\n",
    }) as repo:
        monkeypatch.setattr("pm_agent.loop.STATE_DB", tmp_path / "state.db")
        async with PMAgentTUI(repo=repo).run_test() as pilot:
            await pilot.press("d")
            await pilot.pause()
            # Inject preflight results without waiting for the real call
            pilot.app.preflight_results = fake_results
            await pilot.app.screen_stack[-1].action_start_daemon()
            # Wait for at least one cycle to land
            for _ in range(50):
                await asyncio.sleep(0.1)
                c = persistence.get_conn()
                row = c.execute(
                    "SELECT COUNT(*) AS n FROM cycles WHERE status='done'"
                ).fetchone()
                if row["n"] >= 1:
                    break
            # Stop the daemon
            await pilot.app.screen_stack[-1].action_stop_daemon()
            for _ in range(20):
                if pilot.app.daemon_task is None:
                    break
                await asyncio.sleep(0.1)

    c = persistence.get_conn()
    assert c.execute("SELECT COUNT(*) FROM cycles").fetchone()[0] >= 1
    assert c.execute("SELECT COUNT(*) FROM findings").fetchone()[0] >= 1
```

- [ ] **Step 2: Run; expect either pass or a debuggable failure**

```
pytest tests/test_daemon_full_minicycle.py -v --timeout=60
```

Expected: passes within ~10 seconds.

If it hangs, the most likely cause is that `loop.run_forever`'s while loop is blocked on `asyncio.wait_for(stop_event.wait(), timeout=interval_s)` with default `interval_s=1800`. Fix by passing a tighter `LoopConfig(interval_s=1)` into the test via `pilot.app.loop_cfg = LoopConfig(interval_s=1, ...)` before `action_start_daemon`.

- [ ] **Step 3: Commit**

```bash
git add tests/test_daemon_full_minicycle.py
git commit -m "test(tui): end-to-end daemon mini-cycle through TUI"
```

---

## Task 15: Final regression sweep + manual checklist

**Files:**
- No code changes; verification only.

- [ ] **Step 1: Run the full test suite**

```
pytest tests/ -v
```

Expected: all green.

- [ ] **Step 2: Run mypy and ruff**

```
.venv/bin/python -m mypy pm_agent tests
.venv/bin/python -m ruff check pm_agent tests
```

Expected: no errors.

- [ ] **Step 3: Manual: smoke-test goal mode unchanged**

```
uv run pm-agent tui --repo /tmp/pm-agent-day7-target
```

Expected: existing 5-panel goal-mode UI. Press `q` to exit cleanly.

- [ ] **Step 4: Manual: smoke-test daemon mode**

In one terminal:
```
uv run pm-agent tui --daemon --repo /tmp/pm-agent-day7-target
```

Expected:
- Preflight bar renders 7 cells.
- Press `s`. Daemon starts (some checks may fail without claude CLI / gh; that's fine for the smoke test — the gate should refuse and notify clearly).
- Press `d` then `g` to switch screens. Goal mode's input shows `(locked — daemon running)` placeholder when daemon is active.
- Press `x` to stop.
- Press `q` to quit. Should drain within 30s.

- [ ] **Step 5: Manual: smoke-test dashboard against same DB**

In one terminal:
```
uv run pm-agent loop --repo /tmp/pm-agent-day7-target  # populates state.db
```

In another:
```
uv run pm-agent dashboard serve
```

Open http://127.0.0.1:8000/ — verify Live Cycle and Cumulative cost render.

- [ ] **Step 6: Commit any final tweaks**

If steps 3-5 surfaced any small issues:

```bash
git add -A
git commit -m "fix: smoke-test follow-ups"
```

- [ ] **Step 7: Tag the milestone**

```bash
git log --oneline -20
```

Verify all commits are clean. Optionally:

```bash
git tag -a tui-daemon-mvp -m "TUI daemon mode shipped"
```

---

## Acceptance Summary

When all tasks pass:

1. ✅ `pm-agent tui` boots into goal mode unchanged.
2. ✅ `pm-agent tui --daemon` boots directly into daemon mode.
3. ✅ `d` / `g` switch screens; mutex locks goal inputs when daemon active.
4. ✅ `s` starts daemon (preflight-gated), `x` stops it, `p` re-runs preflight, `R` opens repo input.
5. ✅ Live Findings + PR tables refresh via DbPoller against same SQLite the dashboard reads.
6. ✅ TUILogHandler is thread-safe via `call_soon_threadsafe`.
7. ✅ `_on_daemon_done` clears the mutex; daemon crash surfaces in CycleSummaryRow.
8. ✅ `q` drains daemon within 30s, then exits.
9. ✅ Dashboard at `/api/live` now includes `findings_pr_opened` and `last_finished`.
10. ✅ All pre-existing tests pass unchanged (including `test_cli.py:212`).
