#!/usr/bin/env python3
"""R3-C-03: Live Cycle findings list ordered ASC LIMIT 50 → hides newest with >50 findings.

Seed 60 findings (b000..b059), poll /api/live, check whether the newest
(b059) is in the returned list. Buggy code returns b000..b049; fix returns
newest-first.

Exit 0 = REPRODUCED (bug present): newest finding missing from response.
Exit 1 = NOT REPRODUCED (bug fixed): newest finding present.
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
from pm_agent.scanner import Finding  # noqa: E402


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="bug-r3-c-03-"))
    db_path = tmp / "state.db"
    persistence.init_db(db_path)

    cid = persistence.start_cycle()
    for i in range(60):
        f = Finding(
            bug_id=f"b{i:03d}",
            title=f"finding {i}",
            severity="High",
            paths=[f"x{i}.py"],
            acceptance=["ok"],
            evidence="e",
            kind="bug",
        )
        persistence.record_finding(cid, f)

    client = TestClient(create_app())
    data = client.get("/api/live").json()
    bug_ids = {f["bug_id"] for f in data.get("findings", [])}

    newest_present = "b059" in bug_ids
    oldest_present = "b000" in bug_ids

    # Buggy code: ASC → b000..b049 returned, b059 missing.
    # Fixed code: DESC → b059..b010 returned (newest 50), b000 missing.
    reproduced = (not newest_present) and oldest_present
    return report(
        "R3-C-03",
        reproduced=reproduced,
        evidence=(
            f"b059 present={newest_present}, b000 present={oldest_present}, "
            f"n_returned={len(bug_ids)}"
        ),
    )


if __name__ == "__main__":
    sys.exit(main())
