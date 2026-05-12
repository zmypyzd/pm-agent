"""SQLite WAL state store for the Beta autonomous loop.

Schema and contracts per spec §3. All callers use module-level CRUD
functions; connections are thread-local. WAL mode lets dashboard polling
coexist with daemon writes without 'database is locked'.

Reconciler (called once at daemon startup) cleans zombie cycles/findings
that were mid-flight when the previous daemon died.
"""
from __future__ import annotations

import datetime as _dt
import json
import shutil
import sqlite3
import subprocess
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Literal

from pm_agent.scanner import Finding

CycleStatus = Literal["running", "done", "scan-empty", "aborted", "errored"]
FindingStatus = Literal[
    "discovered", "fixing-code", "fixing-test", "integrating", "testing",
    "routing", "done", "failed", "skipped", "interrupted",
]

_DB_PATH: Path | None = None
_LOCAL = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS cycles (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL CHECK(status IN
        ('running','done','scan-empty','aborted','errored')),
    cost_usd REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_cycles_started_at ON cycles(started_at);

CREATE TABLE IF NOT EXISTS findings (
    id INTEGER PRIMARY KEY,
    cycle_id INTEGER NOT NULL REFERENCES cycles(id),
    bug_id TEXT NOT NULL,
    title TEXT NOT NULL,
    severity TEXT NOT NULL CHECK(severity IN ('Critical','High','Medium','Low')),
    paths_json TEXT NOT NULL,
    acceptance_json TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('bug','tech-debt')),
    status TEXT NOT NULL CHECK(status IN
        ('discovered','fixing-code','fixing-test','integrating','testing',
         'routing','done','failed','skipped','interrupted')),
    UNIQUE(cycle_id, bug_id)
);
CREATE INDEX IF NOT EXISTS idx_findings_bug_id ON findings(bug_id);
CREATE INDEX IF NOT EXISTS idx_findings_cycle_status ON findings(cycle_id, status);

CREATE TABLE IF NOT EXISTS prs (
    id INTEGER PRIMARY KEY,
    finding_id INTEGER NOT NULL REFERENCES findings(id),
    github_number INTEGER NOT NULL UNIQUE,
    url TEXT NOT NULL,
    state TEXT NOT NULL,
    action TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_prs_state ON prs(state);

CREATE TABLE IF NOT EXISTS costs (
    id INTEGER PRIMARY KEY,
    cycle_id INTEGER NOT NULL REFERENCES cycles(id),
    agent TEXT NOT NULL,
    usd REAL NOT NULL,
    at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_costs_cycle_at ON costs(cycle_id, at);
"""


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def init_db(path: Path) -> None:
    """Open + create schema + enable WAL. Idempotent.

    On re-init in the same thread, closes the existing connection before clearing
    the thread-local pool to avoid file-handle leaks.

    NOTE: This only clears the CALLING thread's pool. Other threads holding
    conns from a prior init_db will keep using the old DB file. For tests
    that re-init across threads, ensure background threads are joined first.
    """
    global _DB_PATH
    _DB_PATH = Path(path)
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    # Close any existing thread-local conn before clearing the pool
    if hasattr(_LOCAL, "conn"):
        try:
            _LOCAL.conn.close()
        except Exception:
            pass
    _LOCAL.__dict__.clear()


def get_conn() -> sqlite3.Connection:
    """Thread-local connection. Opens on first call per thread."""
    if not hasattr(_LOCAL, "conn"):
        if _DB_PATH is None:
            raise RuntimeError("init_db() must be called before get_conn()")
        c = sqlite3.connect(_DB_PATH, isolation_level=None)  # autocommit
        c.execute("PRAGMA foreign_keys = ON")
        c.row_factory = sqlite3.Row
        _LOCAL.conn = c
    return _LOCAL.conn


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """Multi-statement scoping with BEGIN/COMMIT/ROLLBACK.

    On exception inside the with-block, attempts ROLLBACK best-effort; the
    ROLLBACK's own failure (if any) is suppressed so the original exception
    surfaces with its traceback intact.
    """
    c = get_conn()
    c.execute("BEGIN")
    try:
        yield c
    except Exception:
        try:
            c.execute("ROLLBACK")
        except Exception:
            pass  # best-effort; original exception takes priority
        raise
    else:
        c.execute("COMMIT")


def start_cycle() -> int:
    cur = get_conn().execute(
        "INSERT INTO cycles (started_at, status) VALUES (?, 'running')",
        (_now(),),
    )
    return cur.lastrowid  # type: ignore[return-value]


def finish_cycle(cycle_id: int, status: CycleStatus, cost_usd: float) -> None:
    get_conn().execute(
        "UPDATE cycles SET finished_at=?, status=?, cost_usd=? WHERE id=?",
        (_now(), status, cost_usd, cycle_id),
    )


def record_finding(cycle_id: int, finding: Finding) -> int:
    """INSERT OR IGNORE; returns existing finding_id on (cycle_id, bug_id) collision.

    Raises ValueError if the row could not be inserted AND does not already exist —
    typically a CHECK constraint violation (invalid severity/kind/status).
    """
    c = get_conn()
    c.execute(
        """INSERT OR IGNORE INTO findings
           (cycle_id, bug_id, title, severity, paths_json, acceptance_json, kind, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'discovered')""",
        (cycle_id, finding.bug_id, finding.title, finding.severity,
         json.dumps(finding.paths), json.dumps(finding.acceptance), finding.kind),
    )
    row = c.execute(
        "SELECT id FROM findings WHERE cycle_id=? AND bug_id=?",
        (cycle_id, finding.bug_id),
    ).fetchone()
    if row is None:
        raise ValueError(
            f"record_finding failed for bug_id={finding.bug_id!r}: CHECK constraint "
            f"likely rejected the row (severity={finding.severity!r}, kind={finding.kind!r}). "
            f"No row inserted and no existing row found."
        )
    return row["id"]


def fix_attempts(bug_id: str) -> int:
    """Count of failed/interrupted attempts for this bug across all cycles."""
    row = get_conn().execute(
        "SELECT COUNT(*) AS n FROM findings WHERE bug_id=? AND status IN ('failed','interrupted')",
        (bug_id,),
    ).fetchone()
    return row["n"]


def update_finding(finding_id: int, status: FindingStatus) -> None:
    get_conn().execute(
        "UPDATE findings SET status=? WHERE id=?",
        (status, finding_id),
    )


def record_pr(finding_id: int, gh_number: int, url: str, state: str, action: str) -> int:
    cur = get_conn().execute(
        """INSERT INTO prs (finding_id, github_number, url, state, action, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (finding_id, gh_number, url, state, action, _now()),
    )
    return cur.lastrowid  # type: ignore[return-value]


def update_pr_state(gh_number: int, state: str) -> None:
    get_conn().execute(
        "UPDATE prs SET state=? WHERE github_number=?",
        (state, gh_number),
    )


def record_cost(cycle_id: int, agent: str, usd: float) -> None:
    get_conn().execute(
        "INSERT INTO costs (cycle_id, agent, usd, at) VALUES (?, ?, ?, ?)",
        (cycle_id, agent, usd, _now()),
    )


@dataclass
class ReconcileReport:
    zombie_cycles: int = 0
    zombie_findings: int = 0
    orphan_worktrees: int = 0
    orphan_branches: int = 0


def reconcile(repo: Path) -> ReconcileReport:
    """Called once at daemon startup. Mark stale cycles/findings; clean orphan
    git artifacts in `repo`. Idempotent.

    Per spec §3: status='running' cycles → 'aborted';
                  status in {fixing-code, fixing-test, integrating, testing, routing}
                  findings → 'interrupted'.

    Counters in ReconcileReport reflect successful cleanups only — failed
    git subprocess calls do NOT bump the counter.
    """
    report = ReconcileReport()
    # Zombie cycle + finding updates wrapped in a transaction so the two
    # tables stay internally consistent if the daemon crashes between them.
    with transaction() as c:
        r = c.execute(
            "UPDATE cycles SET status='aborted', finished_at=? WHERE status='running'",
            (_now(),),
        )
        report.zombie_cycles = r.rowcount
        r = c.execute(
            """UPDATE findings SET status='interrupted'
               WHERE status IN ('fixing-code','fixing-test','integrating','testing','routing')""",
        )
        report.zombie_findings = r.rowcount

    # Clean orphan worktrees + branches. repo may not exist (e.g. test scenario);
    # bail gracefully.
    if not repo.exists():
        return report

    wt_dir = repo / ".pm-agent-worktrees"
    if wt_dir.exists():
        for d in wt_dir.iterdir():
            if d.is_dir() and d.name != ".lock":
                rc = subprocess.run(
                    ["git", "worktree", "remove", "--force", str(d)],
                    cwd=repo, capture_output=True,
                ).returncode
                cleaned = (rc == 0)
                if d.exists():
                    # Fallback: rmtree counts as cleanup if it removes the dir.
                    shutil.rmtree(d, ignore_errors=True)
                    cleaned = cleaned or (not d.exists())
                if cleaned:
                    report.orphan_worktrees += 1

    try:
        out = subprocess.run(
            ["git", "branch", "--list", "ai/*"],
            cwd=repo, capture_output=True, text=True, check=False,
        ).stdout
    except FileNotFoundError:
        # git not on PATH (rare); bail
        return report

    for line in out.splitlines():
        branch = line.strip().lstrip("* ").strip()
        if branch and branch.startswith("ai/"):
            rc = subprocess.run(
                ["git", "branch", "-D", branch],
                cwd=repo, capture_output=True,
            ).returncode
            if rc == 0:
                report.orphan_branches += 1
    return report
