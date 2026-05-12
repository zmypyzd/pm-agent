"""Unit tests for pm_agent.report (pm-agent loop report subcommand)."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from pm_agent import report as rpt


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CREATE_CYCLES = """
CREATE TABLE IF NOT EXISTS cycles (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    cost_usd REAL NOT NULL DEFAULT 0
);
"""

_CREATE_FINDINGS = """
CREATE TABLE IF NOT EXISTS findings (
    id INTEGER PRIMARY KEY,
    cycle_id INTEGER NOT NULL,
    bug_id TEXT NOT NULL,
    title TEXT NOT NULL,
    severity TEXT NOT NULL,
    paths_json TEXT NOT NULL,
    acceptance_json TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    UNIQUE(cycle_id, bug_id)
);
"""

_CREATE_PRS = """
CREATE TABLE IF NOT EXISTS prs (
    id INTEGER PRIMARY KEY,
    finding_id INTEGER NOT NULL,
    github_number INTEGER NOT NULL UNIQUE,
    url TEXT NOT NULL,
    state TEXT NOT NULL,
    action TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

_CREATE_COSTS = """
CREATE TABLE IF NOT EXISTS costs (
    id INTEGER PRIMARY KEY,
    cycle_id INTEGER NOT NULL,
    agent TEXT NOT NULL,
    usd REAL NOT NULL,
    at TEXT NOT NULL
);
"""


def _conn(db_path: Path) -> sqlite3.Connection:
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    return c


def _init_full_schema(db_path: Path) -> sqlite3.Connection:
    c = _conn(db_path)
    c.executescript(_CREATE_CYCLES + _CREATE_FINDINGS + _CREATE_PRS + _CREATE_COSTS)
    c.commit()
    return c


# ---------------------------------------------------------------------------
# (a) Empty state.db — no tables → all zeros, Window: (no cycles)
# ---------------------------------------------------------------------------

class TestEmptyDb:
    def test_no_tables_no_cycles_message(self, tmp_path: Path) -> None:
        db = tmp_path / "state.db"
        # Create a raw empty file with no tables
        sqlite3.connect(db).close()
        out = _capture_report(db)
        assert "Window: (no cycles)" in out

    def test_all_counts_zero(self, tmp_path: Path) -> None:
        db = tmp_path / "state.db"
        sqlite3.connect(db).close()
        out = _capture_report(db)
        for label in ("total", "ok", "errored", "aborted", "running"):
            # Look for "label : 0" with flexible whitespace
            assert _find_count(out, label) == 0, f"Expected 0 for {label!r}"

    def test_costs_na_when_no_table(self, tmp_path: Path) -> None:
        db = tmp_path / "state.db"
        sqlite3.connect(db).close()
        out = _capture_report(db)
        # All cost lines should show N/A
        assert "N/A" in out

    def test_non_existent_db_path(self, tmp_path: Path) -> None:
        """--db pointing to a non-existent file creates it and shows empty."""
        db = tmp_path / "does_not_exist.db"
        out = _capture_report(db)
        assert "Window: (no cycles)" in out
        # File should now exist (or at minimum no crash)

    def test_cycles_table_empty(self, tmp_path: Path) -> None:
        """Cycles table exists but has 0 rows → no-cycles message."""
        db = tmp_path / "state.db"
        c = _conn(db)
        c.executescript(_CREATE_CYCLES)
        c.commit()
        c.close()
        out = _capture_report(db)
        assert "Window: (no cycles)" in out


# ---------------------------------------------------------------------------
# (b) Seeded db — 2 cycles + 3 findings + 1 PR → correct counts
# ---------------------------------------------------------------------------

class TestSeededDb:
    def _seed(self, db: Path) -> None:
        c = _init_full_schema(db)
        # 2 cycles: one done, one errored
        c.execute(
            "INSERT INTO cycles (id, started_at, finished_at, status) VALUES "
            "(1, '2024-01-01T00:00:00', '2024-01-01T06:00:00', 'done')"
        )
        c.execute(
            "INSERT INTO cycles (id, started_at, finished_at, status) VALUES "
            "(2, '2024-01-02T00:00:00', '2024-01-02T06:00:00', 'errored')"
        )
        # 3 findings: 2 bugs, 1 tech-debt
        c.execute(
            "INSERT INTO findings "
            "(cycle_id, bug_id, title, severity, paths_json, acceptance_json, kind, status) "
            "VALUES (1, 'B1', 'Bug one', 'High', '[]', '[]', 'bug', 'done')"
        )
        c.execute(
            "INSERT INTO findings "
            "(cycle_id, bug_id, title, severity, paths_json, acceptance_json, kind, status) "
            "VALUES (1, 'B2', 'Bug two', 'Medium', '[]', '[]', 'bug', 'done')"
        )
        c.execute(
            "INSERT INTO findings "
            "(cycle_id, bug_id, title, severity, paths_json, acceptance_json, kind, status) "
            "VALUES (2, 'T1', 'Tech debt one', 'Low', '[]', '[]', 'tech-debt', 'done')"
        )
        # 1 PR: auto-merged
        c.execute(
            "INSERT INTO prs "
            "(finding_id, github_number, url, state, action, created_at) "
            "VALUES (1, 42, 'https://gh/pr/42', 'merged', 'auto-merged', '2024-01-01T07:00:00')"
        )
        # costs
        c.execute(
            "INSERT INTO costs (cycle_id, agent, usd, at) "
            "VALUES (1, 'scanner', 0.50, '2024-01-01T01:00:00')"
        )
        c.execute(
            "INSERT INTO costs (cycle_id, agent, usd, at) "
            "VALUES (1, 'coder', 1.25, '2024-01-01T02:00:00')"
        )
        c.commit()
        c.close()

    def test_cycle_counts(self, tmp_path: Path) -> None:
        db = tmp_path / "state.db"
        self._seed(db)
        out = _capture_report(db)
        assert "total       : 2" in out
        assert "ok          : 1" in out
        assert "errored     : 1" in out

    def test_finding_counts(self, tmp_path: Path) -> None:
        db = tmp_path / "state.db"
        self._seed(db)
        out = _capture_report(db)
        assert "total       : 3" in out
        assert "bugs        : 2" in out
        assert "tech-debt   : 1" in out

    def test_pr_counts(self, tmp_path: Path) -> None:
        db = tmp_path / "state.db"
        self._seed(db)
        out = _capture_report(db)
        assert "opened       : 1" in out
        assert "auto-merged  : 1" in out
        assert "merge-failed : 0" in out
        # human-review = 1 - 1 - 0 = 0
        assert "human-review : 0" in out

    def test_cost_values(self, tmp_path: Path) -> None:
        db = tmp_path / "state.db"
        self._seed(db)
        out = _capture_report(db)
        assert "$0.50" in out   # scanner
        assert "$1.25" in out   # coder
        assert "$1.75" in out   # total

    def test_window_shows_timestamps(self, tmp_path: Path) -> None:
        db = tmp_path / "state.db"
        self._seed(db)
        out = _capture_report(db)
        assert "2024-01-01" in out
        assert "→" in out

    def test_separator_present(self, tmp_path: Path) -> None:
        db = tmp_path / "state.db"
        self._seed(db)
        out = _capture_report(db)
        assert "=== Dry-run report ===" in out
        assert "=======================" in out


# ---------------------------------------------------------------------------
# (c) Missing cost column / table → N/A
# ---------------------------------------------------------------------------

class TestMissingCostColumn:
    def test_no_costs_table_prints_na(self, tmp_path: Path) -> None:
        db = tmp_path / "state.db"
        c = _conn(db)
        c.executescript(_CREATE_CYCLES + _CREATE_FINDINGS + _CREATE_PRS)
        c.execute(
            "INSERT INTO cycles (id, started_at, finished_at, status) VALUES "
            "(1, '2024-01-01T00:00:00', '2024-01-01T01:00:00', 'done')"
        )
        c.commit()
        c.close()
        out = _capture_report(db)
        assert "N/A" in out

    def test_costs_table_missing_usd_column_prints_na(self, tmp_path: Path) -> None:
        """A costs table with no 'usd' column → N/A (schema mismatch guard)."""
        db = tmp_path / "state.db"
        c = _conn(db)
        c.executescript(_CREATE_CYCLES)
        c.execute(
            "INSERT INTO cycles (id, started_at, finished_at, status) VALUES "
            "(1, '2024-01-01T00:00:00', '2024-01-01T01:00:00', 'done')"
        )
        # costs table without expected columns
        c.execute(
            "CREATE TABLE costs (id INTEGER PRIMARY KEY, x TEXT)"
        )
        c.commit()
        c.close()
        out = _capture_report(db)
        assert "N/A" in out

    def test_coder_cost_aggregates_coder_1_and_coder_2(self, tmp_path: Path) -> None:
        """BUG-R4-2: real loop writes per-coder rows with agent='coder-1'/'coder-2'.

        Before fix: report.py looked for the literal agent='coder' and missed
        them, so cost_coder was always None and total under-reported. After
        fix: any agent name starting with 'coder' is summed.
        """
        db = tmp_path / "state.db"
        c = _init_full_schema(db)
        c.execute(
            "INSERT INTO cycles (id, started_at, finished_at, status) VALUES "
            "(1, '2024-01-01T00:00:00', '2024-01-01T01:00:00', 'done')"
        )
        c.executemany(
            "INSERT INTO costs (cycle_id, agent, usd, at) VALUES (?, ?, ?, ?)",
            [
                (1, "scanner", 0.10, "2024-01-01T00:01:00"),
                (1, "coder-1", 1.20, "2024-01-01T00:05:00"),
                (1, "coder-2", 0.80, "2024-01-01T00:10:00"),
            ],
        )
        c.commit()
        c.close()
        out = _capture_report(db)
        assert "$0.10" in out, "scanner cost missing"
        assert "$2.00" in out, "coder cost should be sum(coder-1, coder-2) = 2.00"
        assert "$2.10" in out, "total = scanner + coders"
        assert "N/A" not in out.split("Costs (USD):", 1)[1], (
            "coder cost should not be N/A when coder-1/coder-2 rows exist"
        )

    def test_running_cycle_shows_zombie_note(self, tmp_path: Path) -> None:
        """A 'running' cycle should show the zombie warning note."""
        db = tmp_path / "state.db"
        c = _init_full_schema(db)
        c.execute(
            "INSERT INTO cycles (id, started_at, status) VALUES "
            "(1, '2024-01-01T00:00:00', 'running')"
        )
        c.commit()
        c.close()
        out = _capture_report(db)
        assert "zombie" in out.lower()
        assert "running     : 1" in out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _capture_report(db_path: Path) -> str:
    """Call print_report and capture stdout."""
    import io
    import sys
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        rpt.print_report(db_path)
    finally:
        sys.stdout = old
    return buf.getvalue()


def _find_count(text: str, label: str) -> int:
    """Extract the integer after 'label : N' in report output."""
    import re
    pattern = rf"{re.escape(label)}\s*:\s*(\d+)"
    m = re.search(pattern, text)
    if m is None:
        raise AssertionError(f"Label {label!r} not found in:\n{text}")
    return int(m.group(1))
