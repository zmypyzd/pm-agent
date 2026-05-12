#!/usr/bin/env python3
"""R3-C-01 / R3-C-02: Dashboard reads cycles.cost_usd directly for live + trend.

cycles.cost_usd is only written when finish_cycle() runs, so a running cycle
shows $0.00 even after record_cost(cid, 'coder', 1.00) inserts rows into the
costs table. Same root cause makes the cumulative-cost banner under-report.

Exit 0 = REPRODUCED (bug present): /api/live shows cost 0 despite $1.50 in costs.
Exit 1 = NOT REPRODUCED (bug fixed): /api/live sums from costs table → 1.50.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _harness import report  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from fastapi.testclient import TestClient  # noqa: E402

from pm_agent import persistence  # noqa: E402
from pm_agent.dashboard.server import create_app  # noqa: E402


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="bug-r3-c-01-"))
    db_path = tmp / "state.db"
    persistence.init_db(db_path)

    # Insert a running cycle + two cost rows totaling $1.50
    cid = persistence.start_cycle()
    persistence.record_cost(cid, "scanner", 0.50)
    persistence.record_cost(cid, "coder", 1.00)

    client = TestClient(create_app())
    live = client.get("/api/live").json()
    trend = client.get("/api/trend").json()

    live_cost = (live.get("cycle") or {}).get("cost_usd", 0)
    trend_points = trend.get("points") or []
    trend_cumulative = trend_points[-1]["cumulative_cost_usd"] if trend_points else 0

    # On fixed code, both should reflect the live $1.50 spend.
    reproduced = (
        abs(float(live_cost) - 1.50) > 1e-6
        or abs(float(trend_cumulative) - 1.50) > 1e-6
    )
    return report(
        "R3-C-01",
        reproduced=reproduced,
        evidence=(
            f"live.cost_usd={live_cost!r} (expected 1.50); "
            f"trend.cumulative={trend_cumulative!r} (expected 1.50)"
        ),
    )


if __name__ == "__main__":
    sys.exit(main())
