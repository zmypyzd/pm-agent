"""Post-run cycle-health report for pm-agent dry runs.

Called by ``pm-agent loop report``.  All reads are direct sqlite queries —
no persistence module globals are touched (so this is safe to call on an
arbitrary ``--db PATH`` without side-effects).

Column-existence is checked at runtime; missing columns print ``N/A`` instead
of crashing.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _open_readonly(db_path: Path) -> sqlite3.Connection:
    """Open *db_path* read-only (creates file if absent via touch first)."""
    # Touch so sqlite3.connect doesn't silently create a brand-new empty file
    # when the caller passes a non-existent path via --db.
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return any(r["name"] == column for r in rows)
    except Exception:
        return False


def _fmt_duration(seconds: float) -> str:
    h = int(seconds) // 3600
    m = (int(seconds) % 3600) // 60
    return f"{h}h {m}m"


# ---------------------------------------------------------------------------
# Data gathering
# ---------------------------------------------------------------------------

def _gather(conn: sqlite3.Connection) -> dict:  # type: ignore[type-arg]
    data: dict = {}  # type: ignore[type-arg]

    # ------------------------------------------------------------------
    # cycles
    # ------------------------------------------------------------------
    if not _table_exists(conn, "cycles"):
        # Nothing initialised yet — return all-zero/None data.
        data["window_start"] = None
        data["window_end"] = None
        data["duration_s"] = None
        data["cycles_total"] = 0
        data["cycles_ok"] = 0
        data["cycles_errored"] = 0
        data["cycles_aborted"] = 0
        data["cycles_running"] = 0
        data["findings_total"] = 0
        data["findings_bugs"] = 0
        data["findings_tech_debt"] = 0
        data["prs_opened"] = 0
        data["prs_auto_merged"] = 0
        data["prs_merge_failed"] = 0
        data["cost_scanner"] = None
        data["cost_coder"] = None
        return data

    # Window
    row = conn.execute(
        "SELECT MIN(started_at) AS s, MAX(finished_at) AS e FROM cycles"
    ).fetchone()
    data["window_start"] = row["s"]
    data["window_end"] = row["e"]

    # Duration — difference between earliest started_at and latest finished_at
    # Both are ISO-8601 strings; sqlite julianday() handles that natively.
    dur_row = conn.execute(
        """SELECT (julianday(MAX(COALESCE(finished_at, datetime('now'))))
                   - julianday(MIN(started_at))) * 86400.0 AS dur_s
           FROM cycles"""
    ).fetchone()
    data["duration_s"] = dur_row["dur_s"] if dur_row and dur_row["dur_s"] is not None else None

    # Status counts
    status_rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM cycles GROUP BY status"
    ).fetchall()
    status_map = {r["status"]: r["n"] for r in status_rows}
    data["cycles_total"] = sum(status_map.values())
    # "ok" = done + scan-empty (both are clean exits)
    data["cycles_ok"] = status_map.get("done", 0) + status_map.get("scan-empty", 0)
    data["cycles_errored"] = status_map.get("errored", 0)
    data["cycles_aborted"] = status_map.get("aborted", 0)
    data["cycles_running"] = status_map.get("running", 0)

    # ------------------------------------------------------------------
    # findings
    # ------------------------------------------------------------------
    if not _table_exists(conn, "findings"):
        data["findings_total"] = 0
        data["findings_bugs"] = 0
        data["findings_tech_debt"] = 0
    else:
        row2 = conn.execute("SELECT COUNT(*) AS n FROM findings").fetchone()
        data["findings_total"] = row2["n"]
        kind_rows = conn.execute(
            "SELECT kind, COUNT(*) AS n FROM findings GROUP BY kind"
        ).fetchall()
        kind_map = {r["kind"]: r["n"] for r in kind_rows}
        data["findings_bugs"] = kind_map.get("bug", 0)
        data["findings_tech_debt"] = kind_map.get("tech-debt", 0)

    # ------------------------------------------------------------------
    # prs
    # ------------------------------------------------------------------
    if not _table_exists(conn, "prs"):
        data["prs_opened"] = 0
        data["prs_auto_merged"] = 0
        data["prs_merge_failed"] = 0
    else:
        row3 = conn.execute("SELECT COUNT(*) AS n FROM prs").fetchone()
        data["prs_opened"] = row3["n"]
        action_rows = conn.execute(
            "SELECT action, COUNT(*) AS n FROM prs GROUP BY action"
        ).fetchall()
        action_map = {r["action"]: r["n"] for r in action_rows}
        data["prs_auto_merged"] = action_map.get("auto-merged", 0)
        data["prs_merge_failed"] = action_map.get("merge-failed", 0)

    # ------------------------------------------------------------------
    # costs  (optional — columns/table may not exist)
    # ------------------------------------------------------------------
    if not _table_exists(conn, "costs"):
        data["cost_scanner"] = None
        data["cost_coder"] = None
    else:
        has_agent = _column_exists(conn, "costs", "agent")
        has_usd = _column_exists(conn, "costs", "usd")
        if has_agent and has_usd:
            cost_rows = conn.execute(
                "SELECT agent, SUM(usd) AS total FROM costs GROUP BY agent"
            ).fetchall()
            cost_map = {r["agent"]: r["total"] for r in cost_rows}
            data["cost_scanner"] = cost_map.get("scanner")
            # BUG-R4-2: the cycle loop writes per-coder rows with agent
            # names "coder-1" and "coder-2" (see pm_agent.loop calls to
            # record_cost), not the literal "coder". Aggregate any key
            # whose name starts with "coder" so the report adds them all.
            coder_totals = [v for k, v in cost_map.items() if k.startswith("coder")]
            data["cost_coder"] = sum(coder_totals) if coder_totals else None
        else:
            data["cost_scanner"] = None
            data["cost_coder"] = None

    return data


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def _fmt_cost(val: Optional[float]) -> str:
    if val is None:
        return "N/A"
    return f"${val:.2f}"


def format_report(data: dict) -> str:  # type: ignore[type-arg]
    lines: list[str] = []
    lines.append("=== Dry-run report ===")

    # Window
    if data["window_start"] is None:
        lines.append("Window: (no cycles)")
        lines.append("Duration: N/A")
    else:
        end = data["window_end"] or "(in progress)"
        lines.append(f"Window: {data['window_start']} → {end}")
        dur = data["duration_s"]
        lines.append(f"Duration: {_fmt_duration(dur) if dur is not None else 'N/A'}")

    lines.append("")
    lines.append("Cycles:")
    lines.append(f"  total       : {data['cycles_total']}")
    lines.append(f"  ok          : {data['cycles_ok']}")
    lines.append(f"  errored     : {data['cycles_errored']}")
    lines.append(f"  aborted     : {data['cycles_aborted']}")
    lines.append(
        f"  running     : {data['cycles_running']}"
        + (
            "   (← unfinished, possible zombie if not currently running)"
            if data["cycles_running"] > 0
            else ""
        )
    )

    lines.append("")
    lines.append("Findings:")
    lines.append(f"  total       : {data['findings_total']}")
    lines.append(f"  bugs        : {data['findings_bugs']}")
    lines.append(f"  tech-debt   : {data['findings_tech_debt']}")

    lines.append("")
    lines.append("PRs:")
    opened = data["prs_opened"]
    auto_merged = data["prs_auto_merged"]
    merge_failed = data["prs_merge_failed"]
    human_review = opened - auto_merged - merge_failed
    lines.append(f"  opened       : {opened}")
    lines.append(f"  auto-merged  : {auto_merged}")
    lines.append(f"  merge-failed : {merge_failed}")
    lines.append(
        f"  human-review : {human_review}"
        + " (= opened - auto-merged - merge-failed)"
    )

    lines.append("")
    lines.append("Costs (USD):")
    scanner_cost = _fmt_cost(data["cost_scanner"])
    coder_cost = _fmt_cost(data["cost_coder"])
    # total: only numeric if both are available
    sc = data["cost_scanner"]
    co = data["cost_coder"]
    if sc is None and co is None:
        total_cost = "N/A"
    else:
        total_cost = f"${(sc or 0.0) + (co or 0.0):.2f}"
    lines.append(f"  scanner      : {scanner_cost}")
    lines.append(f"  coder        : {coder_cost}")
    lines.append(f"  total        : {total_cost}")
    lines.append("=======================")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def print_report(db_path: Path) -> None:
    """Open *db_path*, gather stats, print report to stdout."""
    conn = _open_readonly(db_path)
    try:
        data = _gather(conn)
    finally:
        conn.close()
    print(format_report(data))
