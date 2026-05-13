"""Unit tests for pm_agent.persistence — SQLite WAL state store + reconciler."""
from __future__ import annotations

import pytest

from pm_agent.persistence import (
    init_db, get_conn, transaction,
    start_cycle, finish_cycle, record_finding, fix_attempts,
    update_finding, record_cost, reconcile,
    record_pr, update_pr_state, find_open_pr_for_bug,
)
from pm_agent.scanner import Finding
from tests._fixtures.fake_repo import fake_repo


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


def test_record_finding_raises_on_check_violation(tmp_path):
    """Bad severity → CHECK fails → INSERT OR IGNORE drops → no row → ValueError."""
    init_db(tmp_path / "state.db")
    cid = start_cycle()
    bad = Finding(
        bug_id="bad01", title="t",
        severity="UrgentlyBad",  # not in CHECK list
        paths=["a.py"], acceptance=["ok"], evidence="e", kind="bug",
    )
    with pytest.raises(ValueError, match="CHECK constraint"):
        record_finding(cid, bad)


def test_transaction_rollback_preserves_original_exception(tmp_path):
    """Exception inside @transaction → ROLLBACK best-effort → original raises."""
    init_db(tmp_path / "state.db")

    class Sentinel(Exception):
        pass

    with pytest.raises(Sentinel):
        with transaction() as c:
            c.execute("INSERT INTO cycles (started_at, status) VALUES (?, 'running')",
                      ("2026-01-01",))
            raise Sentinel("original")
    # The INSERT should have been rolled back.
    cnt = get_conn().execute("SELECT COUNT(*) AS n FROM cycles").fetchone()["n"]
    assert cnt == 0


def test_reconcile_marks_zombies_and_counts_successes(tmp_path):
    """reconcile() marks running cycles aborted, fixing-* findings interrupted,
    counts only successfully-cleaned worktrees/branches."""
    init_db(tmp_path / "state.db")
    # Pre-seed: 1 zombie cycle + 1 zombie finding
    cid = start_cycle()
    f = Finding(bug_id="zz01", title="t", severity="Low",
                paths=["a.py"], acceptance=["ok"], evidence="e", kind="bug")
    fid = record_finding(cid, f)
    update_finding(fid, "fixing-code")
    # No worktree dir, no branches — just verify the cycle + finding part
    with fake_repo() as repo:
        report = reconcile(repo)
    assert report.zombie_cycles == 1
    assert report.zombie_findings == 1
    # Verify status changed
    c = get_conn()
    assert c.execute("SELECT status FROM cycles WHERE id=?", (cid,)).fetchone()["status"] == "aborted"
    assert c.execute("SELECT status FROM findings WHERE id=?", (fid,)).fetchone()["status"] == "interrupted"


def test_find_open_pr_for_bug_no_match(tmp_path) -> None:
    """No findings / no PRs → returns None."""
    init_db(tmp_path / "state.db")
    assert find_open_pr_for_bug("nothing-here") is None
    cid = start_cycle()
    fid = record_finding(cid, _make_finding(bug_id="zz99zz99"))
    # Finding exists but no PR → still None
    assert find_open_pr_for_bug("zz99zz99") is None


def test_find_open_pr_for_bug_returns_open_pr(tmp_path) -> None:
    """Open PR for bug_id → returns its github_number."""
    init_db(tmp_path / "state.db")
    cid = start_cycle()
    fid = record_finding(cid, _make_finding(bug_id="bb22bb22"))
    record_pr(fid, gh_number=42, url="https://gh/x/y/pull/42", state="open", action="opened")
    assert find_open_pr_for_bug("bb22bb22") == 42


def test_find_open_pr_for_bug_ignores_merged_and_closed(tmp_path) -> None:
    """Merged or closed PR → returns None; the bug is considered eligible
    for a fresh attempt in a future cycle."""
    init_db(tmp_path / "state.db")
    cid = start_cycle()
    fid_m = record_finding(cid, _make_finding(bug_id="merged-bug"))
    record_pr(fid_m, gh_number=10, url="u10", state="open", action="opened")
    update_pr_state(10, "merged")
    assert find_open_pr_for_bug("merged-bug") is None

    fid_c = record_finding(cid, _make_finding(bug_id="closed-bug", paths=("c.py",)))
    record_pr(fid_c, gh_number=11, url="u11", state="open", action="opened")
    update_pr_state(11, "closed")
    assert find_open_pr_for_bug("closed-bug") is None


def test_find_open_pr_for_bug_returns_newest_when_multiple(tmp_path) -> None:
    """If somehow >1 OPEN PR exists for same bug_id (race / pre-fix state),
    return the newest by prs.id desc."""
    init_db(tmp_path / "state.db")
    cid = start_cycle()
    fid_1 = record_finding(cid, _make_finding(bug_id="dup-bug"))
    record_pr(fid_1, gh_number=20, url="u20", state="open", action="opened")
    # Simulate a second cycle re-finding the same bug and (pre-R5-2 fix)
    # opening a second PR before the gate existed.
    cid2 = start_cycle()
    fid_2 = record_finding(cid2, _make_finding(bug_id="dup-bug"))
    record_pr(fid_2, gh_number=21, url="u21", state="open", action="opened")
    assert find_open_pr_for_bug("dup-bug") == 21
