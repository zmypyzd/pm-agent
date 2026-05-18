# TUI Daemon Extension Design

**Status:** Approved
**Date:** 2026-05-18
**Author:** brainstormed with Claude
**Related:** [2026-05-12 pm-agent Beta autonomous loop](./2026-05-12-pm-agent-beta-autonomous-loop-design.md)

## Problem

The existing `pm_agent/tui.py` (1341 lines) only covers one of two product lines:
the human-driven "give me a goal" flow (planner + 2 coders + integrate worktree).
The autonomous "Beta loop" line — `scanner → coders → gates → PR` — has no TUI
front-end; it is only visible through the FastAPI+HTMX dashboard or by tailing logs.

Operators have asked for a single TUI that surfaces both lines: real-time cycle
state, the findings/PR backlog, preflight health, and live daemon log — without
giving up the existing goal-driven workflow.

## Goals

1. Add a daemon mode to the existing TUI behind a single keystroke (`d`).
2. Show, in daemon mode: preflight health, current/last cycle summary,
   findings table, recent PR table, and live daemon log.
3. Allow operators to start and stop the daemon from inside the TUI (`s` / `x`).
4. Preserve the existing goal mode unchanged when daemon is idle.
5. Surface the same data the dashboard surfaces, using shared SQL.

## Non-goals

- Replacing the dashboard — it remains the canonical web front-end and the
  artifact for non-operators (PMs, demo viewers).
- Replacing the CLI — `pm-agent loop run` continues to be the unattended
  production entry point. The TUI is for interactive operation.
- Auto-merging via the TUI — merge policy stays in `loop.py`.
- Modifying daemon semantics — the TUI is a viewer + lifecycle controller,
  not a behavior change for the loop.

## Decisions (record of user choices)

| Question | Choice |
|---|---|
| Daemon vs goal mode coexistence | Single TUI, two Screens, `g`/`d` keys |
| TUI ↔ loop coupling | In-process `asyncio.Task` running `loop.run_forever` |
| Preflight presentation | Always-visible top bar, auto-run on entry |
| Center layout | Cycle summary row + Findings/PR stacked left + Live Log right |
| Repo path source | CLI `--repo` default, `R` opens input to override |
| Goal/daemon concurrency | Mutually exclusive — daemon running locks goal inputs |
| Code structure | Textual `Screen` split inside `tui.py` |

**Textual API surface used (verified against `textual>=8.2.5`):**

- `App.install_screen(screen, name)` — accepts Screen instance positional.
- `App.switch_screen(name_or_screen)` — non-awaiting OK.
- `App.push_screen(name_or_screen)` — non-awaiting OK.
- `Screen.is_current` — property, true when this screen is at the top of the
  app's screen stack or in background.
- `on_screen_resume(self, event)` / `on_screen_suspend(self, event)` —
  message-handler convention; delivered when `ScreenResume`/`ScreenSuspend`
  events fire on the Screen.
- `RichLog(..., max_lines=N)` — supported.

## §1 Architecture

```
PMAgentTUI (App)
├─ shared state
│   ├─ repo: Path
│   ├─ daemon_task: asyncio.Task | None
│   ├─ daemon_starting: bool                     (window between action_start_daemon
│   │                                             entry and create_task assignment)
│   ├─ daemon_stop_event: asyncio.Event | None   (lazy, see I-3)
│   ├─ daemon_last_error: BaseException | None
│   ├─ preflight_results: list[CheckResult] | None
│   ├─ loop_cfg: LoopConfig                      (built from CLI flags; see §2.8)
│   ├─ log_handler: TUILogHandler
│   ├─ log_buffer: collections.deque[str] maxlen=2000
│   └─ db_poller: DbPoller
│
├─ BINDINGS (App level)
│   "g" → switch_screen("goal")
│   "d" → switch_screen("daemon")
│   "q" → action_request_quit (graceful daemon drain + exit)
│
├─ GoalScreen (existing 5 panels, packaged as a Screen)
└─ DaemonScreen (new)
```

### Invariants

**I-1 · DB lifecycle.**
`STATE_DB` is the module constant currently defined at `pm_agent/loop.py:27`
(`Path.home() / ".pm-agent" / "state.db"`). The TUI imports it: `from pm_agent.loop import STATE_DB`.
TUI calls `persistence.init_db(STATE_DB)` exactly once in `PMAgentTUI.__init__`.
`loop.run_forever` is invoked with `skip_init=True, skip_reconcile=True`; the
daemon Screen does its own `persistence.reconcile(repo)` explicitly inside
`action_start_daemon` so the result is visible in the Live Log. WAL mode
allows concurrent reads (DbPoller) and writes (daemon).

Worker threads spawned by `asyncio.to_thread` each get their own thread-local
SQLite connection via `persistence.get_conn()`. As long as `_DB_PATH` is not
rebound after startup, this is safe — old connections continue to read the
same file. The Python default executor caps at ~32 workers, so connection
churn stays bounded.

**I-2 · Log flow.**
`TUILogHandler` is a `logging.Handler` attached to the **root** logger (not
`pm_agent`). Reason: most pm_agent modules (`runner`, `preflight`, `report`,
…) currently use `print()` rather than `logger.info()`; routing those onto
`logging` is a follow-up. Attaching at root keeps third-party debug noise
filtered via per-logger levels (`textual` and `uvicorn` clamped to WARNING)
while leaving the door open for future `print → logger` migration.

`emit()` is called from arbitrary threads (daemon's `asyncio.to_thread`
workers). It uses `self._loop.call_soon_threadsafe(self._dispatch, msg)` to
schedule a callback on the main loop. `_dispatch` runs on the main thread
and is the only place that touches the buffer or Screen widgets.

Two independent update channels: log queue carries narrative (which agent is
doing what); DbPoller carries canonical state (cycle progress, finding
status, cost). Operators see both.

**I-3 · daemon task lifecycle.**
`daemon_task` lives on the App. `daemon_stop_event` is lazy-created inside
`action_start_daemon` (not `__init__`) so that re-instantiating App across
`asyncio.run()` boundaries (tests) does not bind it to a stale event loop.

`task.add_done_callback(_on_daemon_done)` clears `daemon_task = None` and
caches the exception (if any) on `daemon_last_error`. The goal-mode mutex
predicate is `task is not None and not task.done()`.

`run_forever` is called with `install_signal_handlers=False` so it does not
override Textual's SIGINT handler.

**I-4 · Preflight is async.**
`DaemonScreen.on_mount` runs `await asyncio.to_thread(preflight.run_preflight,
STATE_DB, app.repo)` so the UI does not freeze during the 15-second
`gh auth` check. Re-runs on `p` use the same wrapper.

**I-5 · Mutual exclusion.**
`is_daemon_active` is a single App property — **must include the starting
window** so GoalScreen does not race-unlock between `daemon_starting=True`
and `daemon_task` assignment:

```python
@property
def is_daemon_active(self) -> bool:
    return self.daemon_starting or (
        self.daemon_task is not None and not self.daemon_task.done()
    )
```

When `is_daemon_active`:
- `GoalScreen` `#goal-input` is disabled with placeholder
  `"(locked — daemon running)"`.
- `GoalScreen` `r` (rerun) is a no-op with a notification.
- `DaemonScreen` `R` (set repo) is a no-op with `"stop daemon first to
  change repo"`.

All consumers read `app.is_daemon_active`, not the raw fields.

**I-6 · BINDINGS focus.**
Textual `Input` widgets swallow keys when focused. `escape` is bound on both
Screens to blur the focused Input. Operators press `esc` then `q`/`g`/`d`/
`s`/`x`/`p`/`R`.

## §2 Components

**Imports used throughout the snippets in §2** (declared once here, not
repeated in each block):

```python
import asyncio
import collections
import logging
import signal
import sqlite3

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import DataTable, Input, RichLog, Static

from pm_agent import loop, persistence, persistence_queries as queries, preflight
from pm_agent.loop import LoopConfig, STATE_DB
from pm_agent.preflight import CheckResult

log = logging.getLogger(__name__)
```

### 2.1 PMAgentTUI

```python
class PMAgentTUI(App):
    BINDINGS = [
        ("g", "switch_screen('goal')",   "Goal mode"),
        ("d", "switch_screen('daemon')", "Daemon mode"),
        ("q", "request_quit",            "Quit"),
    ]

    def __init__(self, repo, goal=None, open_daemon=False, **goal_kwargs):
        super().__init__()
        self.repo = repo
        self.goal = goal
        self._open_daemon = open_daemon
        self._goal_kwargs = goal_kwargs
        persistence.init_db(STATE_DB)                # I-1
        self.log_buffer = collections.deque(maxlen=2000)
        self.daemon_task = None
        self.daemon_starting = False
        self.daemon_stop_event = None                # I-3 lazy
        self.daemon_last_error = None
        self.preflight_results = None

    def on_mount(self):
        loop = asyncio.get_running_loop()
        root = logging.getLogger()
        for h in list(root.handlers):                # idempotent re-mount
            if isinstance(h, TUILogHandler):
                root.removeHandler(h)
        self.log_handler = TUILogHandler(loop, self.log_buffer, self)
        self.log_handler.setLevel(logging.INFO)
        self.log_handler.setFormatter(logging.Formatter(
            "%(asctime)s [%(name)s] %(levelname)s %(message)s",
            datefmt="%H:%M:%S",
        ))
        root.addHandler(self.log_handler)            # I-2 root
        logging.getLogger("textual").setLevel(logging.WARNING)
        logging.getLogger("uvicorn").setLevel(logging.WARNING)
        logging.getLogger("pm_agent").setLevel(logging.INFO)

        self.install_screen(
            GoalScreen(repo=self.repo, goal=self.goal, **self._goal_kwargs),
            name="goal",
        )
        self.install_screen(DaemonScreen(), name="daemon")

        self.db_poller = DbPoller(self)
        self.db_poller.start()

        try:
            loop.add_signal_handler(signal.SIGTERM, self.exit)
        except NotImplementedError:
            pass  # Windows

        self.push_screen("daemon" if self._open_daemon else "goal")

    def on_unmount(self):
        logging.getLogger().removeHandler(self.log_handler)
        if self.db_poller:
            self.db_poller.stop()

    def _on_daemon_done(self, task):
        try:
            task.result()
            self.daemon_last_error = None
        except asyncio.CancelledError as e:
            self.daemon_last_error = e
        except Exception as e:
            self.daemon_last_error = e
        # Note: do NOT catch SystemExit / KeyboardInterrupt here — those should
        # propagate naturally to the Textual event loop and trigger app exit.
        self.daemon_task = None
        log.info("daemon stopped: %s", self.daemon_last_error or "clean")
```

### 2.2 TUILogHandler

```python
class TUILogHandler(logging.Handler):
    def __init__(self, loop, buffer, app):
        super().__init__()
        self._loop = loop
        self._buffer = buffer
        self._app = app

    def emit(self, record):
        try:
            msg = self.format(record)
        except Exception:
            self.handleError(record); return
        self._loop.call_soon_threadsafe(self._dispatch, msg)

    def _dispatch(self, msg):
        # Runs on main loop thread
        self._buffer.append(msg)
        screen = self._app.screen_stack[-1] if self._app.screen_stack else None
        if isinstance(screen, DaemonScreen):
            screen.append_log_line(msg)
```

### 2.3 DbPoller

```python
class DbPoller:
    """Polls SQLite on two cadences. Each tick wraps a sync query in
    asyncio.to_thread; queries run on the default executor (~32 workers,
    each with its own thread-local SQLite connection). See I-1."""
    LIVE_INTERVAL = 1.0
    PRS_INTERVAL  = 30.0
    LIVE_TIMEOUT  = 2.0
    PRS_TIMEOUT   = 10.0

    def __init__(self, app):
        self._app = app
        self._live_task = None
        self._prs_task = None

    def start(self):
        self._live_task = asyncio.create_task(
            self._loop_live(), name="db-poller-live")
        self._prs_task = asyncio.create_task(
            self._loop_prs(),  name="db-poller-prs")

    def stop(self):
        for t in (self._live_task, self._prs_task):
            if t and not t.done():
                t.cancel()

    async def tick_once(self):
        live = await asyncio.wait_for(
            asyncio.to_thread(queries.live_cycle), timeout=self.LIVE_TIMEOUT)
        screen = self._current_daemon_screen()
        if screen:
            screen.update_cycle_and_findings(live)
        prs = await asyncio.wait_for(
            asyncio.to_thread(queries.recent_prs_24h), timeout=self.PRS_TIMEOUT)
        if screen:
            screen.update_prs(prs)

    async def _loop_live(self):
        while True:
            try:
                data = await asyncio.wait_for(
                    asyncio.to_thread(queries.live_cycle),
                    timeout=self.LIVE_TIMEOUT)
                screen = self._current_daemon_screen()
                if screen:
                    screen.update_cycle_and_findings(data)
            except asyncio.CancelledError:
                raise
            except sqlite3.OperationalError as e:
                log.warning("db poll (live) failed: %s", e)
            except asyncio.TimeoutError:
                log.warning("db poll (live) timeout")
            except Exception:
                log.exception("db poller live tick crashed")
            await asyncio.sleep(self.LIVE_INTERVAL)

    async def _loop_prs(self):
        while True:
            try:
                data = await asyncio.wait_for(
                    asyncio.to_thread(queries.recent_prs_24h),
                    timeout=self.PRS_TIMEOUT)
                screen = self._current_daemon_screen()
                if screen:
                    screen.update_prs(data)
            except asyncio.CancelledError:
                raise
            except sqlite3.OperationalError as e:
                log.warning("db poll (prs) failed: %s", e)
            except asyncio.TimeoutError:
                log.warning("db poll (prs) timeout")
            except Exception:
                log.exception("db poller prs tick crashed")
            await asyncio.sleep(self.PRS_INTERVAL)

    def _current_daemon_screen(self):
        if not self._app.screen_stack:
            return None
        top = self._app.screen_stack[-1]
        return top if isinstance(top, DaemonScreen) else None
```

### 2.4 pm_agent/persistence_queries.py (new)

Three pure functions, shared by dashboard and TUI.

`live_cycle()` returns `LiveCyclePayload` — current running cycle + findings
+ counts + cost + `last_finished` summary. Counts use a single query with
`SUM(CASE WHEN status='done' THEN 1 ELSE 0 END)` — SQLite supports this
since 3.x (the project requires SQLite 3.24+ for WAL anyway). Index
`idx_findings_cycle_status` covers it.

`trend_24h()` returns cumulative cost + per-cycle counts, same shape as
dashboard's current `/api/trend` body.

`recent_prs_24h()` returns last 24h PRs:

```sql
SELECT p.github_number, p.state, p.action, p.url, p.created_at,
       f.bug_id, f.title, f.severity
FROM prs p
JOIN findings f ON p.finding_id = f.id
WHERE p.created_at >= ?
ORDER BY p.id DESC
LIMIT 50
```

`?` is bound to `(datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()`.
`prs.created_at` is stored by `persistence._now()` (UTC ISO 8601), and ISO
8601 strings sort lexicographically — so a string `>=` comparison is correct
for the cutoff.

dashboard `server.py` becomes a thin wrapper that calls these functions.

### 2.5 DaemonScreen

```python
# Names must match preflight.py CheckResult.name strings verbatim.
# Verified against preflight.py:40,72,119,160,195,241,276 on 2026-05-18.
HARD_CHECK_NAMES = {
    "state.db dir writable",   # preflight.py:40
    "claude CLI on PATH",      # preflight.py:119
    "gh CLI authenticated",    # preflight.py:160
    "repo clean",              # preflight.py:195
    "/tmp writable",           # preflight.py:276
}
# Deliberately excluded:
#   "state.db clean"  — hard-fails on any prior cycle (would refuse run-2+);
#                       we demote to soft-warn here so re-launches work.
#   "repo on main"    — already warn_only=True in preflight.py:262, never a gate.

class DaemonScreen(Screen):
    BINDINGS = [
        ("s",      "start_daemon",  "Start"),
        ("x",      "stop_daemon",   "Stop"),
        ("p",      "preflight",     "Re-preflight"),
        ("R",      "focus_repo",    "Set repo"),
        ("escape", "blur_input",    ""),
    ]

    def compose(self):
        yield PreflightBar(id="preflight-bar")
        yield CycleSummaryRow(id="cycle-row")
        with Horizontal(id="main"):
            with Vertical(id="left"):
                yield Static("Findings (current cycle)", classes="panel-title")
                yield DataTable(id="findings", zebra_stripes=True)
                yield Static("Recent PRs (24h)", classes="panel-title")
                yield DataTable(id="prs", zebra_stripes=True)
            yield RichLog(id="daemon-log", wrap=True, highlight=True,
                          markup=True, max_lines=5000)
        yield Input(placeholder="repo path (press R to focus)", id="repo-input")

    async def on_mount(self):
        try:
            results, _ = await asyncio.to_thread(
                preflight.run_preflight, STATE_DB, self.app.repo)
        except Exception as e:
            log.exception("preflight crashed")
            results = [CheckResult("preflight runner", False, f"crashed: {e!r}")]
        self.app.preflight_results = results
        self.query_one(PreflightBar).update(results)

    async def on_screen_resume(self):
        log_widget = self.query_one("#daemon-log", RichLog)
        log_widget.clear()
        for line in list(self.app.log_buffer):
            log_widget.write(line)
        await self.app.db_poller.tick_once()

    async def action_start_daemon(self):
        if self.app.daemon_task and not self.app.daemon_task.done():
            self.notify("daemon already running"); return
        if self.app.daemon_starting:
            self.notify("daemon already starting"); return
        if self.app.preflight_results is None:
            self.notify("preflight not yet run; press p first"); return

        hard_fails = [r for r in self.app.preflight_results
                      if not r.ok and not r.warn_only
                      and r.name in HARD_CHECK_NAMES]
        if hard_fails:
            names = ", ".join(r.name for r in hard_fails)
            self.notify(f"preflight failed: {names}", severity="error")
            return

        for r in self.app.preflight_results:
            if not r.ok and not r.warn_only and r.name not in HARD_CHECK_NAMES:
                log.info("preflight soft-warn: %s — %s", r.name, r.message)

        self.app.daemon_starting = True
        try:
            self.app.daemon_stop_event = asyncio.Event()  # I-3 lazy
            rec = await asyncio.to_thread(persistence.reconcile, self.app.repo)
            log.info("reconcile: %s", rec)

            self.app.daemon_last_error = None
            # B3: pass through CLI-supplied LoopConfig fields. main() builds this
            # from --coder-timeout / --test-cmd / --test-timeout / --max-retries
            # and stores it on the App as self.app.loop_cfg.
            self.app.daemon_task = asyncio.create_task(
                loop.run_forever(
                    self.app.repo, self.app.loop_cfg,
                    install_signal_handlers=False,
                    skip_init=True, skip_reconcile=True,
                    stop_event=self.app.daemon_stop_event,
                ),
                name="pm-agent-loop",
            )
            self.app.daemon_task.add_done_callback(self.app._on_daemon_done)
        finally:
            self.app.daemon_starting = False

    async def action_stop_daemon(self):
        if self.app.daemon_starting:
            self.notify("daemon still starting; try again in a moment"); return
        t = self.app.daemon_task
        if not t or t.done():
            self.notify("no running daemon"); return
        self.app.daemon_stop_event.set()
        self.notify("stop signal sent; daemon will finish current cycle")

    async def action_preflight(self):
        try:
            results, _ = await asyncio.to_thread(
                preflight.run_preflight, STATE_DB, self.app.repo)
        except Exception as e:
            log.exception("preflight crashed")
            results = [CheckResult("preflight runner", False, f"crashed: {e!r}")]
        self.app.preflight_results = results
        self.query_one(PreflightBar).update(results)

    def action_focus_repo(self):
        if self.app.is_daemon_active:
            self.notify("stop daemon first to change repo"); return
        self.query_one("#repo-input", Input).focus()
```

### 2.6 PreflightBar / CycleSummaryRow

Custom `Static`-derived widgets. PreflightBar shows 7 cells, one row,
` ✓ db │ ✓ cli │ ✗ gh │ … `. CycleSummaryRow shows one of:

- `daemon crashed: <reason> — press s to restart` (red, when `daemon_last_error`)
- `idle  last cycle #N done $X.XXXX at HH:MM`
- `cycle #N <status>  PR opened M/T  $X.XXXX  elapsed Xs`

Column label is `"PR opened"` not `"fixed"` — `status='done'` corresponds
to PR opened, not PR merged.

### 2.7 GoalScreen

Existing PMAgentTUI body refactored into a Screen class. `on_screen_resume`
refreshes the disable state of `#goal-input` and `r` action based on
`app.is_daemon_active` (see I-5). Otherwise unchanged.

**`ARTIFACTS_ROOT` constraint.** The module-level constant `pm_agent.tui.
ARTIFACTS_ROOT` (currently `tui.py:151`) MUST remain a module-level constant
in `tui.py` after the refactor. `tests/conftest.py:24-30` monkey-patches it
to redirect artifact writes into the test's tmp dir; moving it into a class
would silently break the test fixture.

### 2.8 main() argv

Adds `--daemon` (boolean) to the existing argparse setup. `cli.py`'s
argv-hack invocation continues to work; `tests/test_cli.py:212` unchanged.

**Existing flags route to daemon mode.** `main()` builds a `LoopConfig` from
the same flags the goal mode already accepts, and assigns it to `app.loop_cfg`
so `action_start_daemon` can pass it into `loop.run_forever`:

```python
loop_cfg = LoopConfig(
    coder_timeout=args.coder_timeout,
    test_timeout=args.test_timeout,
    max_retries=args.max_retries,
    # interval_s and blocklist keep LoopConfig defaults; CLI flag for
    # interval_s is a follow-up (see Out of scope).
)
app = PMAgentTUI(repo=..., goal=..., open_daemon=args.daemon, loop_cfg=loop_cfg,
                 # remaining kwargs go to GoalScreen as before
                 single=args.single, test_cmd=args.test_cmd, ...)
```

`args.test_cmd` is consumed by GoalScreen (as today). LoopConfig itself does
not have a `test_cmd` field — `loop.run_gates` always uses `pytest`. If the
user supplies `--test-cmd` in daemon mode it is silently ignored (documented
limitation; daemon's gate command is fixed by spec).

### 2.9 loop.run_forever signature

```python
async def run_forever(
    repo: Path,
    cfg: LoopConfig | None = None,
    *,
    install_signal_handlers: bool = True,
    skip_init: bool = False,
    skip_reconcile: bool = False,
    stop_event: asyncio.Event | None = None,
) -> None:
```

CLI path retains current behavior via defaults. TUI passes
`install_signal_handlers=False, skip_init=True, skip_reconcile=True,
stop_event=<own>`. Uses `asyncio.get_running_loop()` (3.12-clean).

## §3 Data flow (sequence)

### Startup → first daemon screen

```
cli.cmd_tui (argv hack) → tui.main()
→ PMAgentTUI.__init__ (init_db, log_buffer)
→ App.run() → on_mount (handler, screens, poller, push_screen)
→ DaemonScreen.on_mount (async preflight, ~15s, UI responsive)
→ DaemonScreen.on_screen_resume (log replay, tick_once)
```

### `s` press → daemon start

`action_start_daemon`: mutex check → preflight gate → lazy stop_event →
reconcile (await to_thread) → `asyncio.create_task(run_forever(...))` →
add_done_callback → return.

### Log flow (both main-thread and worker-thread)

```
log.info(...)
→ TUILogHandler.emit  (runs on caller's thread)
→ self._loop.call_soon_threadsafe(self._dispatch, msg)
→ main loop tick: _dispatch(msg)
   → buffer.append(msg)
   → if current screen is DaemonScreen: RichLog.write(msg)
```

### SQLite poll

```
DbPoller._loop_live (main loop)
→ each 1s: await asyncio.wait_for(to_thread(queries.live_cycle), 2.0)
→ if DaemonScreen current: update_cycle_and_findings(payload)
DbPoller._loop_prs: same pattern, 30s / 10s timeout
```

### Screen switch

```
press 'g' on Daemon screen:
  DaemonScreen.on_screen_suspend  (timestamp only)
  GoalScreen.on_screen_resume     (refresh mutex disable state)

press 'd' back:
  GoalScreen.on_screen_suspend
  DaemonScreen.on_screen_resume:
    RichLog.clear() + replay log_buffer
    await db_poller.tick_once()
```

### `x` press → graceful stop

`action_stop_daemon`: `stop_event.set()` → notify → return.
Daemon checks `stop_event` between findings and after the cycle's
`asyncio.wait_for(stop_event.wait(), timeout=interval_s)`, returns
cleanly. `_on_daemon_done` clears `daemon_task`.

### `q` press → exit

```
action_request_quit:
  if daemon active:
    stop_event.set()
    notify("draining daemon... (30s max)")
    try: await wait_for(daemon_task, timeout=30)
    except TimeoutError:
      daemon_task.cancel()
      notify("forced exit; worktree cleanup may be incomplete")
      try: await daemon_task; except: pass
  self.exit()
```

On_unmount removes log handler and cancels DbPoller tasks.

### Daemon crash

`run_forever` swallows almost everything per spec, but if a `BaseException`
bubbles up, `_on_daemon_done` captures it in `daemon_last_error`. The
CycleSummaryRow next tick shows `daemon crashed: <reason>`. Goal mode is
unlocked.

## §4 Error handling

### Daemon-side errors (mirrored, never modified by TUI)

| Failure | run_forever behavior | TUI surface |
|---|---|---|
| `GhAuthError` | break loop, return | RichLog "gh auth failure"; cycle row "daemon crashed: ..." |
| scanner crash | finish_cycle('errored'); next cycle | log.exception in RichLog; cycles table updated |
| coder timeout | finding marked 'failed'; next finding | findings table row red; log shows timeout |
| coder SIGKILL | same path as timeout | same |
| pytest/mypy/ruff fail | finding marked 'failed'; no PR | log shows gate stderr; finding row red |
| unexpected per-cycle exception | log.exception, continue next interval | log error; daemon self-heals |

### TUI-side errors

- **Preflight crash:** caught in `on_mount` / `action_preflight`; PreflightBar
  shows a synthetic "preflight runner ✗ crashed" cell, gate refuses start.
- **Rapid `s` presses:** first-line mutex check is idempotent.
- **`x` during startup:** `app.daemon_starting` flag covers the window
  between `action_start_daemon` entry and `daemon_task = ...` assignment.
  `action_stop_daemon` notifies "daemon still starting" if seen.
- **Invalid repo path on `R`:** validate `is_dir()` and `(path/.git).is_dir()`;
  reject with notification; do not mutate `app.repo`.
- **SQLite OperationalError / lock / corruption:** DbPoller swallows
  `sqlite3.OperationalError`, logs a warning, skips one tick. Never lets a
  poll error kill the task. `CancelledError` re-raises.
- **Long-running query (deadlock, slow disk):** `asyncio.wait_for` per tick
  with `LIVE_TIMEOUT=2.0` and `PRS_TIMEOUT=10.0`.

### Resource cleanup

- **Subprocesses on quit:** Coder claude processes get `proc.terminate()` via
  `runner.run_claude_async` finally clause when their task is cancelled.
  `run_gates` runs sync `subprocess.run(timeout=300)` in a worker thread —
  cancel does **not** propagate into the subprocess. Documented limitation:
  quitting during gates may leave a pytest/mypy/ruff process running until
  it self-times out.
- **Worktree leftovers:** `persistence.reconcile` runs on next daemon start
  and cleans up. The TUI explicitly calls it in `action_start_daemon`.
- **Log handler:** removed in `on_unmount`; re-mount removes any stale
  instance first.
- **DbPoller tasks:** cancelled in `stop()`. `on_unmount` calls `stop()`.

### Concurrency edges

- **Screen suspend mid-tick:** poll continues fetching but `update_*` calls
  skip when no DaemonScreen is current. Next `tick_once` on resume catches
  up.
- **Rapid `g`/`d` switching:** install_screen instances are persistent;
  `on_screen_resume` does a buffer replay (deque ≤2000 lines, ms-cost).
- **SIGTERM:** `loop.add_signal_handler(SIGTERM, app.exit)` installed in
  `on_mount` so `kill <pid>` routes through the graceful path.

### UI degradation prevention

- **Silent daemon death:** `_on_daemon_done` is the canonical notification;
  CycleSummaryRow renders red "daemon crashed" with the reason.
- **Stale preflight:** automatically re-run when `R` switches repo; `p`
  manual refresh otherwise.
- **RichLog unbounded growth:** `max_lines=5000` caps render-side memory.

### Edge case test matrix

Minimum 12 automated, rest manual (see acceptance criteria below).

| # | Scenario | Expected |
|---|---|---|
| E1 | `--repo` points at nonexistent path | Preflight `repo clean` red; gate refuses start |
| E2 | `--repo` is a non-git directory | `git status` rc != 0 → `repo clean` fails |
| E3 | claude CLI not installed | Preflight `claude CLI` red |
| E4 | `gh auth status` fails | Preflight `gh auth` red |
| E5 | STATE_DB parent unwritable | `state.db dir writable` red |
| E6 | DB already contains cycles | `state.db clean` warn (not red); gate still admits |
| E7 | Daemon starts → 5 s GhAuthError → self-stops | `daemon_last_error` set; task cleared; CycleSummaryRow red "daemon crashed" |
| E8 | Rapid `d`/`g` switching | Log replay holds; cycle table keeps refreshing |
| E9 | Quit while cycle is mid-finding | 30 s drain; cancel; "forced exit" notice |
| E10 | Quit while reconcile is running | `action_request_quit` awaits `daemon_starting` to clear (max 30 s) before exit; reconcile is a `to_thread` call and completes before `daemon_starting=False` runs |
| E11 | DB file deleted externally | DbPoller swallows OperationalError; UI freezes its values; log warning |
| E12 | Preflight runner itself crashes | Synthetic "preflight runner ✗" cell; gate refuses |
| E13 | `--daemon` boot with all-failing preflight | Daemon screen shows red preflight; `g` returns to working goal mode |
| E14 | `R` to a valid new repo while daemon is stopped | Preflight re-runs; `s` then uses new repo |
| E15 | Pytest re-instantiates `PMAgentTUI` in one process | No TUILogHandler accumulation; no DbPoller task leak |


## §5 Testing

### Stack

- pytest + pytest-asyncio.
- Textual `App.run_test()` pilot for integration.
- Mock boundaries: `fake_claude.py` script, monkey-patch `github._gh`, tmp
  git repo, tmp `STATE_DB`.

### Unit tests

- `tests/test_persistence_queries.py` — pure SQL behavior, including idle
  vs running, `findings_pr_opened`, OperationalError pass-through.
- `tests/test_tui_log_handler.py` — emit from main thread, emit from
  worker thread, buffer maxlen, drop-when-no-screen, format exception.
- `tests/test_db_poller.py` — tick cadence, error swallowing, timeout,
  stop cancels.
- `tests/test_widgets.py` — PreflightBar / CycleSummaryRow renderings
  across all states.

### Integration tests (`App.run_test`)

- App starts in goal mode by default.
- `d` switches to daemon Screen and runs preflight.
- preflight failure on `gh auth` blocks `s`.
- `R` is locked while daemon is active.
- `g`/`d` switching preserves log replay.

### End-to-end mini-cycle

- `tests/test_daemon_full_minicycle.py` — fake claude returns one finding
  + coder NO_CHANGES; mock gh push and PR; assert 1 cycle + 1 finding +
  1 PR in DB, CycleSummaryRow transitions to "last cycle …" after stop.

### Regression gates (must still pass unchanged)

- `tests/test_cli.py:212` (argv hack survives).
- `tests/test_loop.py` (CLI loop path unchanged).
- `tests/test_persistence.py`.
- `tests/conftest.py` ARTIFACTS_ROOT patch.

### Performance

- DbPoller 10-minute soak: memory growth < 5 MB; connection pool ≤ 30.
- RichLog 8-hour daemon soak: nightly job only, not CI.

### Acceptance criteria

1. All new unit tests pass; coverage ≥ 80% on new files.
2. All existing tests pass unchanged.
3. mypy clean on new files.
4. ruff clean.
5. Manual: `pm-agent tui --daemon --repo <fake-git>` against fake_claude
   completes one cycle visually correct.
6. Manual: `pm-agent tui` without `--daemon` is byte-equivalent to
   pre-change behavior.
7. Manual: dashboard and TUI running against same SQLite show consistent
   numbers.
8. Manual: `pm-agent loop run` CLI unchanged.
9. Manual: Ctrl-C drains daemon within 30 s in normal cases.
10. Edge cases E1–E15: ≥ 12 automated, rest manual.

## Out of scope (follow-up tickets)

- Migrate `runner.py` / `preflight.py` / `report.py` `print()` calls to
  `logging` so claude session-id / cost / exit reaches the TUI log.
- pytest-textual-snapshot tests (visual regression).
- dashboard rendering `last_finished_cycle` and `findings_pr_opened` in its
  UI (data already available via API).
- Long-running soak benchmarks as a CI job.
- Removing the argv-hack from `cli.cmd_tui` and migrating to a kwarg
  invocation — bundled with a `tests/test_cli.py:212` rewrite.
- `--interval-s` CLI flag and `--blocklist` CLI flag (daemon currently uses
  `LoopConfig` defaults: 1800s interval, fixed blocklist tuple).
- Configurable graceful-drain timeout (currently hardcoded 30 s in
  `action_request_quit`).
- First-time-operator UX: a one-line hint banner explaining `s`/`x`/`p`/`R`
  bindings when DaemonScreen first mounts.

## Files touched

| File | Change |
|---|---|
| `pm_agent/tui.py` | Refactor `PMAgentTUI` into App+two Screens; add `TUILogHandler`, `DbPoller`, `PreflightBar`, `CycleSummaryRow`. |
| `pm_agent/loop.py` | Add `install_signal_handlers / skip_init / skip_reconcile / stop_event` kwargs; switch to `asyncio.get_running_loop()`. |
| `pm_agent/persistence_queries.py` | New: shared SQL for dashboard + TUI. |
| `pm_agent/dashboard/server.py` | Thin wrappers around new `queries` module; add `findings_pr_opened` to `LiveCycleResponse` (additive field; HTMX template ignores unknown fields, no template update required in this PR). |
| `pm_agent/cli.py` | Unchanged (argv-hack preserved). |
| `tests/conftest.py` | Unchanged. |
| `tests/test_cli.py` | Unchanged. |
| `tests/test_persistence_queries.py` | New. |
| `tests/test_tui_log_handler.py` | New. |
| `tests/test_db_poller.py` | New. |
| `tests/test_widgets.py` | New. |
| `tests/test_daemon_full_minicycle.py` | New. |
