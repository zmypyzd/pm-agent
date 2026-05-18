"""TUI for pm-agent.

Modes:

  1. Mock mode (no goal argv): day-2 ticker over fake data.
       uv run python -m pm_agent.tui

  2. Single-coder real mode (--single): day-3/4 wiring. One claude -p
     subprocess streams into the right panel.
       uv run python -m pm_agent.tui --single "what is 2+2"

  3. Multi-coder mode (default for non-mock): day-6 wiring.
     A real Planner agent (claude with structured-YAML system prompt)
     decomposes the goal into 2 file-disjoint subtasks; two Coders run
     in parallel, each in its own git worktree. Falls back to
     mock_planner_decompose if Planner fails parse/validation.
       uv run python -m pm_agent.tui "add cost CSV export"
     Force fast mock planner with --mock-planner.

Five panels (per design doc):
  top    — goal + progress bar (advances as tasks complete)
  left   — task table
  center — agent status cards
  right  — RichLog stream of all subprocesses
  bottom — cost / events / elapsed

q to quit.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from rich.markup import escape as _rich_escape
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import DataTable, Input, Label, ProgressBar, RichLog, Static

from pm_agent.planner import PlannerError, plan
from pm_agent.runner import run_claude_async
from pm_agent.tasks import CoderTask
from pm_agent.worktree import IntegrationResult, WorktreeManager

import collections
import logging as _logging  # avoid clashing with the existing `log` variable used in this module
import signal
import sqlite3



class TUILogHandler(_logging.Handler):
    """Bridge stdlib logging → Textual RichLog, thread-safe.

    emit() may be called from any thread (e.g. asyncio.to_thread workers).
    It schedules _dispatch on the main event loop via call_soon_threadsafe,
    where it is safe to touch the buffer and Screen widgets.
    """

    def __init__(self, loop, buffer, app):
        super().__init__()
        self._loop = loop
        self._buffer = buffer
        self._app = app

    def emit(self, record: _logging.LogRecord) -> None:
        try:
            msg = self.format(record)
        except Exception:
            self.handleError(record)
            return
        self._loop.call_soon_threadsafe(self._dispatch, msg)

    def _dispatch(self, msg: str) -> None:
        """Runs on the main event loop thread."""
        self._buffer.append(msg)
        screen = self._app.screen_stack[-1] if self._app.screen_stack else None
        if isinstance(screen, DaemonScreen):
            screen.append_log_line(msg)


class DbPoller:
    """Polls SQLite on two cadences. Each tick wraps a sync query in
    asyncio.to_thread; queries run on the default executor (≤32 workers,
    each with its own thread-local SQLite connection)."""

    LIVE_INTERVAL = 1.0
    PRS_INTERVAL = 30.0
    LIVE_TIMEOUT = 2.0
    PRS_TIMEOUT = 10.0

    def __init__(self, app):
        self._app = app
        self._live_task = None
        self._prs_task = None

    def start(self) -> None:
        self._live_task = asyncio.create_task(self._loop_live(), name="db-poller-live")
        self._prs_task = asyncio.create_task(self._loop_prs(),  name="db-poller-prs")

    def stop(self) -> None:
        for t in (self._live_task, self._prs_task):
            if t is not None and not t.done():
                t.cancel()

    async def tick_once(self) -> None:
        """Run one live + prs query immediately. Used by Screen resume."""
        from pm_agent import persistence_queries as _q
        live = await asyncio.wait_for(
            asyncio.to_thread(_q.live_cycle), timeout=self.LIVE_TIMEOUT)
        screen = self._current_daemon_screen()
        if screen is not None:
            screen.update_cycle_and_findings(live)
        prs = await asyncio.wait_for(
            asyncio.to_thread(_q.recent_prs_24h), timeout=self.PRS_TIMEOUT)
        if screen is not None:
            screen.update_prs(prs)

    def _current_daemon_screen(self):
        if not self._app.screen_stack:
            return None
        top = self._app.screen_stack[-1]
        return top if isinstance(top, DaemonScreen) else None

    async def _loop_live(self):
        from pm_agent import persistence_queries as _q
        while True:
            try:
                data = await asyncio.wait_for(
                    asyncio.to_thread(_q.live_cycle), timeout=self.LIVE_TIMEOUT)
                screen = self._current_daemon_screen()
                if screen is not None:
                    screen.update_cycle_and_findings(data)
            except asyncio.CancelledError:
                raise
            except sqlite3.OperationalError as e:
                _logging.getLogger(__name__).warning("db poll (live) failed: %s", e)
            except asyncio.TimeoutError:
                _logging.getLogger(__name__).warning("db poll (live) timeout")
            except Exception:
                _logging.getLogger(__name__).exception("db poller live tick crashed")
            await asyncio.sleep(self.LIVE_INTERVAL)

    async def _loop_prs(self):
        from pm_agent import persistence_queries as _q
        while True:
            try:
                data = await asyncio.wait_for(
                    asyncio.to_thread(_q.recent_prs_24h), timeout=self.PRS_TIMEOUT)
                screen = self._current_daemon_screen()
                if screen is not None:
                    screen.update_prs(data)
            except asyncio.CancelledError:
                raise
            except sqlite3.OperationalError as e:
                _logging.getLogger(__name__).warning("db poll (prs) failed: %s", e)
            except asyncio.TimeoutError:
                _logging.getLogger(__name__).warning("db poll (prs) timeout")
            except Exception:
                _logging.getLogger(__name__).exception("db poller prs tick crashed")
            await asyncio.sleep(self.PRS_INTERVAL)


class PreflightBar(Static):
    """One-row, seven-cell preflight status bar."""

    DEFAULT_CSS = """
    PreflightBar {
        height: 1;
        background: $panel;
        padding: 0 1;
    }
    """

    _markup: str = ""

    @property
    def renderable(self) -> str:
        """Return the raw markup string; str(bar.renderable) includes color tags."""
        return self._markup

    def update_from(self, results) -> None:
        """Replace bar content with rendered cells.

        `results` is the list returned by `preflight.run_preflight(...)[0]`.
        """
        cells = []
        for r in results:
            if r.ok:
                symbol, color = "✓", "green"
            elif r.warn_only:
                symbol, color = "⚠", "yellow"
            else:
                symbol, color = "✗", "red"
            # Shorten name to ≤14 chars to keep 7 cells on one row
            short = (
                r.name
                .replace("state.db ", "db ")
                .replace("repo on main", "main")
                .replace("claude CLI on PATH", "claude")
                .replace("gh CLI authenticated", "gh")
            )
            cells.append(f"[{color}]{symbol} {short}[/]")
        self._markup = " │ ".join(cells)
        self.update(self._markup)


from datetime import datetime as _dt_datetime, timezone as _dt_timezone


class CycleSummaryRow(Static):
    """One-row cycle summary above the Findings/PR tables."""

    DEFAULT_CSS = """
    CycleSummaryRow {
        height: 1;
        background: $boost;
        padding: 0 1;
    }
    """

    _last_error: object | None = None
    _markup: str = ""

    @property
    def renderable(self):
        """Test-friendly access to the raw Rich markup string.

        Textual 8.2.5's Static does not expose .renderable on unmounted
        widgets; the property here lets unit tests assert on the markup
        without mounting the widget into an App.
        """
        return self._markup

    def set_last_error(self, err) -> None:
        self._last_error = err

    def clear_last_error(self) -> None:
        self._last_error = None

    def update_from(self, payload) -> None:
        if self._last_error is not None:
            reason = _rich_escape(str(self._last_error))[:80]
            self._markup = (
                f"[red]daemon crashed:[/] {reason}  "
                f"[grey50]press 's' to restart[/]"
            )
        elif payload.status == "idle":
            lf = payload.last_finished
            if lf is None:
                self._markup = (
                    "[grey50]daemon idle — press 's' to start "
                    "(no prior cycle)[/]"
                )
            else:
                when = lf.finished_at or lf.started_at
                short = when.split("T")[1][:5] if "T" in when else when
                self._markup = (
                    f"[grey50]idle[/]  last cycle [bold]#{lf.id}[/] "
                    f"{lf.status}  ${lf.cost_usd:.4f}  at {short}"
                )
        else:
            c = payload.cycle
            elapsed = self._fmt_elapsed(c.started_at)
            self._markup = (
                f"cycle [bold]#{c.id}[/]  [yellow]{c.status}[/]  "
                f"PR opened [green]{c.findings_pr_opened}[/]/"
                f"{c.findings_total}  "
                f"${c.cost_usd:.4f}  elapsed {elapsed}"
            )
        self.update(self._markup)

    @staticmethod
    def _fmt_elapsed(started_at: str) -> str:
        try:
            t0 = _dt_datetime.fromisoformat(started_at)
            if t0.tzinfo is None:
                t0 = t0.replace(tzinfo=_dt_timezone.utc)
            secs = int((_dt_datetime.now(_dt_timezone.utc) - t0).total_seconds())
            return f"{secs}s" if secs < 60 else f"{secs // 60}m{secs % 60}s"
        except ValueError:
            return started_at


GOAL_MOCK = "Add room invite link API to werewolf platform (mock)"

# Day 10 polish: status icons + colors used by agent cards and task table.
STATUS_ICONS = {
    "running": "▶",
    "done":    "✓",
    "failed":  "✗",
    "idle":    "⏸",
    "blocked": "⏸",
    "ready":   "·",
    "timeout": "✗",
    "api_error": "✗",
}
STATUS_COLORS = {
    "running":   "yellow",
    "done":      "green",
    "failed":    "red",
    "idle":      "grey50",
    "blocked":   "red",
    "ready":     "grey50",
    "timeout":   "orange1",
    "api_error": "red",
}

AGENTS_INITIAL = [
    ("Planner",  "done",     "Decomposed into 3 tasks"),
    ("Coder-1",  "running",  "Editing server/rooms/api.py"),
    ("Coder-2",  "running",  "Writing tests for invite expiry"),
]

TASKS_MOCK = [
    ("T-1", "Backend invite API",  "running"),
    ("T-2", "Tests for expiry",    "running"),
    ("T-3", "Frontend join page",  "blocked"),
]

MOCK_LOG_LINES = [
    "[Planner] Tasks decomposed (3)",
    "[Coder-1] git worktree add ../wt-T-1",
    "[Coder-1] Editing server/rooms/api.py",
    "[Coder-2] Writing pytest fixture",
    "[Coder-2] Test 1 passed",
    "[Coder-1] Lint clean",
    "[Reviewer] Waking up",
]


CODER_COMMIT_SUFFIX = """

When you finish making your code changes:
1. Stage your changes:  git add -A
2. Commit them:          git commit -m "{task_id}: <one-line summary>"
3. Print exactly: DONE

If you made no file changes, instead print: NO_CHANGES
Stay strictly inside this worktree directory. Do not push, do not switch branches.
"""


# BUG-006: Coder needs to know its sandbox boundary explicitly. Without this,
# the Coder happily writes outside its allowed_paths and the merge step
# catches the conflict after the cost was already burned.
#
# Tone matters: a too-strict "stop and explain" wording made the Coder bail
# when a sibling task's output was needed (e.g. T-2's test needs T-1's
# /health). We soften to "prefer the allowed paths; do your task even if
# external state isn't yet present — other Coders may be working in
# parallel".
CODER_CONSTRAINTS_PREFIX = """\
TASK BOUNDARY (read first):
  You are Coder for task {task_id}, running in parallel with sibling Coders.
  Allowed paths — prefer editing only these:
{allowed_paths_block}
  Acceptance criteria — your output must satisfy:
{acceptance_block}

Sibling Coders may be modifying other files concurrently in their own git
worktrees. Trust them to produce what your acceptance criteria reference
(routes, modules, fixtures). Write your code as if their work is in place.
Stay within the Allowed paths whenever the task allows it; do not create
unrelated files outside that list.

---

YOUR TASK:

"""


def _safe(s: object) -> str:
    """Escape user / LLM-controlled strings for Rich markup contexts (BUG-039 / 053).
    Returns the input rendered as literal text so '[red]EVIL[/]' shows as
    '[red]EVIL[/]' rather than red EVIL.
    """
    return _rich_escape(str(s))


ARTIFACTS_ROOT = Path.home() / ".pm-agent" / "runs"


def mock_planner_decompose(goal: str) -> list[CoderTask]:
    """Cheap fallback when --mock-planner is set or the real planner errors.
    Returns 2 trivially parallelizable subtasks."""
    return [
        CoderTask(
            id="T-1",
            title="Explain asyncio.gather",
            prompt="In 5 words, what does asyncio.gather do?",
            allowed_paths=["mock/T-1/**"],
            acceptance=["claude returns a 5-word string"],
        ),
        CoderTask(
            id="T-2",
            title="Explain git worktree",
            prompt="In 5 words, what does git worktree do?",
            allowed_paths=["mock/T-2/**"],
            acceptance=["claude returns a 5-word string"],
        ),
    ]


class GoalScreen(Screen):
    """Day-5 — multi-coder via git worktree + asyncio.gather.

    Task 10 (TUI daemon extension): extracted from the prior
    ``PMAgentTUI(App)`` into a ``Screen`` so that the App-level shell
    (added in Task 12) can host both this goal-mode UI and the
    upcoming ``DaemonScreen``. ``PMAgentTUI`` remains as a temporary
    module-level alias to this class until Task 12 lands the real App.
    """

    CSS = """
    Screen { layout: vertical; }

    #goal-bar {
        height: 4;
        background: $primary 30%;
        padding: 0 1;
        border: solid $primary;
    }
    #goal-bar Label { text-style: bold; }
    #progress-row { height: 1; }
    #progress { width: 1fr; }
    #task-count {
        width: auto;
        padding: 0 1;
        color: $text;
    }

    #main { height: 1fr; }

    #left, #center, #right {
        height: 100%;
        border: solid $secondary;
        padding: 0 1;
    }
    #left   { width: 28%; }
    #center { width: 36%; }
    #right  { width: 1fr; }

    .panel-title {
        text-style: bold;
        background: $boost;
        height: 1;
        margin: 0 0 1 0;
    }

    .agent-card {
        height: 3;
        margin: 0 0 1 0;
        padding: 0 1;
        border: solid $accent;
    }
    .status-running { color: $warning; }
    .status-done    { color: $success; }
    .status-idle    { color: $text-muted; }
    .status-blocked { color: $error; }
    .status-failed  { color: $error; }

    #footer-bar {
        height: 3;
        background: $secondary 40%;
        padding: 0 1;
    }

    #goal-input {
        height: 3;
        background: $surface;
        border: solid $accent;
        margin: 0 0 0 0;
    }
    """

    # Task 10: q/quit lives on the App in Task 12, not on the Screen.
    BINDINGS = [
        ("r", "rerun", "Re-run"),
        ("n", "focus_input", "New goal"),
        ("escape", "blur_input", ""),
    ]

    _start_time: float = 0.0
    _tick_counter: int = 0
    _event_count: int = 0
    _cost_usd: float = 0.0
    _tasks_done: int = 0
    _streamed_text: str = ""
    _session_complete: bool = False

    def __init__(
        self,
        goal: str | None = None,
        repo: Path | None = None,
        single: bool = False,
        use_real_planner: bool = True,
        test_cmd: str | None = None,
        coder_timeout: float = 180.0,
        inject_fault: str | None = None,
        interactive: bool = False,
        max_retries: int = 2,
        test_timeout: float = 120.0,
    ) -> None:
        super().__init__()
        self.goal = goal
        self.max_retries = max_retries
        self.test_timeout = test_timeout
        # Day 15: interactive mode lets the user type goals in a TUI Input
        # widget and run them back-to-back without restarting the process.
        # With interactive=True we are NOT in mock mode even when goal=None;
        # we just sit and wait for the first input.
        self.interactive = interactive
        self.is_mock = goal is None and not interactive
        self.single = single
        self.use_real_planner = use_real_planner
        self.test_cmd = test_cmd
        self.coder_timeout = coder_timeout
        self.repo = repo
        # Day 11: fault injection for the recovery-flow demo. None = normal run.
        # Valid values: "planner-yaml", "coder-timeout", "api-error".
        self.inject_fault = inject_fault
        self.wm: WorktreeManager | None = None
        if not self.is_mock and repo is not None:
            self.wm = WorktreeManager(repo)
        self._tasks: list[CoderTask] = []
        self._run_id: str = (time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4])
        self._artifacts_dir: Path = ARTIFACTS_ROOT / self._run_id
        self._task_diffs: dict[str, str] = {}
        self._task_errors: dict[str, str] = {}
        self._rate_limit_events: list[dict] = []
        self._integration: IntegrationResult | None = None

    def _goal_text(self) -> str:
        if self.goal:
            display = self.goal
        elif self.is_mock:
            display = GOAL_MOCK
        else:
            # Interactive mode, waiting for first input.
            display = "(type a goal in the input bar below and press Enter)"
        text = f"Goal: {display}"
        if self.inject_fault:
            text += f"   [bold red on white] FAULT: {self.inject_fault} [/]"
        return text

    def compose(self) -> ComposeResult:
        with Vertical(id="goal-bar"):
            yield Label(self._goal_text(), id="goal")
            with Horizontal(id="progress-row"):
                yield ProgressBar(total=100, show_eta=False, id="progress")
                yield Label("0/0 tasks", id="task-count")
        with Horizontal(id="main"):
            with Vertical(id="left"):
                yield Static("Tasks", classes="panel-title")
                yield DataTable(id="tasks", show_header=True, zebra_stripes=True)
            with Vertical(id="center"):
                yield Static("Active Agents", classes="panel-title")
                for name, status, action in AGENTS_INITIAL:
                    yield Static(
                        self._agent_card_text(name, status, action),
                        id=f"agent-{name}",
                        classes=f"agent-card status-{status}",
                    )
            with Vertical(id="right"):
                yield Static("Live Log", classes="panel-title")
                yield RichLog(id="logs", wrap=True, highlight=True, markup=True)
        if self.interactive:
            yield Input(
                placeholder="Type goal, Enter to run · esc to defocus · n to refocus",
                id="goal-input",
            )
        yield Static(self._footer_text(0.0, 0, 0.0, "events"), id="footer-bar")

    def on_mount(self) -> None:
        self._start_time = time.time()
        table = self.query_one("#tasks", DataTable)
        # add_columns returns ColumnKeys we'll need to update cells later
        self._col_id, self._col_title, self._col_status = table.add_columns(
            "ID", "Task", "Status"
        )
        self._task_rows: dict[str, object] = {}

        if self.is_mock:
            for tid, title, status in TASKS_MOCK:
                rk = table.add_row(tid, title, self._status_cell(status))
                self._task_rows[tid] = rk
            self._update_task_count()
            self._log("[green]pm-agent TUI started (mock mode)[/]")
            self._log(f"[dim]goal:[/] {GOAL_MOCK}")
            self.set_interval(0.8, self._tick)
            return

        self._log("[green]pm-agent TUI started (real mode)[/]")
        self._log(f"[dim]target repo:[/] {_safe(self.repo)}")
        if self.interactive:
            self._log(
                "[cyan]interactive mode — type a goal in the input bar and press Enter[/]"
            )

        if self.interactive and not self.goal:
            # Waiting state: focus the input, don't run a session yet.
            self._session_complete = True  # so action_rerun / Enter aren't blocked
            self._update_task_count()
            try:
                self.query_one("#goal-input", Input).focus()
            except Exception:
                pass
            return

        self._log(f"[dim]goal:[/] {_safe(self.goal)}")

        # Note: tasks are populated AFTER planner runs (or right now if --single).
        if self.single:
            self._tasks = [
                CoderTask(
                    id="T-1",
                    title="single goal",
                    prompt=self.goal or "",
                    allowed_paths=["**"],
                    acceptance=["claude exits cleanly"],
                )
            ]
            for t in self._tasks:
                rk = table.add_row(t.id, t.title[:30], self._status_cell("ready"))
                self._task_rows[t.id] = rk

        self._update_task_count()
        self._run_session()

    # ---------- mock mode ----------
    def _tick(self) -> None:
        progress = self.query_one("#progress", ProgressBar)
        footer = self.query_one("#footer-bar", Static)

        self._log(MOCK_LOG_LINES[self._tick_counter % len(MOCK_LOG_LINES)])
        self._tick_counter += 1
        pct = min(100, self._tick_counter * 4)
        progress.update(progress=pct)

        # Fake task progression for mock visual: 3 tasks finishing across ticks.
        new_done = min(len(TASKS_MOCK), self._tick_counter // 8)
        if new_done != self._tasks_done:
            self._tasks_done = new_done
            table = self.query_one("#tasks", DataTable)
            for i, (tid, _title, _status) in enumerate(TASKS_MOCK):
                rk = self._task_rows.get(tid)
                if rk is None:
                    continue
                new_status = "done" if i < new_done else "running"
                table.update_cell(rk, self._col_status, self._status_cell(new_status))
        self._update_task_count()

        elapsed = time.time() - self._start_time
        cost = 0.012 * self._tick_counter
        tokens = 320 * self._tick_counter
        footer.update(self._footer_text(cost, tokens, elapsed, "tokens"))

    # ---------- real mode ----------
    @work(exclusive=True)
    async def _run_session(self) -> None:
        table = self.query_one("#tasks", DataTable)
        try:
            # Step 1: Planner (skipped in --single mode where tasks are pre-set)
            if not self.single:
                await self._run_planner(table)
                self._update_task_count()

            if not self._tasks:
                self._log("[red]no tasks to run; aborting session[/]")
                return

            # Idle any preallocated Coder slot we won't use this run. The TUI
            # statically wires Coder-1 and Coder-2 in AGENTS_INITIAL; if the
            # planner returns fewer than 2 tasks, mark the spare card idle.
            MAX_CODERS = 2
            for i in range(len(self._tasks) + 1, MAX_CODERS + 1):
                self._set_agent_status(f"Coder-{i}", "idle", "(unused this session)")

            # Step 2: parallel Coders
            results = await asyncio.gather(
                *[self._stream_one(t) for t in self._tasks],
                return_exceptions=True,
            )

            for t, r in zip(self._tasks, results):
                if isinstance(r, Exception):
                    self._log(f"[red][{t.id}] failed: {r}[/]")
                    self._update_task_status(t.id, "failed")
                    self._set_agent_status(
                        self._coder_card(t.id), "failed", f"{t.id} crashed"
                    )

            self._log(
                f"[bold green]✓ all coders finished[/] "
                f"total cost ${self._cost_usd:.4f} "
                f"in {time.time() - self._start_time:.1f}s"
            )

            # Step 3: integration (skip --single mode; nothing to merge)
            if not self.single and self.wm and self._tasks:
                await self._run_integration()

            summary_path = self._write_run_summary()
            self._log(f"[bold green]→ run summary:[/] {summary_path}")
        finally:
            # Cleanup task branches + integration branch even if cancelled.
            await self._cleanup_branches()
            self._session_complete = True

    async def _run_integration(self) -> None:
        assert self.wm is not None
        self._log(
            f"[bold cyan][Integrator][/] merging "
            f"{', '.join(t.id for t in self._tasks)} into "
            f"ai/integration/{self._run_id}"
        )
        try:
            self._integration = await self.wm.aintegrate(
                self._run_id, [t.id for t in self._tasks],
                test_cmd=self.test_cmd, test_timeout=self.test_timeout,
            )
        except Exception as e:
            self._log(f"[red][Integrator] crashed:[/] {type(e).__name__}: {_safe(e)}")
            return

        ig = self._integration
        if ig.conflicts:
            self._log(
                f"[red][Integrator] {len(ig.conflicts)} conflict(s):[/] "
                + ", ".join(c["task_id"] for c in ig.conflicts)
            )
            for c in ig.conflicts:
                self._log(f"[red]  {c['task_id']}: {c['output'].splitlines()[0][:100]}[/]")
        else:
            self._log(
                f"[green][Integrator] ✓ merged {len(ig.merged_tasks)} branch(es) clean[/]"
            )
            stats = self._diff_stats(ig.diff_against_base)
            self._log(
                f"[dim][Integrator] integrated diff: "
                f"+{stats['added']} -{stats['removed']} in {stats['files']} file(s)[/]"
            )

        if ig.test_result is not None:
            tr = ig.test_result
            ok = tr["exit_code"] == 0
            colour = "green" if ok else "red"
            self._log(
                f"[bold {colour}][Integrator] tests "
                f"{'PASS' if ok else 'FAIL'}[/] "
                f"({tr['command']!r} → exit {tr['exit_code']})"
            )

        # Save the integrated diff as an artifact
        self._artifacts_dir.mkdir(parents=True, exist_ok=True)
        (self._artifacts_dir / "integration.diff").write_text(ig.diff_against_base)

    async def _cleanup_branches(self) -> None:
        if self.wm is None:
            return
        # Best-effort delete of task branches. BUG-019: previously silent on
        # failure; now surface to the live log so accumulated junk branches
        # don't pile up unnoticed.
        for t in self._tasks:
            try:
                await self.wm.adelete_branch(t.id)
            except Exception as e:
                self._log(
                    f"[yellow]cleanup: could not delete branch ai/{_safe(t.id)}: "
                    f"{type(e).__name__}: {_safe(e)}[/]"
                )
        # Integration worktree + branch (if it was created).
        if self._integration is not None:
            try:
                await self.wm.acleanup_integration(self._integration)
            except Exception as e:
                self._log(
                    f"[yellow]cleanup: could not remove integration worktree "
                    f"{_safe(self._integration.branch)}: "
                    f"{type(e).__name__}: {_safe(e)}[/]"
                )

    async def _run_planner(self, table: DataTable) -> None:
        """Decompose self.goal into self._tasks. Falls back to mock on error."""
        used_mock = False
        if self.use_real_planner:
            self._set_agent_status(
                "Planner", "running", "calling claude with YAML system prompt"
            )
            self._log("[bold cyan][Planner][/] decomposing goal...")

            def _on_retry(attempt: int, error: str) -> None:
                self._log(
                    f"[yellow][Planner] retry {attempt}: previous output "
                    f"failed parse[/]"
                )
                self._log(f"[dim][Planner]   error: {error[:120]}[/]")
                self._set_agent_status(
                    "Planner", "running", f"retry {attempt} with error feedback"
                )

            # Day 11 fault injection: planner-yaml forces every attempt
            # (including the final one) to return malformed YAML. The retry
            # loop runs to exhaustion, raises PlannerError, and the existing
            # except branch falls back to mock_planner_decompose.
            sim_failures = 3 if self.inject_fault == "planner-yaml" else 0
            try:
                tasks, planner_cost = await plan(
                    self.goal or "",
                    self.repo,
                    max_retries=self.max_retries,
                    on_retry=_on_retry,
                    simulate_failures=sim_failures,
                )  # type: ignore[arg-type]
                self._tasks = tasks
                self._cost_usd += planner_cost
                self._log(
                    f"[green][Planner] ✓ {len(tasks)} tasks, "
                    f"cost ${planner_cost:.4f}[/]"
                )
            except PlannerError as e:
                self._artifacts_dir.mkdir(parents=True, exist_ok=True)
                (self._artifacts_dir / "planner-error.log").write_text(str(e))
                self._log(f"[red][Planner] failed:[/] {_safe(e)}")
                self._log("[yellow][Planner] falling back to mock decomposer[/]")
                self._tasks = mock_planner_decompose(self.goal or "")
                used_mock = True
            except Exception as e:
                import traceback
                tb = traceback.format_exc()
                # Persist the traceback so smoke tests / users can diagnose.
                self._artifacts_dir.mkdir(parents=True, exist_ok=True)
                (self._artifacts_dir / "planner-crash.log").write_text(
                    f"{type(e).__name__}: {e}\n\n{tb}"
                )
                self._log(f"[red][Planner] crashed:[/] {type(e).__name__}: {_safe(e)}")
                self._log(
                    f"[red][Planner] traceback saved to[/] "
                    f"{self._artifacts_dir / 'planner-crash.log'}"
                )
                self._log("[yellow][Planner] falling back to mock decomposer[/]")
                self._tasks = mock_planner_decompose(self.goal or "")
                used_mock = True
        else:
            self._log("[cyan][Planner mock][/] --mock-planner set, skipping real call")
            self._tasks = mock_planner_decompose(self.goal or "")
            used_mock = True

        # Update task table now that we have real tasks
        for t in self._tasks:
            rk = table.add_row(t.id, t.title[:30], self._status_cell("ready"))
            self._task_rows[t.id] = rk

        # Surface acceptance + paths in the log so user can verify the plan
        for t in self._tasks:
            self._log(
                f"[dim]  {t.id} paths:[/] {', '.join(t.allowed_paths) or '(none)'}"
            )
            for crit in t.acceptance[:3]:
                self._log(f"[dim]  {_safe(t.id)} accept:[/] {_safe(crit)}")

        label = "mock" if used_mock else "real claude"
        self._set_agent_status(
            "Planner", "done", f"{label}: decomposed into {len(self._tasks)}"
        )

    async def _stream_one(self, task: CoderTask) -> None:
        assert self.wm is not None
        progress = self.query_one("#progress", ProgressBar)
        footer = self.query_one("#footer-bar", Static)
        coder_card = self._coder_card(task.id)

        self._set_agent_status(coder_card, "running", f"{task.id}: creating worktree")
        try:
            wt_path = await self.wm.acreate(task.id)
        except Exception as e:
            self._log(f"[red][{task.id}] worktree failed: {e}[/]")
            self._set_agent_status(coder_card, "failed", f"{task.id} worktree error")
            raise

        try:
            self._log(f"[cyan][{task.id}] worktree {wt_path.name} ready[/]")
            self._set_agent_status(coder_card, "running", f"{task.id}: streaming")
            self._update_task_status(task.id, "running")

            # BUG-006: prepend the sandbox boundary (allowed paths +
            # acceptance) so the Coder LLM has explicit context for what it
            # may and must produce — not just the free-form prompt.
            allowed_block = "\n".join(f"    - {p}" for p in task.allowed_paths) or "    (any)"
            accept_block = "\n".join(f"    - {c}" for c in task.acceptance) or "    (none)"
            constraints = CODER_CONSTRAINTS_PREFIX.format(
                task_id=task.id,
                allowed_paths_block=allowed_block,
                acceptance_block=accept_block,
            )
            full_prompt = (
                constraints + task.prompt
                + CODER_COMMIT_SUFFIX.format(task_id=task.id)
            )
            assistant_buf = ""  # BUG-033 local batch buffer for stream

            # Day 11 fault injection: substitute a deterministic synthetic
            # event stream when the demo asks for timeout / api-error. The
            # downstream event handlers stay unchanged so the recovery UI
            # is exercised by the real codepath, just driven from a fake
            # source.
            if self.inject_fault == "coder-timeout":
                stream = self._fault_stream_timeout(task.id)
            elif self.inject_fault == "api-error":
                stream = self._fault_stream_api_error(task.id)
            else:
                stream = run_claude_async(
                    full_prompt,
                    cwd=str(wt_path),
                    unrestricted=True,
                    timeout=self.coder_timeout,
                )

            async for ev in stream:
                self._event_count += 1
                et, st = ev.get("type"), ev.get("subtype")
                if et == "system" and st == "init":
                    sid = (ev.get("session_id") or "")[:8]
                    self._log(f"[dim][{task.id}] session={sid}[/]")
                elif et == "assistant":
                    # BUG-033: batch assistant chunks instead of writing one
                    # RichLog line per token. We flush on newline OR after
                    # the local buffer crosses 200 chars; tail flush below.
                    for part in ev.get("message", {}).get("content", []):
                        if part.get("type") == "text":
                            text = part["text"]
                            self._streamed_text += text
                            assistant_buf += text
                            while "\n" in assistant_buf or len(assistant_buf) > 200:
                                if "\n" in assistant_buf:
                                    chunk, assistant_buf = assistant_buf.split("\n", 1)
                                else:
                                    chunk, assistant_buf = assistant_buf[:200], assistant_buf[200:]
                                self._log(
                                    f"[bold cyan][{_safe(task.id)}][/] {_safe(chunk)}"
                                )
                elif et == "result":
                    cost = ev.get("total_cost_usd") or 0.0
                    dur = ev.get("duration_ms") or 0
                    self._cost_usd += float(cost)
                    is_error = bool(ev.get("is_error"))
                    if is_error:
                        # API-side failure: auth, rate limit, network, etc.
                        # Don't mark done; surface the error reason.
                        reason = (
                            ev.get("result")
                            or ev.get("api_error_status")
                            or "unknown api error"
                        )
                        self._task_errors[task.id] = str(reason)[:300]
                        self._log(
                            f"[red][{_safe(task.id)}] ✗ API ERROR[/] "
                            f"reason={_safe(str(reason)[:120])}"
                        )
                        self._log(
                            f"[dim][{task.id}] cost=${float(cost):.4f} dur={dur}ms[/]"
                        )
                        self._update_task_status(task.id, "api_error")
                        self._set_agent_status(
                            coder_card, "failed", f"{task.id}: API error"
                        )
                        # BUG-018: progress advances on terminal API error
                        # so the bar tracks task completion (success OR fail).
                        self._tasks_done += 1
                    else:
                        self._log(
                            f"[green][{task.id}] ✓ result[/] "
                            f"cost=${float(cost):.4f} dur={dur}ms"
                        )
                        self._tasks_done += 1
                        self._update_task_status(task.id, "done")
                        self._set_agent_status(
                            coder_card, "done", f"{task.id}: complete"
                        )
                    progress.update(
                        progress=int(
                            100 * self._tasks_done / max(1, len(self._tasks))
                        )
                    )
                elif et == "rate_limit_event":
                    info = ev.get("rate_limit_info", {}) or {}
                    self._rate_limit_events.append(info)
                    status = info.get("status")
                    if status and status != "allowed":
                        self._log(
                            f"[yellow][{task.id}] ⚠ rate-limit {status} "
                            f"(reset @ {info.get('resetsAt')})[/]"
                        )
                elif et == "system" and ev.get("subtype") == "timeout":
                    elapsed_s = ev.get("elapsed_s")
                    self._log(
                        f"[red][{_safe(task.id)}] ✗ TIMEOUT after {elapsed_s}s — "
                        f"subprocess killed[/]"
                    )
                    self._update_task_status(task.id, "timeout")
                    self._set_agent_status(
                        coder_card, "failed", f"{task.id}: timeout"
                    )
                    # BUG-018: still advance progress so the bar reflects
                    # that this task entered a terminal state, even though
                    # it didn't succeed.
                    self._tasks_done += 1
                    progress.update(
                        progress=int(100 * self._tasks_done / max(1, len(self._tasks)))
                    )
                elif et == "system" and ev.get("subtype") == "spawn_error":
                    err = ev.get("error", "unknown spawn error")
                    self._log(
                        f"[red][{_safe(task.id)}] ✗ spawn error:[/] {_safe(err)}"
                    )
                    self._update_task_status(task.id, "failed")
                    self._set_agent_status(
                        coder_card, "failed", f"{task.id}: spawn error"
                    )
                    self._tasks_done += 1
                    progress.update(
                        progress=int(100 * self._tasks_done / max(1, len(self._tasks)))
                    )

                elapsed = time.time() - self._start_time
                footer.update(
                    self._footer_text(
                        self._cost_usd, self._event_count, elapsed, "events"
                    )
                )

            # Flush any trailing assistant batch (BUG-033)
            if assistant_buf.strip():
                self._log(f"[bold cyan][{_safe(task.id)}][/] {_safe(assistant_buf)}")
            # Capture diff BEFORE cleanup wipes the branch.
            diff = await asyncio.to_thread(self.wm.diff_against_base, task.id)
            self._task_diffs[task.id] = diff
            self._save_diff_artifact(task.id, diff)
            stats = self._diff_stats(diff)
            self._log(
                f"[bold magenta][{task.id}] diff:[/] "
                f"+{stats['added']} -{stats['removed']} in {stats['files']} file(s)"
            )
        finally:
            # Day 8: only the worktree dir is removed here. The branch lives
            # until end-of-session so the integration step can merge it.
            await self.wm.acleanup_worktree(task.id)
            self._log(f"[dim][{task.id}] worktree dir cleaned (branch kept)[/]")

    # ---------- artifacts ----------
    def _save_diff_artifact(self, task_id: str, diff: str) -> None:
        self._artifacts_dir.mkdir(parents=True, exist_ok=True)
        (self._artifacts_dir / f"{task_id}.diff").write_text(diff)

    @staticmethod
    def _diff_stats(diff: str) -> dict[str, int]:
        """Count added / removed lines and unique files from a unified diff.

        BUG-014 + BUG-045: the previous hand-rolled scanner treated ANY line
        starting with '---' / '+++' as a file header, miscounting renames,
        whitespace-prefixed paths, and content lines like markdown HRs.
        We now walk only the hunk regions, gated by a strict header pattern,
        and rely on git-style 'diff --git a/<x> b/<y>' (or '--- a/<x>' /
        '+++ b/<y>') for file detection.
        """
        added = 0
        removed = 0
        files: set[str] = set()
        in_hunk = False
        for line in diff.splitlines():
            if line.startswith("diff --git "):
                # diff --git a/<path-a> b/<path-b> — pick path-b (post-image)
                parts = line.split(" ", 3)
                if len(parts) >= 4:
                    b = parts[3]
                    if b.startswith("b/"):
                        files.add(b[2:])
                    else:
                        files.add(b)
                in_hunk = False
                continue
            if line.startswith("@@ "):
                in_hunk = True
                continue
            if not in_hunk:
                # Skip everything between diff headers and the first hunk
                # (index, --- a/x, +++ b/x, mode, similarity, etc.)
                continue
            # Inside a hunk every line is content: leading '+'/'-'/' '/'\\'.
            # '+++ ' and '--- ' here are content (e.g. a markdown HR or a
            # docstring banner literally starts with three dashes); they
            # would only be file markers OUTSIDE a hunk, but in_hunk gates
            # that path. So count + and - prefixes uniformly.
            if line.startswith("+"):
                added += 1
            elif line.startswith("-"):
                removed += 1
        return {"added": added, "removed": removed, "files": len(files)}

    def _write_run_summary(self) -> Path:
        self._artifacts_dir.mkdir(parents=True, exist_ok=True)
        out = [
            f"# pm-agent run {self._run_id}",
            "",
            f"- **goal**: {self.goal}",
            f"- **target repo**: {self.repo}",
            f"- **total cost**: ${self._cost_usd:.4f}",
            f"- **duration**: {time.time() - self._start_time:.1f}s",
            f"- **tasks completed**: {self._tasks_done}/{len(self._tasks)}",
        ]

        if self._integration is not None:
            ig = self._integration
            if ig.conflicts:
                out.append(
                    f"- **integration**: ❌ {len(ig.conflicts)} conflict(s) on "
                    + ", ".join(c["task_id"] for c in ig.conflicts)
                )
            elif ig.merged_tasks:
                out.append(
                    f"- **integration**: ✅ merged "
                    f"{', '.join(ig.merged_tasks)} into {ig.branch}"
                )
            if ig.test_result is not None:
                tr = ig.test_result
                ok = tr["exit_code"] == 0
                out.append(
                    f"- **integration tests**: "
                    f"{'✅ PASS' if ok else '❌ FAIL'} "
                    f"({tr['command']!r} → exit {tr['exit_code']})"
                )
        out.append("")

        if self._rate_limit_events:
            non_allowed = [
                e for e in self._rate_limit_events
                if (e.get("status") or "allowed") != "allowed"
            ]
            if non_allowed:
                out.append(
                    f"- **rate-limit hits**: {len(non_allowed)} non-allowed "
                    f"(of {len(self._rate_limit_events)} total)"
                )

        for t in self._tasks:
            diff = self._task_diffs.get(t.id, "")
            stats = self._diff_stats(diff) if diff else {"added": 0, "removed": 0, "files": 0}
            err = self._task_errors.get(t.id)
            status_str = "❌ API ERROR" if err else "✓ done"
            out += [
                f"## {t.id}: {t.title}  ({status_str})",
                "",
                f"- **branch**: ai/{t.id}",
                f"- **diff**: +{stats['added']} -{stats['removed']} lines, {stats['files']} file(s)",
                f"- **allowed_paths**: {', '.join(t.allowed_paths) or '(none)'}",
            ]
            if err:
                out.append(f"- **error**: {err}")
            out += [
                "",
                "**acceptance criteria:**",
                "",
            ]
            for c in t.acceptance:
                out.append(f"- {c}")
            out += ["", "<details><summary>diff</summary>", "", "```diff", diff[:8000], "```", "", "</details>", ""]

        if self._integration is not None:
            ig = self._integration
            out += [
                "## Integration",
                "",
                f"- **branch**: {ig.branch}",
                f"- **merged**: {', '.join(ig.merged_tasks) or '(none)'}",
            ]
            if ig.conflicts:
                out += [
                    "- **conflicts**:",
                ]
                for c in ig.conflicts:
                    out.append(f"  - **{c['task_id']}**: {c['output'].splitlines()[0][:160]}")
            if ig.test_result is not None:
                tr = ig.test_result
                out += [
                    "",
                    f"### Tests: {'PASS' if tr['exit_code'] == 0 else 'FAIL'}",
                    "",
                    f"- **command**: `{tr['command']}`",
                    f"- **exit_code**: {tr['exit_code']}",
                    "",
                    "<details><summary>stdout (tail)</summary>",
                    "",
                    "```",
                    tr["stdout"] or "(empty)",
                    "```",
                    "",
                    "</details>",
                    "",
                ]
                if tr["stderr"]:
                    out += [
                        "<details><summary>stderr (tail)</summary>",
                        "",
                        "```",
                        tr["stderr"],
                        "```",
                        "",
                        "</details>",
                        "",
                    ]
            out += [
                "",
                "<details><summary>integrated diff (vs base)</summary>",
                "",
                "```diff",
                ig.diff_against_base[:12000],
                "```",
                "",
                "</details>",
                "",
            ]

        path = self._artifacts_dir / "summary.md"
        path.write_text("\n".join(out))
        return path

    # ---------- fault injection (Day 11) ----------
    async def _fault_stream_timeout(self, task_id: str):
        """Synthetic event stream that mimics a Coder subprocess hitting the
        configured timeout. Runs free, deterministic — used when
        --inject-fault=coder-timeout is set."""
        yield {
            "type": "system",
            "subtype": "init",
            "session_id": f"fault-{task_id}",
        }
        # Brief pause so the "creating worktree → running → timeout" arc
        # is legible in the recording.
        await asyncio.sleep(0.4)
        yield {
            "type": "system",
            "subtype": "timeout",
            "elapsed_s": int(self.coder_timeout),
        }

    async def _fault_stream_api_error(self, task_id: str):
        """Synthetic event stream that yields a rate-limit-rejected event
        followed by a result(is_error=true). Reproduces the exact API
        failure path Day 8 added detection for, without spending tokens."""
        yield {
            "type": "system",
            "subtype": "init",
            "session_id": f"fault-{task_id}",
        }
        await asyncio.sleep(0.3)
        yield {
            "type": "rate_limit_event",
            "rate_limit_info": {
                "status": "rejected",
                "resetsAt": "2026-05-09T23:59:59Z",
            },
        }
        await asyncio.sleep(0.3)
        yield {
            "type": "result",
            "is_error": True,
            "result": "rate limit exceeded — synthetic fault injection",
            "total_cost_usd": 0.0,
            "duration_ms": 500,
        }

    # ---------- ui helpers ----------
    @staticmethod
    def _coder_card(task_id: str) -> str:
        # T-1 -> Coder-1, T-2 -> Coder-2 (matching ID convention)
        return task_id.replace("T-", "Coder-")

    def _log(self, msg: str) -> None:
        """RichLog.write with a left-aligned HH:MM:SS timestamp prefix."""
        try:
            log = self.query_one("#logs", RichLog)
        except Exception:
            return
        ts = time.strftime("%H:%M:%S")
        log.write(f"[dim]{ts}[/] {msg}")

    @staticmethod
    def _agent_card_text(name: str, status: str, action: str) -> str:
        icon = STATUS_ICONS.get(status, "•")
        return f"[bold]{icon} {name}[/]\n[dim]{action}[/]"

    @staticmethod
    def _status_cell(status: str) -> Text:
        """Return a colored Rich Text cell for the task table Status column."""
        colour = STATUS_COLORS.get(status, "white")
        icon = STATUS_ICONS.get(status, "")
        label = f"{icon} {status}".strip()
        return Text(label, style=colour)

    def _update_task_count(self) -> None:
        try:
            label = self.query_one("#task-count", Label)
        except Exception:
            return
        if self.is_mock:
            total = len(TASKS_MOCK)
        else:
            total = len(self._tasks)
        label.update(f"{self._tasks_done}/{total} tasks")

    def _update_task_status(self, task_id: str, status: str) -> None:
        rk = self._task_rows.get(task_id)
        if rk is None:
            return
        try:
            table = self.query_one("#tasks", DataTable)
            table.update_cell(rk, self._col_status, self._status_cell(status))
        except Exception:
            pass  # best-effort; agent cards are the primary visual cue

    def _set_agent_status(self, name: str, status: str, action: str) -> None:
        try:
            card = self.query_one(f"#agent-{name}", Static)
        except Exception:
            # BUG-009: surface the miss instead of swallowing silently.
            # Previously a 3rd Coder (N>2 tasks) lost its UI feedback with
            # zero indication. The Planner system prompt caps task count at
            # 2, so this only fires on misuse / future changes.
            self._log(
                f"[yellow]agent card missing: {_safe(name)} → "
                f"status={_safe(status)} action={_safe(action)}[/]"
            )
            return
        card.update(self._agent_card_text(name, status, action))
        for cls in (
            "status-running", "status-done", "status-idle",
            "status-blocked", "status-failed",
        ):
            card.remove_class(cls)
        card.add_class(f"status-{status}")

    def _footer_text(self, cost: float, count: int, elapsed: float, count_label: str) -> str:
        elapsed_str = str(timedelta(seconds=int(elapsed)))
        hint = "q to quit · r to re-run"
        if self.interactive:
            hint += " · n new goal · esc defocus"
        return (
            f"cost: [bold green]${cost:.4f}[/]   "
            f"{count_label}: [bold cyan]{count:,}[/]   "
            f"elapsed: [bold]{elapsed_str}[/]   "
            f"|   [dim]{hint}[/]"
        )

    # ---------- bindings ----------
    def _reset_for_new_run(self) -> None:
        """Reset per-run state. Shared by action_rerun and on_input_submitted."""
        self._tasks_done = 0
        self._event_count = 0
        self._cost_usd = 0.0
        self._streamed_text = ""
        self._task_diffs = {}
        self._task_errors = {}
        self._rate_limit_events = []
        self._integration = None
        self._session_complete = False
        self._start_time = time.time()
        self._run_id = (time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4])
        self._artifacts_dir = ARTIFACTS_ROOT / self._run_id

        try:
            self.query_one("#progress", ProgressBar).update(progress=0)
        except Exception:
            pass
        try:
            self.query_one("#tasks", DataTable).clear()
        except Exception:
            pass
        self._task_rows = {}
        for name, status, action in AGENTS_INITIAL:
            self._set_agent_status(name, status, action)

    def on_screen_resume(self) -> None:
        """Refresh disable state when switching back to goal mode.

        Task 10: wired against ``self.app.is_daemon_active`` once the real
        App lands in Task 12. We use ``getattr`` so the interim
        ``PMAgentTUI = GoalScreen`` alias (which has no daemon state) and
        plain ``Screen.run_test()`` harnesses don't blow up.
        """
        from textual.css.query import NoMatches
        try:
            input_w = self.query_one("#goal-input", Input)
        except NoMatches:
            return  # not yet mounted / wrong screen
        is_active = getattr(self.app, "is_daemon_active", False)
        if is_active:
            input_w.disabled = True
            input_w.placeholder = "(locked — daemon running)"
        else:
            input_w.disabled = False
            input_w.placeholder = "type a goal and press Enter"

    def action_rerun(self) -> None:
        """Re-run the session. Mock mode resets the ticker; real mode reruns
        the planner + coders + integration. Refuses if a real-mode session is
        still in flight (press q to abort first)."""
        # Task 10: daemon mode (added in Task 11/12) takes exclusive control
        # of subprocess scheduling, so disable goal-mode re-runs while it is
        # active. ``getattr`` keeps this safe before the real App lands.
        if getattr(self.app, "is_daemon_active", False):
            self.notify("daemon running; goal mode locked", severity="warning")
            return
        if self.is_mock:
            self._tick_counter = 0
            self._tasks_done = 0
            self._event_count = 0
            self._cost_usd = 0.0
            self._start_time = time.time()
            try:
                progress = self.query_one("#progress", ProgressBar)
                progress.update(progress=0)
            except Exception:
                pass
            try:
                table = self.query_one("#tasks", DataTable)
                for i, (_tid, _title, status) in enumerate(TASKS_MOCK):
                    rk = table.coordinate_to_cell_key((i, 0)).row_key
                    table.update_cell(rk, self._col_status, self._status_cell(status))
            except Exception:
                pass
            self._update_task_count()
            self._log("[yellow]↻ mock re-run[/]")
            return

        if not self._session_complete:
            self._log("[yellow]↻ session still running; press q to abort first[/]")
            return

        if not self.goal:
            self._log("[yellow]no goal set; type one in the input bar and press Enter[/]")
            return

        self._log("[yellow]↻ re-running session...[/]")
        self._reset_for_new_run()

        table = self.query_one("#tasks", DataTable)
        if self.single:
            for t in self._tasks:
                rk = table.add_row(t.id, t.title[:30], self._status_cell("ready"))
                self._task_rows[t.id] = rk
        else:
            self._tasks = []

        self._update_task_count()
        # @work(exclusive=True) will cancel any lingering worker before starting.
        self._run_session()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Day 15: user typed a goal into the input bar and pressed Enter."""
        if event.input.id != "goal-input":
            return
        value = event.value.strip()
        if not value:
            return
        if not self._session_complete:
            self._log("[yellow]session still running; press q to abort first[/]")
            return
        self.goal = value
        event.input.value = ""
        try:
            self.query_one("#goal", Label).update(self._goal_text())
        except Exception:
            pass
        self._log(f"[cyan]new goal:[/] {_safe(value)}")

        self._reset_for_new_run()
        # Interactive mode always uses real Planner — no --single path here.
        self._tasks = []
        self._update_task_count()
        self.set_focus(None)  # defocus input so q/r work without escape first
        self._run_session()

    def action_focus_input(self) -> None:
        if not self.interactive:
            return
        try:
            self.query_one("#goal-input", Input).focus()
        except Exception:
            pass

    def action_blur_input(self) -> None:
        if not self.interactive:
            return
        self.set_focus(None)


# Names must match preflight.py CheckResult.name strings verbatim.
# Verified 2026-05-18 against preflight.py:40,72,119,160,195,241,276.
HARD_CHECK_NAMES = {
    "state.db dir writable",
    "claude CLI on PATH",
    "gh CLI authenticated",
    "repo clean",
    "/tmp writable",
}
# Excluded deliberately:
#   "state.db clean" — hard-fails on any prior cycle; we demote to soft-warn.
#   "repo on main"   — already warn_only=True in preflight.


class DaemonScreen(Screen):
    BINDINGS = [
        ("s",      "start_daemon",  "Start"),
        ("x",      "stop_daemon",   "Stop"),
        ("p",      "preflight",     "Re-preflight"),
        ("R",      "focus_repo",    "Set repo"),
        ("escape", "blur_input",    ""),
    ]

    DEFAULT_CSS = """
    DaemonScreen { layout: vertical; }
    #cycle-row { height: 1; }
    #main { height: 1fr; }
    #left {
        width: 50%;
        height: 100%;
        border: solid $secondary;
    }
    #daemon-log {
        width: 1fr;
        height: 100%;
        border: solid $secondary;
    }
    #repo-input {
        height: 3;
        background: $surface;
        border: solid $accent;
    }
    """

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

    def on_mount(self) -> None:
        from textual.css.query import NoMatches
        try:
            ftable = self.query_one("#findings", DataTable)
            ftable.add_columns("bug_id", "severity", "status", "title")
        except NoMatches:
            pass
        try:
            ptable = self.query_one("#prs", DataTable)
            ptable.add_columns("#", "state", "bug_id", "title")
        except NoMatches:
            pass
        # Kick off preflight in the background; do not block on_mount
        asyncio.create_task(self._run_preflight_async())

    async def _run_preflight_async(self) -> None:
        from pm_agent import preflight as _pf
        from pm_agent.preflight import CheckResult
        from pm_agent.loop import STATE_DB
        try:
            results, _ = await asyncio.to_thread(
                _pf.run_preflight, STATE_DB, self.app.repo)
        except Exception as e:
            _logging.getLogger(__name__).exception("preflight crashed")
            results = [CheckResult("preflight runner", False,
                                   f"crashed: {e!r}")]
        self.app.preflight_results = results
        from textual.css.query import NoMatches
        try:
            self.query_one(PreflightBar).update_from(results)
        except NoMatches:
            pass

    async def on_screen_resume(self) -> None:
        from textual.css.query import NoMatches
        # Replay buffered log lines
        try:
            log_w = self.query_one("#daemon-log", RichLog)
            log_w.clear()
            for line in list(self.app.log_buffer):
                log_w.write(line)
        except NoMatches:
            pass
        # Immediate refresh from DB
        try:
            await self.app.db_poller.tick_once()
        except (AttributeError, asyncio.TimeoutError):
            pass

    def append_log_line(self, msg: str) -> None:
        from textual.css.query import NoMatches
        try:
            self.query_one("#daemon-log", RichLog).write(msg)
        except NoMatches:
            pass

    def update_cycle_and_findings(self, payload) -> None:
        from textual.css.query import NoMatches
        try:
            self.query_one(CycleSummaryRow).update_from(payload)
        except NoMatches:
            pass
        try:
            ftable = self.query_one("#findings", DataTable)
            ftable.clear()
            for f in payload.findings:
                ftable.add_row(f.bug_id, f.severity, f.status, f.title)
        except NoMatches:
            pass

    def update_prs(self, rows) -> None:
        from textual.css.query import NoMatches
        try:
            ptable = self.query_one("#prs", DataTable)
            ptable.clear()
            for r in rows:
                ptable.add_row(
                    f"#{r.github_number}", r.state, r.bug_id, r.title)
        except NoMatches:
            pass

    # ── actions ────────────────────────────────────────────────────────

    async def action_start_daemon(self) -> None:
        from pm_agent import loop as _loop, persistence
        log = _logging.getLogger("pm_agent.tui")
        if self.app.daemon_task and not self.app.daemon_task.done():
            self.app.notify("daemon already running"); return
        if getattr(self.app, "daemon_starting", False):
            self.app.notify("daemon already starting"); return
        if self.app.preflight_results is None:
            self.app.notify("preflight not yet run; press p first"); return

        hard_fails = [
            r for r in self.app.preflight_results
            if not r.ok and not r.warn_only and r.name in HARD_CHECK_NAMES
        ]
        if hard_fails:
            names = ", ".join(r.name for r in hard_fails)
            self.app.notify(f"preflight failed: {names}", severity="error")
            return

        for r in self.app.preflight_results:
            if (not r.ok and not r.warn_only
                    and r.name not in HARD_CHECK_NAMES):
                log.info("preflight soft-warn: %s — %s", r.name, r.message)

        self.app.daemon_starting = True
        try:
            self.app.daemon_stop_event = asyncio.Event()
            rec = await asyncio.to_thread(persistence.reconcile, self.app.repo)
            log.info("reconcile: %s", rec)

            self.app.daemon_last_error = None
            from textual.css.query import NoMatches
            try:
                self.query_one(CycleSummaryRow).clear_last_error()
            except NoMatches:
                pass

            self.app.daemon_task = asyncio.create_task(
                _loop.run_forever(
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

    async def action_stop_daemon(self) -> None:
        if getattr(self.app, "daemon_starting", False):
            self.app.notify("daemon still starting; try again in a moment")
            return
        t = self.app.daemon_task
        if not t or t.done():
            self.app.notify("no running daemon"); return
        self.app.daemon_stop_event.set()
        self.app.notify(
            "stop signal sent; daemon will finish current cycle")

    async def action_preflight(self) -> None:
        await self._run_preflight_async()

    def action_focus_repo(self) -> None:
        if getattr(self.app, "is_daemon_active", False):
            self.app.notify("stop daemon first to change repo")
            return
        from textual.css.query import NoMatches
        try:
            self.query_one("#repo-input", Input).focus()
        except NoMatches:
            pass

    def action_blur_input(self) -> None:
        from textual.css.query import NoMatches
        try:
            self.query_one("#repo-input", Input).blur()
        except NoMatches:
            pass

    async def on_input_submitted(self, event):
        if event.input.id != "repo-input":
            return
        new = Path(event.value).expanduser()
        if not new.is_dir():
            self.app.notify(f"not a directory: {new}", severity="error")
            return
        if not (new / ".git").is_dir():
            self.app.notify(f"not a git repo: {new}", severity="error")
            return
        self.app.repo = new
        _logging.getLogger("pm_agent.tui").info("repo switched to %s", new)
        event.input.blur()
        await self.action_preflight()


def _ensure_target_repo(path: Path) -> Path:
    """Create + init path as a git repo if missing. Used as the default scratch
    target so multi-coder demo runs work out of the box."""
    path = path.expanduser().resolve()
    path.mkdir(parents=True, exist_ok=True)
    if not (path / ".git").exists():
        subprocess.run(["git", "init", "-q", "-b", "master"], cwd=str(path), check=True)
        # Need at least one commit so worktree branch creation works.
        (path / ".gitkeep").write_text("")
        subprocess.run(["git", "add", ".gitkeep"], cwd=str(path), check=True)
        env = {**os.environ, "GIT_AUTHOR_NAME": "pm-agent", "GIT_AUTHOR_EMAIL": "pm@local",
               "GIT_COMMITTER_NAME": "pm-agent", "GIT_COMMITTER_EMAIL": "pm@local"}
        subprocess.run(
            ["git", "commit", "-q", "-m", "init"],
            cwd=str(path), check=True, env=env,
        )
    return path


def main() -> None:
    ap = argparse.ArgumentParser(prog="pm-agent.tui")
    ap.add_argument("goal", nargs="*", help="goal text (omit to enter mock mode)")
    ap.add_argument(
        "--repo",
        type=Path,
        default=Path("/tmp/pm-agent-day7-target"),
        help="target git repo for worktrees (default: /tmp/pm-agent-day7-target, aligned with docs/demo-commands.sh)",
    )
    ap.add_argument(
        "--single",
        action="store_true",
        help="single-coder mode (day 3-4 behavior); ignores planner decomposition",
    )
    ap.add_argument(
        "--mock-planner",
        action="store_true",
        help="skip the real claude-driven planner, use the cheap mock fallback",
    )
    ap.add_argument(
        "--test-cmd",
        default=None,
        help="shell command to run inside the integration worktree after a clean merge "
             "(e.g. 'pytest tests/' or 'python3 -m unittest discover')",
    )
    ap.add_argument(
        "--coder-timeout",
        type=float,
        default=180.0,
        help="seconds before each Coder subprocess is killed (default: 180)",
    )
    ap.add_argument(
        "--max-retries",
        type=int,
        default=2,
        help="planner self-correcting retry budget (default: 2 = up to 3 attempts)",
    )
    ap.add_argument(
        "--test-timeout",
        type=float,
        default=120.0,
        help="seconds before integration test_cmd is killed (default: 120)",
    )
    ap.add_argument(
        "--inject-fault",
        choices=("planner-yaml", "coder-timeout", "api-error"),
        default=None,
        help="Day 11 demo: deterministically trigger an error path. "
             "planner-yaml: every planner attempt fails parse, falling back "
             "to mock_planner_decompose. "
             "coder-timeout: every Coder yields a synthetic timeout event "
             "(real timeout handler runs). "
             "api-error: every Coder yields a synthetic rate-limit + "
             "is_error result. "
             "All three are free / deterministic — no extra claude tokens.",
    )
    ap.add_argument(
        "--interactive", "-i",
        action="store_true",
        help="Day 15: show an input bar at the bottom; type goals and press "
             "Enter to run them back-to-back. Initial goal arg is optional in "
             "this mode — omit it to start at the input prompt.",
    )
    args = ap.parse_args()

    goal = " ".join(args.goal).strip() or None

    # Mock mode: no goal AND not interactive.
    if goal is None and not args.interactive:
        _run_goal_only(goal=None)
        return

    repo = _ensure_target_repo(args.repo)
    _run_goal_only(
        goal=goal,
        repo=repo,
        single=args.single,
        use_real_planner=not args.mock_planner,
        test_cmd=args.test_cmd,
        coder_timeout=args.coder_timeout,
        inject_fault=args.inject_fault,
        interactive=args.interactive,
        max_retries=args.max_retries,
        test_timeout=args.test_timeout,
    )


def _run_goal_only(**screen_kwargs) -> None:
    """Interim runner for Task 10-11: wraps ``GoalScreen`` in a bare App.

    ``GoalScreen`` is a ``Screen``, not an ``App``, so it has no ``run()``
    of its own. Task 12 replaces this with the full ``PMAgentTUI(App)``
    that hosts both Goal and Daemon screens; until then, ``main()`` calls
    this helper to keep ``python -m pm_agent.tui`` working.
    """
    from textual.app import App as _App

    class _Wrapper(_App):
        def on_mount(self) -> None:  # pragma: no cover - exercised manually
            self.push_screen(GoalScreen(**screen_kwargs))

    _Wrapper().run()


class PMAgentTUI(App):
    """Top-level Textual App. Wraps GoalScreen and DaemonScreen and holds
    daemon-task / log-buffer / db-poller shared state."""

    BINDINGS = [
        ("g", "switch_screen('goal')",   "Goal mode"),
        ("d", "switch_screen('daemon')", "Daemon mode"),
        ("q", "request_quit",            "Quit"),
    ]

    DEFAULT_CSS = """
    .panel-title {
        text-style: bold;
        background: $boost;
        height: 1;
        margin: 0 0 1 0;
    }
    """

    def __init__(
        self,
        repo=None,
        goal=None,
        *,
        open_daemon: bool = False,
        loop_cfg=None,
        **goal_kwargs,
    ):
        super().__init__()
        self.repo = Path(repo) if repo is not None else Path("/tmp/pm-agent-day7-target")
        self.goal = goal
        self._open_daemon = open_daemon
        self._goal_kwargs = goal_kwargs
        # Initialize DB exactly once
        from pm_agent import persistence
        from pm_agent.loop import STATE_DB, LoopConfig
        persistence.init_db(STATE_DB)
        self.log_buffer = collections.deque(maxlen=2000)
        self.daemon_task = None
        self.daemon_starting = False
        self.daemon_stop_event = None
        self.daemon_last_error = None
        self.preflight_results = None
        self.loop_cfg = loop_cfg or LoopConfig()
        self.log_handler = None
        self.db_poller = None

    @property
    def is_daemon_active(self) -> bool:
        return self.daemon_starting or (
            self.daemon_task is not None and not self.daemon_task.done()
        )

    def on_mount(self) -> None:
        loop = asyncio.get_running_loop()
        root = _logging.getLogger()
        # Remove any stale TUILogHandler from prior App instances (pytest)
        for h in list(root.handlers):
            if isinstance(h, TUILogHandler):
                root.removeHandler(h)
        self.log_handler = TUILogHandler(loop, self.log_buffer, self)
        self.log_handler.setLevel(_logging.INFO)
        self.log_handler.setFormatter(_logging.Formatter(
            "%(asctime)s [%(name)s] %(levelname)s %(message)s",
            datefmt="%H:%M:%S",
        ))
        root.addHandler(self.log_handler)
        _logging.getLogger("textual").setLevel(_logging.WARNING)
        _logging.getLogger("uvicorn").setLevel(_logging.WARNING)
        _logging.getLogger("pm_agent").setLevel(_logging.INFO)

        # Install screens (passing constructor args)
        goal_screen = GoalScreen(
            repo=self.repo, goal=self.goal, **self._goal_kwargs)
        self.install_screen(goal_screen, name="goal")
        self.install_screen(DaemonScreen(), name="daemon")

        self.db_poller = DbPoller(self)
        self.db_poller.start()

        # Route SIGTERM through Textual exit
        try:
            loop.add_signal_handler(signal.SIGTERM, self.exit)
        except (NotImplementedError, ValueError):
            pass  # Windows / not main thread

        self.push_screen("daemon" if self._open_daemon else "goal")

    def on_unmount(self) -> None:
        if self.log_handler is not None:
            _logging.getLogger().removeHandler(self.log_handler)
        if self.db_poller is not None:
            self.db_poller.stop()

    def _on_daemon_done(self, task) -> None:
        try:
            task.result()
            self.daemon_last_error = None
        except asyncio.CancelledError as e:
            self.daemon_last_error = e
        except Exception as e:
            self.daemon_last_error = e
        # SystemExit / KeyboardInterrupt are intentionally not caught here —
        # they propagate to the event loop.
        self.daemon_task = None
        _logging.getLogger("pm_agent.tui").info(
            "daemon stopped: %s", self.daemon_last_error or "clean")
        # Tell CycleSummaryRow about the error
        from textual.css.query import NoMatches
        try:
            for s in self.screen_stack:
                if isinstance(s, DaemonScreen):
                    row = s.query_one(CycleSummaryRow)
                    if self.daemon_last_error is not None:
                        row.set_last_error(self.daemon_last_error)
                    else:
                        row.clear_last_error()
        except NoMatches:
            pass

    async def action_request_quit(self) -> None:
        if self.daemon_task is not None and not self.daemon_task.done():
            if self.daemon_stop_event is not None:
                self.daemon_stop_event.set()
            self.notify("draining daemon... (30s max)")
            try:
                await asyncio.wait_for(self.daemon_task, timeout=30)
            except asyncio.TimeoutError:
                self.daemon_task.cancel()
                self.notify(
                    "forced exit; worktree cleanup may be incomplete",
                    severity="warning",
                )
                try:
                    await self.daemon_task
                except (asyncio.CancelledError, Exception):
                    pass
        self.exit()


if __name__ == "__main__":
    main()
