#!/usr/bin/env python3
"""R3-C-06: dashboard serve subcommand never calls init_db.

When the dashboard runs in a separate process from the daemon (documented
flow: "open the dashboard in another terminal"), its persistence module
never had init_db() called → get_conn() raises RuntimeError on every
request. The endpoint masks that with `except RuntimeError → status='idle'`,
so the dashboard reads "idle" forever even while the daemon happily
writes cycles into state.db.

Fix: cmd_dashboard_serve calls persistence.init_db(STATE_DB) before
handing off to uvicorn. init_db is idempotent.

Strategy: stub out uvicorn.run so we don't actually bind a port, run
cmd_dashboard_serve, then issue a request via TestClient. If init_db
was called, /api/live can read the seeded cycle and returns
status="running"; if not, it returns "idle".

Exit 0 = REPRODUCED (dashboard returns 'idle' despite running cycle).
Exit 1 = NOT REPRODUCED (dashboard returns 'running' / sees the cycle).
"""
from __future__ import annotations

import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _harness import report  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from fastapi.testclient import TestClient  # noqa: E402

from pm_agent import cli, persistence  # noqa: E402
from pm_agent.dashboard.server import create_app  # noqa: E402


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="bug-r3-c-06-"))
    fake_state_db = tmp / "state.db"

    # 1. Simulate the "daemon already wrote a running cycle" world:
    #    pre-create state.db with a row. Use a *separate* init so we then
    #    clear the persistence module-level handle, mimicking a fresh
    #    dashboard process where no init_db has run yet.
    persistence.init_db(fake_state_db)
    cid = persistence.start_cycle()
    # Close all module-level state so the "dashboard process" starts fresh.
    persistence._DB_PATH = None  # type: ignore[attr-defined]
    persistence._LOCAL.__dict__.clear()

    # 2. Drive cmd_dashboard_serve but stub uvicorn so we don't bind a port.
    #    Patch STATE_DB so the fix (when present) writes to our temp file
    #    rather than the real ~/.pm-agent/state.db.
    fake_uvicorn = types.SimpleNamespace(run=lambda *a, **kw: None)
    args = types.SimpleNamespace(host="127.0.0.1", port=8000)

    with patch.dict(sys.modules, {"uvicorn": fake_uvicorn}), \
            patch("pm_agent.loop.STATE_DB", fake_state_db):
        cli.cmd_dashboard_serve(args)

    # 3. Now hit the API. If init_db ran, /api/live sees cycle cid. If not,
    #    persistence._DB_PATH is None → get_conn() raises RuntimeError →
    #    endpoint masks it as "idle".
    client = TestClient(create_app())
    live = client.get("/api/live").json()
    status = live.get("status")

    reproduced = status == "idle"
    return report(
        "R3-C-06",
        reproduced=reproduced,
        evidence=(
            f"seeded running cycle id={cid}; dashboard /api/live status={status!r}; "
            f"_DB_PATH after cmd_dashboard_serve={persistence._DB_PATH!r}"
        ),
    )


if __name__ == "__main__":
    sys.exit(main())
