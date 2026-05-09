"""TUI for pm-agent.

Three modes:

  1. Mock mode (no goal argv): day-2 ticker over fake data. Useful for
     layout tweaks without burning API.
       uv run python -m pm_agent.tui

  2. Single-coder real mode (--single): day-3/4 wiring. One claude -p
     subprocess streams into the right panel.
       uv run python -m pm_agent.tui --single "what is 2+2"

  3. Multi-coder real mode (default for non-mock): day-5 wiring.
     A mock planner decomposes the goal into 2 fixed subtasks; two
     Coders run in parallel, each in its own git worktree. Streams
     interleave in the live log, prefixed by task id.
       uv run python -m pm_agent.tui "anything you want" --repo /tmp/scratch

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
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Label, ProgressBar, RichLog, Static

from pm_agent.runner import run_claude_async
from pm_agent.worktree import WorktreeManager

GOAL_MOCK = "Add room invite link API to werewolf platform (mock)"

AGENTS_INITIAL = [
    ("Planner",  "done",     "Decomposed into 3 tasks"),
    ("Coder-1",  "running",  "Editing server/rooms/api.py"),
    ("Coder-2",  "running",  "Writing tests for invite expiry"),
    ("Reviewer", "idle",     "Waiting for coders"),
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


@dataclass
class CoderTask:
    id: str
    title: str
    prompt: str


def mock_planner_decompose(goal: str) -> list[CoderTask]:
    """Day-5 placeholder for a real Planner agent.

    Returns 2 short, parallelizable subtasks. Day-6 replaces this with a
    real claude-driven planner that reads the goal + repo and emits a
    YAML task DAG.
    """
    return [
        CoderTask(
            id="T-1",
            title="Explain asyncio.gather",
            prompt="In 5 words, what does asyncio.gather do?",
        ),
        CoderTask(
            id="T-2",
            title="Explain git worktree",
            prompt="In 5 words, what does git worktree do?",
        ),
    ]


class PMAgentTUI(App):
    """Day-5 — multi-coder via git worktree + asyncio.gather."""

    CSS = """
    Screen { layout: vertical; }

    #goal-bar {
        height: 4;
        background: $primary 30%;
        padding: 0 1;
        border: solid $primary;
    }
    #goal-bar Label { text-style: bold; }

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
    """

    BINDINGS = [("q", "quit", "Quit")]

    _start_time: float = 0.0
    _tick_counter: int = 0
    _event_count: int = 0
    _cost_usd: float = 0.0
    _tasks_done: int = 0
    _streamed_text: str = ""

    def __init__(
        self,
        goal: str | None = None,
        repo: Path | None = None,
        single: bool = False,
    ) -> None:
        super().__init__()
        self.goal = goal
        self.is_mock = goal is None
        self.single = single
        self.repo = repo
        self.wm: WorktreeManager | None = None
        if not self.is_mock and repo is not None:
            self.wm = WorktreeManager(repo)
        self._tasks: list[CoderTask] = []

    def compose(self) -> ComposeResult:
        display_goal = self.goal if self.goal else GOAL_MOCK
        with Vertical(id="goal-bar"):
            yield Label(f"Goal: {display_goal}", id="goal")
            yield ProgressBar(total=100, show_eta=False, id="progress")
        with Horizontal(id="main"):
            with Vertical(id="left"):
                yield Static("Tasks", classes="panel-title")
                yield DataTable(id="tasks", show_header=True, zebra_stripes=True)
            with Vertical(id="center"):
                yield Static("Active Agents", classes="panel-title")
                for name, status, action in AGENTS_INITIAL:
                    yield Static(
                        f"[bold]{name}[/]\n[dim]{action}[/]",
                        id=f"agent-{name}",
                        classes=f"agent-card status-{status}",
                    )
            with Vertical(id="right"):
                yield Static("Live Log", classes="panel-title")
                yield RichLog(id="logs", wrap=True, highlight=True, markup=True)
        yield Static(self._footer_text(0.0, 0, 0.0, "events"), id="footer-bar")

    def on_mount(self) -> None:
        self._start_time = time.time()
        table = self.query_one("#tasks", DataTable)
        # add_columns returns ColumnKeys we'll need to update cells later
        self._col_id, self._col_title, self._col_status = table.add_columns(
            "ID", "Task", "Status"
        )
        self._task_rows: dict[str, object] = {}
        log = self.query_one("#logs", RichLog)

        if self.is_mock:
            for tid, title, status in TASKS_MOCK:
                table.add_row(tid, title, status)
            log.write("[green]pm-agent TUI started (mock mode)[/]")
            log.write(f"[dim]goal:[/] {GOAL_MOCK}")
            self.set_interval(0.8, self._tick)
            return

        log.write("[green]pm-agent TUI started (real mode)[/]")
        log.write(f"[dim]goal:[/] {self.goal}")
        log.write(f"[dim]target repo:[/] {self.repo}")

        if self.single:
            self._tasks = [
                CoderTask(id="T-1", title="single goal", prompt=self.goal or "")
            ]
        else:
            self._tasks = mock_planner_decompose(self.goal or "")
            log.write(
                f"[cyan][Planner mock][/] decomposed into "
                f"{len(self._tasks)} parallel subtasks"
            )

        for t in self._tasks:
            rk = table.add_row(t.id, t.title[:30], "ready")
            self._task_rows[t.id] = rk

        self._run_session()

    # ---------- mock mode ----------
    def _tick(self) -> None:
        log = self.query_one("#logs", RichLog)
        progress = self.query_one("#progress", ProgressBar)
        footer = self.query_one("#footer-bar", Static)

        log.write(MOCK_LOG_LINES[self._tick_counter % len(MOCK_LOG_LINES)])
        self._tick_counter += 1
        progress.update(progress=min(100, self._tick_counter * 4))
        elapsed = time.time() - self._start_time
        cost = 0.012 * self._tick_counter
        tokens = 320 * self._tick_counter
        footer.update(self._footer_text(cost, tokens, elapsed, "tokens"))

    # ---------- real mode ----------
    @work(exclusive=True)
    async def _run_session(self) -> None:
        log = self.query_one("#logs", RichLog)
        # Mark non-Coder cards as "out of scope this session"
        self._set_agent_status("Planner",  "done", "Decomposition complete (mock)")
        self._set_agent_status("Reviewer", "idle", "(needs result — day 7+)")
        # Idle any coder card we won't use
        for i in range(len(self._tasks) + 1, 3):
            self._set_agent_status(f"Coder-{i}", "idle", "(unused this session)")

        results = await asyncio.gather(
            *[self._stream_one(t) for t in self._tasks],
            return_exceptions=True,
        )

        for t, r in zip(self._tasks, results):
            if isinstance(r, Exception):
                log.write(f"[red][{t.id}] failed: {r}[/]")
                self._update_task_status(t.id, "failed")
                self._set_agent_status(
                    self._coder_card(t.id), "failed", f"{t.id} crashed"
                )

        log.write(
            f"[bold green]✓ all coders finished[/] "
            f"total cost ${self._cost_usd:.4f} "
            f"in {time.time() - self._start_time:.1f}s"
        )

    async def _stream_one(self, task: CoderTask) -> None:
        assert self.wm is not None
        log = self.query_one("#logs", RichLog)
        progress = self.query_one("#progress", ProgressBar)
        footer = self.query_one("#footer-bar", Static)
        coder_card = self._coder_card(task.id)

        self._set_agent_status(coder_card, "running", f"{task.id}: creating worktree")
        try:
            wt_path = await self.wm.acreate(task.id)
        except Exception as e:
            log.write(f"[red][{task.id}] worktree failed: {e}[/]")
            self._set_agent_status(coder_card, "failed", f"{task.id} worktree error")
            raise

        try:
            log.write(f"[cyan][{task.id}] worktree {wt_path.name} ready[/]")
            self._set_agent_status(coder_card, "running", f"{task.id}: streaming")
            self._update_task_status(task.id, "running")

            async for ev in run_claude_async(task.prompt, cwd=str(wt_path)):
                self._event_count += 1
                et, st = ev.get("type"), ev.get("subtype")
                if et == "system" and st == "init":
                    sid = (ev.get("session_id") or "")[:8]
                    log.write(f"[dim][{task.id}] session={sid}[/]")
                elif et == "assistant":
                    for part in ev.get("message", {}).get("content", []):
                        if part.get("type") == "text":
                            text = part["text"]
                            self._streamed_text += text
                            log.write(f"[bold cyan][{task.id}][/] {text}")
                elif et == "result":
                    cost = ev.get("total_cost_usd") or 0.0
                    dur = ev.get("duration_ms") or 0
                    self._cost_usd += float(cost)
                    log.write(
                        f"[green][{task.id}] ✓ result[/] "
                        f"cost=${float(cost):.4f} dur={dur}ms"
                    )
                    self._tasks_done += 1
                    self._update_task_status(task.id, "done")
                    self._set_agent_status(coder_card, "done", f"{task.id}: complete")
                    progress.update(
                        progress=int(100 * self._tasks_done / max(1, len(self._tasks)))
                    )

                elapsed = time.time() - self._start_time
                footer.update(
                    self._footer_text(
                        self._cost_usd, self._event_count, elapsed, "events"
                    )
                )
        finally:
            await self.wm.acleanup(task.id)
            log.write(f"[dim][{task.id}] worktree cleaned[/]")

    # ---------- helpers ----------
    @staticmethod
    def _coder_card(task_id: str) -> str:
        # T-1 -> Coder-1, T-2 -> Coder-2 (matching ID convention)
        return task_id.replace("T-", "Coder-")

    def _update_task_status(self, task_id: str, status: str) -> None:
        rk = self._task_rows.get(task_id)
        if rk is None:
            return
        try:
            table = self.query_one("#tasks", DataTable)
            table.update_cell(rk, self._col_status, status)
        except Exception:
            pass  # best-effort; agent cards are the primary visual cue

    def _set_agent_status(self, name: str, status: str, action: str) -> None:
        try:
            card = self.query_one(f"#agent-{name}", Static)
        except Exception:
            return
        card.update(f"[bold]{name}[/]\n[dim]{action}[/]")
        for cls in (
            "status-running", "status-done", "status-idle",
            "status-blocked", "status-failed",
        ):
            card.remove_class(cls)
        card.add_class(f"status-{status}")

    @staticmethod
    def _footer_text(cost: float, count: int, elapsed: float, count_label: str) -> str:
        elapsed_str = str(timedelta(seconds=int(elapsed)))
        return (
            f"cost: [bold green]${cost:.4f}[/]   "
            f"{count_label}: [bold cyan]{count:,}[/]   "
            f"elapsed: [bold]{elapsed_str}[/]   "
            f"|   [dim]q to quit[/]"
        )


def _ensure_target_repo(path: Path) -> Path:
    """Create + init path as a git repo if missing. Used as the default scratch
    target so multi-coder demo runs work out of the box."""
    path = path.expanduser().resolve()
    path.mkdir(parents=True, exist_ok=True)
    if not (path / ".git").exists():
        subprocess.run(["git", "init", "-q"], cwd=str(path), check=True)
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
        default=Path("/tmp/pm-agent-target"),
        help="target git repo for worktrees (default: /tmp/pm-agent-target)",
    )
    ap.add_argument(
        "--single",
        action="store_true",
        help="single-coder mode (day 3-4 behavior); ignores planner decomposition",
    )
    args = ap.parse_args()

    goal = " ".join(args.goal).strip() or None
    if goal is None:
        PMAgentTUI(goal=None).run()
        return

    repo = _ensure_target_repo(args.repo)
    PMAgentTUI(goal=goal, repo=repo, single=args.single).run()


if __name__ == "__main__":
    main()
