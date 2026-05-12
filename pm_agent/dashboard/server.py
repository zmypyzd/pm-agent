"""FastAPI dashboard. Polls state.db read-only via HTMX 1s (live) / 30s (trend).

Per spec §3 dashboard endpoints + §7 demo Beat 2 (Live Cycle) / Beat 7 (cost).

The Live Cycle panel sits at the top. Cumulative cost banner shows
prominently with a red color when > $50 — the user's "I should check on
this" signal during a long unattended run.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.requests import Request

from pm_agent import persistence

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))


class CycleSummary(BaseModel):
    id: int
    status: str
    started_at: str
    finished_at: str | None
    cost_usd: float
    findings_total: int


class LiveCycleResponse(BaseModel):
    cycle: CycleSummary | None
    status: Literal["idle", "running", "aborted"]
    findings: list[dict] = []


class TrendPoint(BaseModel):
    ts: str
    findings_total: int
    prs_opened: int
    merges: int
    cumulative_cost_usd: float


class TrendResponse(BaseModel):
    points: list[TrendPoint]


def create_app() -> FastAPI:
    app = FastAPI(title="pm-agent dashboard")
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "index.html")

    @app.get("/api/live", response_model=LiveCycleResponse)
    def live_cycle() -> LiveCycleResponse:
        """HTMX 1s polling. Returns 200 + status='idle' when no running
        cycle — never 404 — so the client polling loop has stable state."""
        try:
            c = persistence.get_conn()
        except RuntimeError:
            return LiveCycleResponse(cycle=None, status="idle", findings=[])
        row = c.execute(
            """SELECT id, status, started_at, finished_at, cost_usd
               FROM cycles WHERE status='running'
               ORDER BY started_at DESC LIMIT 1""",
        ).fetchone()
        if row is None:
            return LiveCycleResponse(cycle=None, status="idle", findings=[])
        n_findings = c.execute(
            "SELECT COUNT(*) AS n FROM findings WHERE cycle_id=?", (row["id"],),
        ).fetchone()["n"]
        findings_rows = c.execute(
            """SELECT bug_id, title, severity, status FROM findings
               WHERE cycle_id=? ORDER BY id""",
            (row["id"],),
        ).fetchall()
        return LiveCycleResponse(
            cycle=CycleSummary(
                id=row["id"], status=row["status"], started_at=row["started_at"],
                finished_at=row["finished_at"], cost_usd=row["cost_usd"],
                findings_total=n_findings,
            ),
            status="running",
            findings=[dict(r) for r in findings_rows],
        )

    @app.get("/api/trend", response_model=TrendResponse)
    def trend_24h() -> TrendResponse:
        """HTMX 30s polling. Returns cumulative-cost time series + per-cycle
        findings/PRs/merges counts for the last 24 hours."""
        try:
            c = persistence.get_conn()
        except RuntimeError:
            return TrendResponse(points=[])
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        cycles = c.execute(
            """SELECT id, started_at, cost_usd FROM cycles
               WHERE started_at >= ? ORDER BY started_at""",
            (cutoff,),
        ).fetchall()
        cumulative = 0.0
        points: list[TrendPoint] = []
        for row in cycles:
            cumulative += float(row["cost_usd"] or 0)
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
        return TrendResponse(points=points)

    return app


# uvicorn entry: pm_agent.dashboard.server:app
app = create_app()
