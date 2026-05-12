#!/usr/bin/env python3
"""R4-2: `pm-agent loop report` coder cost is always N/A.

The loop writes per-coder cost rows with `agent="coder-1"` / `"coder-2"`
(pm_agent.loop calls `persistence.record_cost(cycle_id, "coder-1", ...)`),
but `pm_agent.report._gather` reads `cost_map.get("coder")` — a key that
never exists. Result: every dry-run report shows `coder: N/A` and a
total that only includes scanner.

Exit 0 = REPRODUCED (bug present): report.format_report omits coder cost
                                    despite coder-1/coder-2 rows existing.
Exit 1 = NOT REPRODUCED (bug fixed): report sums all `coder*` agents.

Discovered during dry-run #1 (2026-05-12): real $8.66 spend reported as
$0.28.
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _harness import report  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pm_agent import report as rpt  # noqa: E402


_SCHEMA = """
CREATE TABLE cycles (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    cost_usd REAL NOT NULL DEFAULT 0
);
CREATE TABLE costs (
    id INTEGER PRIMARY KEY,
    cycle_id INTEGER NOT NULL,
    agent TEXT NOT NULL,
    usd REAL NOT NULL,
    at TEXT NOT NULL
);
"""


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="bug-r4-2-"))
    db_path = tmp / "state.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(_SCHEMA)
    # One completed cycle with realistic per-coder cost rows.
    conn.execute(
        "INSERT INTO cycles (id, started_at, finished_at, status, cost_usd) "
        "VALUES (1, '2026-05-12T00:00:00', '2026-05-12T00:20:00', 'done', 3.6414)"
    )
    conn.executemany(
        "INSERT INTO costs (cycle_id, agent, usd, at) VALUES (?, ?, ?, ?)",
        [
            (1, "scanner", 0.09, "2026-05-12T00:01:00"),
            (1, "coder-1", 1.74, "2026-05-12T00:05:00"),
            (1, "coder-2", 1.81, "2026-05-12T00:10:00"),
        ],
    )
    conn.commit()
    conn.close()

    # Capture the formatted report.
    conn = rpt._open_readonly(db_path)
    try:
        data = rpt._gather(conn)
    finally:
        conn.close()
    out = rpt.format_report(data)

    coder_cost = data.get("cost_coder")
    # On buggy code: data["cost_coder"] is None (cost_map.get("coder") miss).
    # On fixed code: data["cost_coder"] == 1.74 + 1.81 == 3.55.
    reproduced = coder_cost is None or "$3.55" not in out

    return report(
        "R4-2",
        reproduced=reproduced,
        evidence=(
            f"data.cost_coder={coder_cost!r}; "
            f"'$3.55' in report output: {'$3.55' in out}"
        ),
    )


if __name__ == "__main__":
    sys.exit(main())
