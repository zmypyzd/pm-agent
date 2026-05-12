# Round 3 Chaos QA — Hunter A (persistence + loop)

> Read-only adversarial review. NO code changes made by this hunter.
> Date: 2026-05-12
> Branch: feat/beta-autonomous-loop @ 5cbbb65

## Hunter scope
- pm_agent/persistence.py
- pm_agent/loop.py

## Findings

---
## R3-A-01: `--interval-s 0` (or any non-positive int) busy-loops the daemon, burning unbounded API spend

- **Severity:** High
- **Type:** Logic / Resource (financial)
- **Code location:** `pm_agent/loop.py:427` — `await asyncio.wait_for(stop_event.wait(), timeout=cfg.interval_s)`; and `pm_agent/cli.py:125-126` — `p_loop_run.add_argument("--interval-s", type=int, default=1800, ...)` (no `min`/validation).
- **Trigger (precise):** Operator types `uv run python -m pm_agent.cli loop run --interval-s 0` (or `-1`). Per `LoopConfig.interval_s: int = 1800` no enforcement on lower bound, and the CLI parser only enforces `type=int`. Inside `run_forever`, `wait_for(..., timeout=0)` raises `asyncio.TimeoutError` *immediately* (zero/negative timeout in asyncio fires before yielding). The `except asyncio.TimeoutError: pass` catches it, and the `while` loop re-enters `run_one_cycle` with no inter-cycle gap. A scan-empty cycle takes ~1.7s of `gh pr list` plus one scanner call — at the configured concurrency this is hundreds of claude scanner invocations per hour.
- **Why it breaks:** `interval_s` is the only knob that prevents tight back-to-back claude calls in the steady state. The CLI accepts the foot-gun value and `asyncio.wait_for` handles `<=0` by raising TimeoutError immediately instead of sleeping. The "interrupted" exit print still happens on Ctrl-C, but until then the cost log climbs without bound.
- **Repro hint:** Pytest: monkeypatch `pm_agent.loop.run_one_cycle` to a stub that records each invocation timestamp and call-count; run `run_forever(repo, LoopConfig(interval_s=0))` inside `asyncio.run`, cancel after 0.5s real time; assert the recorded call-count is, say, >= 50 (would be 1-2 with a sane interval). Or simpler: directly assert `asyncio.wait_for(asyncio.Event().wait(), timeout=0)` raises TimeoutError in <10ms.
- **Confidence:** Confirmed — traced from cli.py argparse → LoopConfig → wait_for; asyncio semantics for `timeout<=0` are documented.

---
## R3-A-02: Per-finding `finally` cleanup leaks worktrees+branches on `CancelledError` mid-finding (every `await` in the finally re-raises)

- **Severity:** High
- **Type:** Resource leak / Concurrency
- **Code location:** `pm_agent/loop.py:355-373` — per-finding `finally` block with three sequential `await wm.acleanup_*(...)` calls, each wrapped in `try/except Exception`.
- **Trigger (precise):** SIGTERM (or `task.cancel()`) fires while `run_one_cycle` is awaiting *any* point inside the per-finding try (e.g. `await wm.acreate(t2.id)` on line 307, or `await _drive_coder(...)` on line 308). CancelledError is raised at that await; Python 3.11+ has `CancelledError` inheriting from `BaseException`, so the per-finding `except Exception` on line 348 does NOT catch it. Control transfers to `finally`. The first `await wm.acleanup_worktree(t1.id)` re-raises CancelledError immediately (cancelled tasks raise CancelledError at every subsequent suspension point). The `except Exception` on line 361 does NOT catch it either. So lines 363-373 (t2 + integration cleanup) are skipped entirely.
- **Why it breaks:** Per Python 3.11+ task-cancellation contract, once a task has been cancelled, every subsequent `await` raises CancelledError until the task exits or `uncancel()` is called. The cleanup block in `finally` performs three sequential awaits with no shielding. Only the first one gets a chance to even attempt running, and even it gets aborted before its body executes (the `to_thread` schedule is never reached). Net effect: `ai/T-<bug>-1`, `ai/T-<bug>-2` branches AND their worktree dirs survive into the next daemon start. The `reconcile()` orphan-branch sweep deletes branches matching `ai/*` at startup but only the branches — the *worktree directories* under `.pm-agent-worktrees/` are removed only if the daemon process restarts AND `reconcile` is called against the same repo. If the daemon is run with a different repo path, or before restart, disk fills.
- **Repro hint:** In a test, build a fake `WorktreeManager`-like with async `acreate` that sleeps 1.0s and `acleanup_worktree`/`acleanup_integration` that records invocation. Run `run_one_cycle` in a task; `task.cancel()` 0.1s in. Then assert all three cleanup methods were called (they will not be — only the first one starts and aborts).
- **Confidence:** Confirmed — Python asyncio docs + traced control flow.

---
## R3-A-03: `_ensure_finished` swallows DB errors but sets the idempotency latch anyway, leaving zombie `running` cycles on transient SQLite hiccups

- **Severity:** Medium
- **Type:** Data / Crash-recovery
- **Code location:** `pm_agent/loop.py:221-229` — `_ensure_finished` closure.
- **Trigger (precise):** Process is mid-cycle, DB file is briefly inaccessible (e.g. WAL checkpoint contention with the dashboard process holding a long read-lock; a noisy macOS Time Machine snapshot grabbing the file; sandbox briefly EBUSY). The first `_ensure_finished` call (e.g. from the success path on line 377) hits `persistence.finish_cycle` which raises `sqlite3.OperationalError("database is locked")`. The `except Exception` catches it, logs, and **still sets `cycle_finished = True`**. The outer `finally` (line 392) calls `_ensure_finished("aborted")` again — but `cycle_finished` is True, so it short-circuits without retrying. The cycle row stays as `status='running'` forever (until the next daemon restart's `reconcile`).
- **Why it breaks:** The idempotency latch is meant to prevent *double-write*, but it also prevents *retry-on-failure*. Either the latch should flip only on success, or the function should re-raise transient errors so the outer finally retries.
- **Repro hint:** Pytest: monkeypatch `persistence.finish_cycle` to raise `sqlite3.OperationalError` on first call, succeed on subsequent. Invoke `run_one_cycle` (with mocked github/scanner). Assert the cycle row's final status is NOT `'running'` (it will be, because the latch eats the retry).
- **Confidence:** Confirmed via static trace; the docstring says "Idempotent: only finish_cycle once per cycle" but the implementation conflates "idempotent" with "best-effort, no retry".

---
## R3-A-04: `init_db` only clears the calling thread's connection pool; if it's invoked while other threads (dashboard request handlers, asyncio default-executor workers) hold conns, they continue using the *old* file path and silently diverge from the canonical state.db

- **Severity:** Medium
- **Type:** Concurrency / Data
- **Code location:** `pm_agent/persistence.py:88-113` — `init_db` mutates `_DB_PATH` (module-global) and clears only `_LOCAL.__dict__` (the calling thread's local namespace).
- **Trigger (precise):** Test or operator runs:
  1. `init_db(path_A)` on main thread.
  2. Spawns a worker that does any persistence call → opens thread-local conn to `path_A`.
  3. `init_db(path_B)` is called on main thread (e.g. test using `monkeypatch.setattr` to swap DB).
  4. Worker thread re-enters `get_conn()` → `hasattr(_LOCAL, "conn")` is True (it's that thread's local), returns the *stale* conn pointing at `path_A` whose file is now deleted/unlinked by tmp_path cleanup.
  5. Worker writes/reads against a phantom file. On macOS the file's inode is still allocated to the open fd, so writes appear to succeed but go to a deleted file; the running cycle's UPDATE never reaches `path_B`. Dashboard polling on `path_B` sees no row.
- **Why it breaks:** `_LOCAL` is `threading.local()` — each thread has its own attribute namespace. Clearing the calling thread's `__dict__` doesn't propagate. The module docstring even admits this footgun but it's a real persistence-correctness hole that the test suite cannot easily catch because pytest runs single-threaded.
- **Repro hint:** Pytest spawns a `threading.Thread` that calls `start_cycle()` in a loop; from the main thread, `init_db(path_B)` is called mid-loop; observe that the worker thread's writes don't appear in `path_B`'s `cycles` table. Note: the docstring already warns ("ensure background threads are joined first") but the FastAPI dashboard process doesn't follow that contract — every HTTP request gets a thread-pool worker.
- **Confidence:** Confirmed — verified module-global `_DB_PATH` rebinding + per-thread `_LOCAL` semantics; matches docstring's own NOTE block.

---
## R3-A-05: `transaction()` does not protect against `BaseException` (KeyboardInterrupt, CancelledError) — leaves DB stuck in an open transaction with no ROLLBACK

- **Severity:** Medium
- **Type:** Concurrency / Resource (file lock)
- **Code location:** `pm_agent/persistence.py:128-147` — `transaction` context manager only catches `Exception`, not `BaseException`.
- **Trigger (precise):** `reconcile()` (only production user of `transaction`) is called at daemon startup. While inside the `with transaction() as c:` on `persistence.py:253`, the user hits Ctrl-C (uncommon during the ~50ms reconcile window, but reachable). The `KeyboardInterrupt` (or `CancelledError` during cancellation) is `BaseException`-derived. The `except Exception` block doesn't fire. There is no `finally` to attempt ROLLBACK. The connection is left with an open `BEGIN` (autocommit mode + manual BEGIN). On daemon restart, `init_db` opens a *new* connection for the new thread — the old conn is held in `_LOCAL` on the dying thread and gets GC'd at interpreter shutdown, which DOES roll back. But if the process is `kill -9`'d, the WAL file may end up with a half-applied write — though SQLite WAL handles this correctly. The more reachable harm: a future call to `transaction()` on the *same* thread (rare; reconcile is once-per-daemon) would `execute("BEGIN")` while a transaction is already open → `sqlite3.OperationalError: cannot start a transaction within a transaction`, and the reconcile would error out, daemon falls back to the outer `except Exception` retry.
- **Why it breaks:** A try/except/else without finally and without `BaseException` coverage leaves a state-changing side-effect (BEGIN) un-undone. SQLite forgives via WAL crash-recovery in most paths, but the API contract (rollback on failure) is violated for `BaseException`.
- **Repro hint:** Pytest: enter `with transaction() as c:` then raise `KeyboardInterrupt` inside; assert that the same thread can then call `transaction()` again without `OperationalError("cannot start a transaction within a transaction")`. It currently cannot.
- **Confidence:** Confirmed — control flow inspection.

---
## R3-A-06: `reconcile()` worktree-cleanup counter double-counts when both `git worktree remove` succeeds AND the dir is then `shutil.rmtree`'d

- **Severity:** Low
- **Type:** Data / Hygiene
- **Code location:** `pm_agent/persistence.py:273-284` — orphan worktree cleanup loop.
- **Trigger (precise):** `git worktree remove --force` returns rc=0 (success) AND removes the dir. Then `if d.exists():` is False, so the fallback rmtree doesn't run. `cleaned = True`, increment by 1. OK.

  Reverse case: `git worktree remove` returns rc=0 BUT the dir somehow still exists (e.g., race with `find` indexing; or git considered it a stale prunable record and didn't actually rmdir). Then `cleaned = True`, and `shutil.rmtree` runs, setting `cleaned = cleaned or (not d.exists())`. Either way bumps once. OK.

  Real bug: `git worktree remove` returns rc != 0 (e.g. "not a working tree"), `cleaned = False`. Then `if d.exists(): shutil.rmtree(...); cleaned = cleaned or (not d.exists())`. If rmtree succeeds, cleaned flips True, counter increments. So far OK.

  But: the variable `cleaned` is set in step 1 from `(rc == 0)` then *augmented* on step 2. If step 1 set cleaned=True (rc=0), step 2 might *unset* it (if not d.exists() is False because d.exists() is True after a partial git-remove). Actually `cleaned or (not d.exists())` can only *go from False to True*, never True to False. So the counter is monotonic-correct. No double-counting after all.

  Closer reading: counter never decrements, but the failure case where git fails AND rmtree fails (e.g. permission denied) still has `cleaned = False`, no increment. That's correct.
- **Why it breaks:** On further inspection this is correct. Downgrading to a no-bug observation.
- **Confidence:** Hypothesis — withdrew after trace. **Not a bug; included as audit trail.** Net findings remain below.

---
## R3-A-07: `reconcile()` deletes ANY branch matching `ai/*`, including branches owned by other tools or by a user's manual git work — no provenance check

- **Severity:** Medium
- **Type:** Data loss
- **Code location:** `pm_agent/persistence.py:286-303` — orphan branch loop.
- **Trigger (precise):** User runs the daemon against a repo they also use for other AI tools (e.g., they have a manual `ai/experiment-2026-04` branch they care about). At daemon startup, `reconcile` runs `git branch --list ai/*`, iterates, and `git branch -D` (force delete) every matched branch. Their work is gone unless they had pushed it.
- **Why it breaks:** The reconciler assumes `ai/` namespace is exclusive to pm-agent. That's a contract not documented in user-facing docs (only in spec comments). Spec §3 says the reconciler cleans "orphan git artifacts" but doesn't define ownership. A safer pattern would be: only delete branches matching `ai/T-*` or `ai/integration/*` (the actual prefixes pm-agent creates per build_coder_tasks/aintegrate code).
- **Repro hint:** Pytest: create a fake repo (use existing `fake_repo` fixture). Pre-create branches `ai/T-bug-1`, `ai/integration/cycle-1-bug`, and `ai/my-personal-experiment`. Call `reconcile(repo)`. Assert the personal experiment branch survives — it currently does not.
- **Confidence:** Confirmed — `["git", "branch", "--list", "ai/*"]` then `branch -D` on each match. No prefix filter.

---
## R3-A-08: `_drive_coder` swallows non-`Exception` failures inside the async generator and never bumps `saw_error` — `BaseException`/cancellation surfaces appear as `(True, 0.0)` "success"

- **Severity:** Medium
- **Type:** Logic / Concurrency
- **Code location:** `pm_agent/loop.py:186-203` — `_drive_coder`.
- **Trigger (precise):** During `async for ev in run_claude_async(...)`, the consumer is cancelled (CancelledError) before any `result` event arrives. The CancelledError propagates out of the `async for`, the function `_drive_coder` re-raises. So the caller in `run_one_cycle` sees CancelledError — which is correct behavior.

  BUT — if `run_claude_async` itself emits a `result` event with `is_error=True` AND `total_cost_usd` is missing (e.g. claude killed by SIGSEGV: the result event may be partial), `saw_error = True` but cost stays 0. Then `_drive_coder` returns `(False, 0.0)`. The loop calls `persistence.record_cost(cycle_id, "coder-1", 0.0)` — that's a zero-row in the costs table. The 3-cycle skip gate counts `failed` findings, so subsequent retries still happen. Fine.

  Real edge: what if `run_claude_async` emits a *system* event with subtype "timeout" but no `result`? The code on line 201 catches that: `et == "system" and st in ("timeout", "spawn_error")` → `saw_error = True`. Then `_drive_coder` returns `(False, 0.0)`. Loop marks finding `failed`. OK.

  Edge that IS broken: what if `run_claude_async` yields exactly one `result` event with `total_cost_usd` being a non-numeric type — e.g. claude's wrapper returns `"$0.05"` (string with currency symbol, hypothetical regression in claude CLI)? `float("$0.05")` raises `ValueError`. This propagates out of `_drive_coder`, the outer per-finding try catches it, marks finding `failed`. So the cycle survives but the entire cost accounting for that coder run is dropped. The `record_cost` row is never written. The `costs` table is missing the spend.
- **Why it breaks:** `float(ev.get("total_cost_usd") or 0.0)` assumes the value is None or numeric. There's no defensive try/except around the cost coercion. The fallback to 0.0 only kicks in for falsy values (None, 0, ""); non-numeric strings raise.
- **Repro hint:** Build a mock async generator that yields `{"type": "result", "total_cost_usd": "abc"}`. Call `_drive_coder` with that. Currently raises ValueError; expected behavior is to return `(False, 0.0)` and log.
- **Confidence:** Confirmed via static trace. Likelihood of triggering depends on claude CLI output stability.

---
## R3-A-09: `reconcile()` does not catch `sqlite3.OperationalError` inside `transaction()` — a locked DB on daemon startup kills the daemon before its first cycle

- **Severity:** Medium
- **Type:** Crash / Crash-recovery
- **Code location:** `pm_agent/persistence.py:253-263` — inside `with transaction()`, two UPDATEs run.
- **Trigger (precise):** Daemon starts. The dashboard process is still up from the previous daemon run and is mid-query (holding a long SELECT). `init_db` opens a conn (autocommit). `reconcile` enters `transaction()` which does `BEGIN`. The first `UPDATE cycles SET ...` tries to acquire a write lock; if the dashboard's SELECT holds a SHARED lock from a previous concurrent read, the BEGIN IMMEDIATE/exclusive escalation can fail with `sqlite3.OperationalError("database is locked")` if no `busy_timeout` is set. (Default is 0ms — no wait.)
- **Why it breaks:** Connection is opened with `sqlite3.connect(_DB_PATH, isolation_level=None)` — no `PRAGMA busy_timeout` is set. Concurrent readers will cause immediate lock-fail on contended writes. WAL mode helps (readers don't block writers in WAL) but a writer waiting on a checkpoint or a concurrent writer (rare in practice but possible during a backup tool sweep) still fails immediately. Reconcile then raises `OperationalError`, propagates out of `run_forever` (no `except` catches it before `run_one_cycle` is even entered), and the daemon dies on startup. The user's `cli.py` shows "interrupted" but the actual exit is on an unhandled exception.
- **Repro hint:** Pytest: open a second conn against the same DB and begin a transaction holding the write lock; call `persistence.reconcile(repo)` on the original conn; assert it does not raise `OperationalError`. It currently can.
- **Confidence:** Suspected — WAL semantics make this rare (readers don't block writers in WAL) but the missing `busy_timeout` PRAGMA is a real omission. To be triggerable in practice you'd need a concurrent writer; only one is expected (the daemon itself).

---
## R3-A-10: `record_pr` has no idempotency: a retried PR open (e.g. after a network blip inside `github.open_pr` that returns success on retry but the daemon doesn't know it already created a row) raises `IntegrityError` and marks the finding `failed`

- **Severity:** Low
- **Type:** Logic / Crash-recovery
- **Code location:** `pm_agent/persistence.py:208-214` — `record_pr` with `github_number INTEGER NOT NULL UNIQUE`.
- **Trigger (precise):** 
  1. Cycle N processes finding A, opens PR #42 via `gh pr create`. Network drop after gh returns success but before `record_pr` is called.
  2. The outer `except Exception` catches whatever followed, marks finding `failed`, runs finally cleanup, continues to next finding.
  3. Cycle N+1 sees the same finding A. `record_finding` returns a NEW finding_id (different cycle_id). Coder + integrate run, then `github.open_pr(branch_cycle_N+1, finding)` — but branch is `f"cycle-{N+1}-{bug_id}"`, different branch, NEW PR #43 is created. `record_pr` inserts row with `github_number=43`. OK no conflict.
  
  More realistic: `github.open_pr` is *idempotent on the branch* (line 127-129 returns existing PR). If a previous cycle had created PR #42 on branch `cycle-N-bug` but never recorded it (daemon crashed after gh API call), then on a fresh re-attempt in *the same cycle* (e.g. retry inside open_pr), the second call returns PR #42 again. Then `record_pr(finding_id, 42, ...)` succeeds (first insert). OK.
  
  Path that actually breaks: if `record_pr` is called twice for the same `gh_number` due to retried calls within the loop (currently it's called only once per finding, but if a future change adds a retry wrapper), the second insert raises `IntegrityError: UNIQUE constraint failed: prs.github_number`.
- **Why it breaks:** No `INSERT OR IGNORE` and no `record_pr_or_update` variant. The UNIQUE constraint makes the call non-retryable. Given the loop's structure of `except Exception → mark failed`, an integrity error pollutes the finding status spuriously.
- **Repro hint:** Pytest: call `record_pr(fid, 42, ...)` twice; second call currently raises. Expected: idempotent insert or upsert.
- **Confidence:** Hypothesis — current call sites don't retry, so the failure mode is one-step removed.

---
## R3-A-11: `cycles.cost_usd` denormalized column is not updated by `reconcile()` — zombie cycles aborted on restart report `$0.00` even though the `costs` table has rows

- **Severity:** Low
- **Type:** Data / Reporting drift
- **Code location:** `pm_agent/persistence.py:254-258` — `UPDATE cycles SET status='aborted', finished_at=? WHERE status='running'` (no cost_usd update).
- **Trigger (precise):** Cycle N is mid-flight when daemon is `kill -9`'d. The `costs` table has rows for `scanner` ($0.02) and `coder-1` ($0.05). The `cycles` row has `cost_usd=0.0` (the default; the cycle never reached its `finish_cycle` call). Daemon restarts; reconciler marks the cycle `aborted` but leaves `cost_usd=0.0`. The dashboard's `/api/live` and `/api/trend` endpoints read `cycles.cost_usd` (per server.py line 81) — so the cycle shows $0.00 spent. The truth is in the `costs` table summed for that cycle_id.
- **Why it breaks:** Spec §3 says "costs preserved across crashes" — they are, in the `costs` table. But the denormalized `cycles.cost_usd` is the field most consumers read. Reconcile should aggregate `SELECT SUM(usd) FROM costs WHERE cycle_id=?` and write it back on cycle abort.
- **Repro hint:** Pytest: `start_cycle()`, `record_cost(cid, "scanner", 1.5)`, then `reconcile(repo)`. Assert the cycle row's `cost_usd` is 1.5 — currently it's 0.0.
- **Confidence:** Confirmed via static trace + dashboard reading from `cycles.cost_usd`.

---
## R3-A-12: `add_signal_handler` is installed on whatever `asyncio.get_event_loop()` returns — under Python 3.12+ this emits a deprecation warning and may bind to the wrong loop if called outside `asyncio.run`

- **Severity:** Low
- **Type:** Hygiene / Compat
- **Code location:** `pm_agent/loop.py:404` — `loop = asyncio.get_event_loop()`.
- **Trigger (precise):** Under Python 3.12+, `asyncio.get_event_loop()` is deprecated when there is no current running loop and is supposed to be replaced with `get_running_loop()`. Inside `run_forever` (which IS running on a loop because it's a coroutine), `get_event_loop()` returns the running loop fine. But the deprecation warning may surface in logs in some configurations, and the API will be removed in 3.14. Functional now, future-fragile.
- **Why it breaks:** API drift, not a current crash. But target environment of "Python 3.11+" includes 3.12 and 3.13 where this is deprecated.
- **Repro hint:** Run pytest with `-W error::DeprecationWarning`; daemon emits a warning on startup.
- **Confidence:** Confirmed — Python 3.10+ deprecation note.

---

## Summary

Found **10 net-new issues** beyond rounds 1+2 (R3-A-01 through R3-A-12, minus R3-A-06 which was withdrawn after trace, and R3-A-09 marked Suspected pending real WAL contention).

Severity breakdown:
- High: 2 (R3-A-01 busy-loop on interval_s<=0; R3-A-02 cleanup leak on cancellation)
- Medium: 5 (R3-A-03 latch eats retry; R3-A-04 init_db thread footgun; R3-A-05 BaseException leaves BEGIN open; R3-A-07 over-broad branch deletion; R3-A-08 cost coercion crash; R3-A-09 missing busy_timeout)
- Low: 3 (R3-A-10 record_pr non-idempotent; R3-A-11 cost_usd not updated on reconcile; R3-A-12 get_event_loop deprecation)

Highest-confidence + highest-impact: **R3-A-01** (operator footgun, real money) and **R3-A-02** (every SIGTERM at a bad moment leaks worktree+branch artifacts).

Remaining attack surface judged clean for read-only static analysis. Areas the next hunter could probe with *runtime* triggers: actual SQLite WAL contention under load (R3-A-09 confirmation), and confirming R3-A-02 leak count under repeated SIGTERMs.
