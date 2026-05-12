# Round 3 Chaos QA — Hunter C (dashboard + CLI)

> Read-only. NO code changes.
> Date: 2026-05-12
> Branch: feat/beta-autonomous-loop @ 5cbbb65

## Hunter scope
- `pm_agent/dashboard/server.py`
- `pm_agent/dashboard/templates/index.html`
- `pm_agent/cli.py`
- `pm_agent/preflight.py`
- `pm_agent/report.py`

## Findings

---
## R3-C-01: Live Cycle cost_usd is permanently $0.00 for the entire duration of a running cycle — defeats the dashboard's primary monitoring purpose
- **Severity:** High
- **Type:** Logic / UX-as-bug
- **Code location:** `pm_agent/dashboard/server.py:81-108` (live_cycle query) + `pm_agent/persistence.py:150-156` (start_cycle) + `pm_agent/persistence.py:158-163` (finish_cycle)
- **Trigger (precise):**
  1. Start daemon. `start_cycle()` inserts row with `cost_usd` defaulting to 0 (schema default).
  2. Inside a long cycle, `record_cost(cid, 'coder', 1.50)` etc. accumulates rows into the `costs` table.
  3. Dashboard polls `/api/live` every 1s; the SQL selects `cycles.cost_usd` directly (not `SUM(costs.usd)`).
  4. Returns `cost_usd: 0.0` for the entire run until `finish_cycle` finally writes the total.
- **Why it breaks:** The Live Cycle panel reports `Cost: $0.0000` for the whole runtime of a cycle that may have spent $10+. Operator's "is this run out of control?" signal is silent until after the cycle completes.
- **Repro hint:**
  ```python
  init_db(p); cid = start_cycle()
  record_cost(cid, 'scanner', 0.10); record_cost(cid, 'coder', 0.50)
  client.get('/api/live').json()['cycle']['cost_usd']  # → 0.0, not 0.60
  ```
- **Confidence:** Confirmed (reproduced)

---
## R3-C-02: Cumulative-cost banner under-reports during a live cycle for the same reason — red-banner ($50) trigger fires LATE
- **Severity:** High
- **Type:** Logic / UX-as-bug
- **Code location:** `pm_agent/dashboard/server.py:127-156` (`/api/trend`) + `pm_agent/dashboard/templates/index.html:14-24` (banner update)
- **Trigger (precise):**
  1. Previous cycles sum to $48 cumulative. Banner shows `$48.00` (green).
  2. Current cycle runs for 4 hours, accumulating $10 in `costs` table.
  3. `/api/trend` query: `cumulative += float(row["cost_usd"] or 0)` reads `cycles.cost_usd` for the in-progress row → still 0.
  4. Banner stays at `$48.00` instead of `$58.00`.
  5. The red-warning threshold (`cost > 50`) only flips AFTER the cycle finishes — exactly when the operator can no longer prevent the over-spend.
- **Why it breaks:** Same root cause as R3-C-01. The "I should check on this" signal during long unattended runs (per the module docstring) is structurally late by up to one full cycle interval (1800s default).
- **Repro hint:** Same as R3-C-01 but observe `/api/trend` instead of `/api/live`.
- **Confidence:** Confirmed (same root cause, propagates to trend endpoint)

---
## R3-C-03: Live Cycle findings list shows OLDEST 50 findings, not newest — operator misses fresh discoveries
- **Severity:** High
- **Type:** Data / Logic
- **Code location:** `pm_agent/dashboard/server.py:92-96`
- **Trigger (precise):**
  - SQL: `SELECT bug_id, title, severity, status FROM findings WHERE cycle_id=? ORDER BY id LIMIT 50`
  - `ORDER BY id` is ASC. With 60+ findings in one cycle, the dashboard returns ids 1–50, hiding ids 51–60.
  - `findings_total: 60` reports the true count but the list is wrong.
- **Why it breaks:** Counter-spec to the demo Beat 2 expectation of "see the cycle's freshly discovered findings." Reproduction showed `findings[0].bug_id == 'b000'` and `findings[-1].bug_id == 'b049'` from 60 inserted findings — the 10 newest are invisible.
- **Repro hint:**
  ```python
  cid = start_cycle()
  for i in range(60): record_finding(cid, Finding(bug_id=f'b{i:03d}', ...))
  client.get('/api/live').json()['findings'][-1]['bug_id']  # → 'b049' not 'b059'
  ```
- **Confidence:** Confirmed (reproduced)

---
## R3-C-04: `cmd_loop_run` ALWAYS returns exit 130 and prints "interrupted" to stderr — including on graceful shutdown
- **Severity:** Medium
- **Type:** Logic
- **Code location:** `pm_agent/cli.py:22-47`
- **Trigger (precise):**
  1. `pm-agent loop run`; daemon receives SIGTERM, signal handler sets `stop_event`, loop exits cleanly.
  2. `asyncio.run(run_forever(...))` returns normally (no exception).
  3. After the `try/except`, the function unconditionally executes `print("interrupted", file=sys.stderr); return 130`.
- **Why it breaks:**
  - Shell scripts wrapping `pm-agent loop run` see `$? = 130` and assume failure, even for the orderly daemon shutdown the loop is designed for.
  - The unconditional stderr message ("interrupted") prints even on `KeyboardInterrupt` — fine — and also on the normal exit path — wrong. The comment claims the only exits are signal-driven, but `GhAuthError` is also documented in `loop.py` (line 419-421) as a `break` path. After that break, cli.py STILL says "interrupted" and returns 130.
- **Repro hint:**
  ```python
  async def fake_run(*a, **k): pass
  with patch("pm_agent.loop.run_forever", fake_run):
      rc = cmd_loop_run(ns)
  # rc == 130, stderr has "interrupted"
  ```
- **Confidence:** Confirmed (reproduced)

---
## R3-C-05: Tilde in `--db` / `--repo` arguments is never expanded — `pm-agent loop report --db ~/.pm-agent/state.db` silently creates a fake state.db in CWD and reports zero cycles
- **Severity:** High
- **Type:** Logic / UX-as-bug
- **Code location:** `pm_agent/cli.py:124, 135, 142` (`type=Path` without `expanduser`) + `pm_agent/report.py:21-29` (`_open_readonly` auto-creates parent dirs AND lets sqlite3 create an empty DB)
- **Trigger (precise):**
  1. `pm-agent loop report --db ~/.pm-agent/state.db`
  2. argparse `type=Path` stores literal `Path('~/.pm-agent/state.db')` — no expansion.
  3. `_open_readonly` does `db_path.parent.mkdir(parents=True, exist_ok=True)` → creates `./\~/.pm-agent/` in CWD (a literal `~` directory).
  4. `sqlite3.connect(...)` creates an empty file at `./\~/.pm-agent/state.db`.
  5. Report runs against the empty DB → prints `Window: (no cycles)` and all zeros.
- **Why it breaks:**
  - Operator believes their dry-run produced no cycles. False signal during the most critical operator workflow (post-run report).
  - Filesystem litter: a directory literally named `~` is created in CWD. Easy to overlook, hard to clean up safely.
  - Same problem applies to `--repo ~/projects/foo` for `loop preflight` and `loop run`. The preflight git check then fails with a confusing "not a git repo" error.
- **Repro hint:**
  ```bash
  cd /tmp/safe && pm-agent loop report --db ~/.pm-agent/state.db
  ls -la /tmp/safe   # → drwxr-xr-x  ~/  (literal tilde dir)
  ```
- **Confidence:** Confirmed (reproduced)

---
## R3-C-06: Dashboard reports `status="idle"` permanently if started before any `init_db()` call — even when state.db has a running cycle
- **Severity:** High
- **Type:** Logic / Lifecycle
- **Code location:** `pm_agent/cli.py:95-101` (cmd_dashboard_serve) + `pm_agent/dashboard/server.py:76-78` (catches `RuntimeError`)
- **Trigger (precise):**
  1. Operator runs daemon and dashboard in **separate shells / separate processes** (the documented flow per `docs/superpowers/dry-run-runbook.md`).
  2. In the dashboard process, `persistence._DB_PATH` is module-global None — `init_db` was never called.
  3. `get_conn()` raises `RuntimeError("init_db() must be called before get_conn()")`.
  4. `live_cycle()` catches `RuntimeError` and returns `LiveCycleResponse(cycle=None, status="idle", ...)`.
  5. Dashboard shows "No running cycle — daemon idle" even while the daemon process is actively running cycle 37.
- **Why it breaks:** The dashboard process has no awareness that state.db exists on disk. The except clause meant for "DB not yet initialized" silently masks a true cross-process integration bug. There is no code path that calls `init_db` from `cmd_dashboard_serve`.
- **Repro hint:**
  ```python
  # In a fresh process, with state.db on disk already containing a running cycle:
  persistence._DB_PATH = None
  client.get("/api/live").json()  # → {"cycle": None, "status": "idle", ...}
  ```
- **Confidence:** Confirmed (reproduced)

---
## R3-C-07: `pm-agent loop run` with negative or zero `--interval-s` hot-loops at 100% CPU instead of being rejected
- **Severity:** Medium
- **Type:** Logic / Input validation
- **Code location:** `pm_agent/cli.py:125-129` (no `choices=` or range validation) + `pm_agent/loop.py:427` (`asyncio.wait_for(..., timeout=cfg.interval_s)`)
- **Trigger (precise):**
  1. `pm-agent loop run --interval-s -1` (or `0`).
  2. argparse `type=int` accepts negative ints. No validation.
  3. `asyncio.wait_for(stop_event.wait(), timeout=-1)` raises `TimeoutError` immediately.
  4. Outer `while` loop re-enters `_run_one_cycle` instantly — hot loop.
  5. Same for `--coder-timeout -5` (`type=float`), `--test-timeout -5`, `--max-retries -3`.
- **Why it breaks:** Operator typo silently destroys their dry-run budget. The CLI accepts an interval that is structurally impossible (a negative duration), then proceeds.
- **Repro hint:**
  ```bash
  pm-agent loop run --interval-s -1     # CPU pegs
  pm-agent loop run --interval-s 0      # CPU pegs
  pm-agent loop run --coder-timeout -1  # silent
  pm-agent loop run --max-retries -1    # silent — likely infinite-retry behavior in runner
  ```
- **Confidence:** Confirmed (CLI accepts; `wait_for(timeout=-1)` raises TimeoutError immediately)

---
## R3-C-08: `pm-agent loop report` crashes with uncaught `sqlite3.DatabaseError` on a corrupted state.db
- **Severity:** Medium
- **Type:** Crash / Hygiene
- **Code location:** `pm_agent/report.py:21-29` (`_open_readonly` doesn't validate magic bytes) + `pm_agent/report.py:251-258` (`print_report` has no top-level exception handler)
- **Trigger (precise):**
  1. State.db gets corrupted (partial truncation, disk full mid-write, manual `dd` accident).
  2. `pm-agent loop report` is the operator's go-to post-run inspection tool.
  3. `_open_readonly` does `sqlite3.connect(...)` — succeeds (sqlite3 lazy-loads).
  4. First query `SELECT name FROM sqlite_master ...` raises `sqlite3.DatabaseError: file is not a database`.
  5. Traceback dumped to stdout, no actionable message for operator.
- **Why it breaks:** preflight's `check_state_db_clean` handles this gracefully (`except Exception`); report.py does not. Inconsistent hardening.
- **Repro hint:**
  ```python
  Path("state.db").write_bytes(b"garbage" * 1000)
  rpt.print_report(Path("state.db"))   # DatabaseError uncaught
  ```
- **Confidence:** Confirmed (reproduced)

---
## R3-C-09: `check_tmp_writable` is a placebo — `touch()` doesn't probe actual write capability, and nothing in `loop.py` actually writes `/tmp/loop.log`
- **Severity:** Low
- **Type:** Hygiene / Placebo check
- **Code location:** `pm_agent/preflight.py:263-278`
- **Trigger (precise):**
  - `check_tmp_writable` calls `Path("/tmp/loop.log").touch()` which only sets mtime / creates an empty file.
  - On macOS / Linux, `touch` can succeed when the file already exists and is owned by another user (e.g. system tmp cleanup wrote one and changed ownership), but a subsequent `open(..., "w")` truncate would fail with `PermissionError`.
  - More fundamentally: `grep -rn /tmp/loop.log pm_agent/` returns ONLY the preflight check itself. `loop.py` doesn't touch this file. `/tmp/loop.log` exists only as a SHELL REDIRECTION in `docs/superpowers/dry-run-runbook.md` (`pm-agent loop run ... > /tmp/loop.log 2>&1`). The shell handles writability when it opens the FD, not the Python code.
- **Why it breaks:** The check provides false confidence. If the docs change the log path to `/var/log/...`, the check still passes irrelevantly. If a stale `/tmp/loop.log` is owned by another user, the check passes but the operator's shell redirection fails.
- **Repro hint:** Read the check. There is no Python code path that opens `/tmp/loop.log` for write.
- **Confidence:** Confirmed (static read)

---
## R3-C-10: report.py displays "scanner: $0.50 / coder: N/A / total: $0.50" — total contradicts the "N/A" for coder
- **Severity:** Low
- **Type:** UX-as-bug
- **Code location:** `pm_agent/report.py:230-241`
- **Trigger (precise):**
  1. costs table has scanner rows but no coder rows (e.g. all cycles scan-empty, never invoked coder).
  2. `data["cost_scanner"] = 0.50`, `data["cost_coder"] = None`.
  3. Per-agent display: `_fmt_cost(None) → "N/A"`.
  4. Total computation: `if sc is None and co is None` → false → `total = f"${(sc or 0.0) + (co or 0.0):.2f}"` → `"$0.50"`.
- **Why it breaks:** Operator reads "coder: N/A" as "coder cost is unknown" but then the total is presented as if coder cost is 0. Mathematically inconsistent presentation. Pick one: either treat None as 0 throughout, or surface "$0.50 + N/A = N/A".
- **Repro hint:** Seed costs table with only scanner rows, run report. See lines 229-241 of report.py.
- **Confidence:** Confirmed (logic read)

---
## R3-C-11: `_fmt_duration` produces `-1h 0m` for negative durations (clock skew / out-of-order timestamps); sub-minute durations display as `0h 0m`
- **Severity:** Low
- **Type:** UX-as-bug
- **Code location:** `pm_agent/report.py:48-51`
- **Trigger (precise):**
  - Negative input (`_fmt_duration(-3600)`) → `int(-3600) // 3600 == -1`, `(-3600) % 3600 // 60 == 0` → `"-1h 0m"`.
  - Reproducible when state.db has a cycle row with finished_at earlier than started_at (clock skew during NTP step, manual sqlite injection, or zombie reconcile race that backfills finished_at before started_at gets fully written).
  - Also: any dry-run cycle that finishes in <60s reports `Duration: 0h 0m` — the report rounds away seconds entirely. Most demo cycles ("scan-empty" or fast skip paths) will display 0h 0m.
- **Why it breaks:** Both edges produce misleading or nonsensical output during normal operation. The negative case can crash future code that does `_fmt_duration(...)` on a `Decimal` if expectations change.
- **Repro hint:**
  ```python
  from pm_agent.report import _fmt_duration
  _fmt_duration(-3600)  # → "-1h 0m"
  _fmt_duration(0.5)     # → "0h 0m"
  ```
- **Confidence:** Confirmed (reproduced)

---
## R3-C-12: `/api/trend` is unbounded — a long dry run can produce 1000+ cycles in 24h, returning a massive JSON every 30s polled by every open dashboard tab
- **Severity:** Low
- **Type:** UX-as-bug / Performance
- **Code location:** `pm_agent/dashboard/server.py:127-156`
- **Trigger (precise):**
  - Query `SELECT id, started_at, cost_usd FROM cycles WHERE started_at >= ? ORDER BY started_at` has NO `LIMIT`.
  - Loop body runs 3 additional COUNT queries per cycle (findings, prs, merges) — pure N+1.
  - With `--interval-s 30` and a 24h run, that's 2880 cycles × 3 queries = 8640 sqlite reads every 30 seconds while the dashboard is open.
- **Why it breaks:** Memory + bandwidth grow linearly with run length. Performance degrades silently. Operator has no way to cap. The trend canvas also renders 2880 bars at 1px each — chart becomes unreadable.
- **Repro hint:** Either run 24h with --interval-s 60, or seed 5000 cycles, then `client.get("/api/trend")` and observe response size + duration.
- **Confidence:** Suspected (math is straightforward; not observed end-to-end)

---

Found 12 net-new beyond rounds 1+2.

### Lane notes for Hunters A & B
- R3-C-07 (negative `--interval-s`) is mostly a CLI/argparse gap; the loop-side consequence (hot loop) is Hunter A's territory.
- R3-C-06 (dashboard-vs-daemon process boundary) brushes against Hunter A's persistence lane — root cause is missing `init_db` call from the dashboard process; fix lives in cli.py / dashboard server module init.
