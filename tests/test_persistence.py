"""Unit tests for pm_agent.persistence — SQLite WAL state store + reconciler."""
from __future__ import annotations

import pytest

from pm_agent.persistence import (
    init_db, get_conn, transaction,
    start_cycle, finish_cycle, record_finding, fix_attempts,
    update_finding, record_cost, reconcile,
)
from pm_agent.scanner import Finding


def _make_finding(bug_id: str = "ab12cd34", paths=("a.py",)) -> Finding:
    return Finding(
        bug_id=bug_id, title="t", severity="Low",
        paths=list(paths), acceptance=["ok"], evidence="e", kind="bug",
    )


def test_init_db_enables_wal_mode(tmp_path):
    db = tmp_path / "state.db"
    init_db(db)
    conn = get_conn()
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_fix_attempts_counts_failed_findings(tmp_path):
    init_db(tmp_path / "state.db")
    f = _make_finding()
    for _ in range(3):
        cid = start_cycle()
        fid = record_finding(cid, f)
        update_finding(fid, "failed")
    assert fix_attempts("ab12cd34") == 3


def test_record_finding_unique_within_cycle(tmp_path):
    """Same bug_id in same cycle should NOT insert duplicate row.

    INSERT OR IGNORE returns the existing finding_id on collision.
    """
    init_db(tmp_path / "state.db")
    cid = start_cycle()
    f = _make_finding()
    fid_a = record_finding(cid, f)
    fid_b = record_finding(cid, f)
    assert fid_a == fid_b
