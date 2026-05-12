#!/usr/bin/env python3
"""R4-1: persistence.record_pr crashes on the second failed PR per cycle.

When `gh pr create` fails (no remote, auth expired, rate limit, branch
protection), `github.open_pr` returns ``PRResult(action="failed", number=0)``.
The cycle loop used to call ``record_pr(finding_id, gh_number=0, ...)``
once per failed PR. The first insert succeeded; the second hit the
``UNIQUE constraint`` on ``prs.github_number`` and raised
``sqlite3.IntegrityError``, caught as a finding-level exception and
marked the finding "failed". Two findings per cycle could therefore die
on PR-recording even when the upstream Coder pipeline succeeded.

This repro exercises ``persistence.record_pr`` directly, since the fix
lives there (no-op on ``gh_number <= 0``). The associated loop-level
guard (``loop.py``: skip ``record_pr`` + mark finding failed when
``pr.action == "failed"``) is verified by ``tests/test_loop.py``.

Exit 0 = REPRODUCED (bug present): second insert raises IntegrityError.
Exit 1 = NOT REPRODUCED (bug fixed): both calls return -1, no row inserted.

Discovered during dry-run #1 (2026-05-12). The bug fired at least 5
times across 3 cycles in that run.
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _harness import report  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pm_agent import persistence  # noqa: E402


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="bug-r4-1-"))
    db_path = tmp / "state.db"
    persistence.init_db(db_path)

    # Two findings in the same cycle, each with a failed gh pr create.
    cid = persistence.start_cycle()
    from pm_agent.scanner import Finding
    f1 = Finding(
        bug_id="r4_1_bug_a", title="bug a", evidence="x",
        paths=("dummy_a.py",), severity="High", kind="bug",
        acceptance=("nope",),
    )
    f2 = Finding(
        bug_id="r4_1_bug_b", title="bug b", evidence="y",
        paths=("dummy_b.py",), severity="High", kind="bug",
        acceptance=("nope",),
    )
    fid1 = persistence.record_finding(cid, f1)
    fid2 = persistence.record_finding(cid, f2)

    inserted_rows: list[int] = []
    integrity_error: Exception | None = None
    try:
        # First failed PR — buggy code inserted (number=0).
        r1 = persistence.record_pr(fid1, 0, "", state="open", action="failed")
        inserted_rows.append(r1)
        # Second failed PR — same number=0 → UNIQUE violation on buggy code.
        r2 = persistence.record_pr(fid2, 0, "", state="open", action="failed")
        inserted_rows.append(r2)
    except sqlite3.IntegrityError as e:  # buggy path
        integrity_error = e

    # Verify final state: prs table must NOT contain any github_number=0 rows.
    conn = persistence.get_conn()
    n_junk = conn.execute(
        "SELECT COUNT(*) AS n FROM prs WHERE github_number = 0"
    ).fetchone()["n"]

    reproduced = (
        integrity_error is not None
        or n_junk > 0
        or any(r != -1 for r in inserted_rows)
    )
    return report(
        "R4-1",
        reproduced=reproduced,
        evidence=(
            f"integrity_error={integrity_error!r}; "
            f"inserted_rows={inserted_rows!r}; "
            f"prs rows with number=0: {n_junk}"
        ),
    )


if __name__ == "__main__":
    sys.exit(main())
