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
    findings_pr_opened: int = 0


class FinishedCycleSummary(BaseModel):
    id: int
    status: str
    started_at: str
    finished_at: Optional[str] = None
    cost_usd: float


class LiveCycleResponse(BaseModel):
    cycle: Optional[CycleSummary] = None
    status: str
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
