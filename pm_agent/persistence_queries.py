"""Shared SQL queries for the dashboard and the TUI daemon mode.

Pure functions that read the SQLite state DB initialized by
`pm_agent.persistence`. Each function opens its connection via
`persistence.get_conn()` (thread-local) and returns a dataclass payload.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
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
