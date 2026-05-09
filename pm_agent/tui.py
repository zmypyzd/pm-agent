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
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Label, ProgressBar, RichLog, Static

from pm_agent.planner import PlannerError, plan
from pm_agent.runner import run_claude_async
from pm_agent.tasks import CoderTask
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


CODER_COMMIT_SUFFIX = """

When you finish making your code changes:
1. Stage your changes:  git add -A
2. Commit them:          git commit -m "{task_id}: <one-line summary>"
3. Print exactly: DONE

If you made no file changes, instead print: NO_CHANGES
Stay strictly inside this worktree directory. Do not push, do not switch branches.
"""


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
    _session_complete: bool = False

    def __init__(
        self,
        goal: str | None = None,
        repo: Path | None = None,
        single: bool = False,
        use_real_planner: bool = True,
    ) -> None:
        super().__init__()
        self.goal = goal
        self.is_mock = goal is None
        self.single = single
        self.use_real_planner = use_real_planner
        self.repo = repo
        self.wm: WorktreeManager | None = None
        if not self.is_mock and repo is not None:
            self.wm = WorktreeManager(repo)
        self._tasks: list[CoderTask] = []
        self._run_id: str = time.strftime("%Y%m%d-%H%M%S")
        self._artifacts_dir: Path = ARTIFACTS_ROOT / self._run_id
        self._task_diffs: dict[str, str] = {}

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
        table = self.query_one("#tasks", DataTable)

        # Step 1: Planner (skipped in --single mode where tasks are pre-set)
        if not self.single:
            await self._run_planner(log, table)

        if not self._tasks:
            log.write("[red]no tasks to run; aborting session[/]")
            return

        # Idle any coder card we won't use this run
        for i in range(len(self._tasks) + 1, 3):
            self._set_agent_status(f"Coder-{i}", "idle", "(unused this session)")
        self._set_agent_status("Reviewer", "idle", "(needs result — day 7+)")

        # Step 2: parallel Coders
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

        summary_path = self._write_run_summary()
        log.write(f"[bold green]→ run summary:[/] {summary_path}")
        self._session_complete = True

    async def _run_planner(self, log: RichLog, table: DataTable) -> None:
        """Decompose self.goal into self._tasks. Falls back to mock on error."""
        used_mock = False
        if self.use_real_planner:
            self._set_agent_status(
                "Planner", "running", "calling claude with YAML system prompt"
            )
            log.write("[bold cyan][Planner][/] decomposing goal...")
            try:
                tasks, planner_cost = await plan(self.goal or "", self.repo)  # type: ignore[arg-type]
                self._tasks = tasks
                self._cost_usd += planner_cost
                log.write(
                    f"[green][Planner] ✓ {len(tasks)} tasks, "
                    f"cost ${planner_cost:.4f}[/]"
                )
            except PlannerError as e:
                self._artifacts_dir.mkdir(parents=True, exist_ok=True)
                (self._artifacts_dir / "planner-error.log").write_text(str(e))
                log.write(f"[red][Planner] failed:[/] {e}")
                log.write("[yellow][Planner] falling back to mock decomposer[/]")
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
                log.write(f"[red][Planner] crashed:[/] {type(e).__name__}: {e}")
                log.write(
                    f"[red][Planner] traceback saved to[/] "
                    f"{self._artifacts_dir / 'planner-crash.log'}"
                )
                log.write("[yellow][Planner] falling back to mock decomposer[/]")
                self._tasks = mock_planner_decompose(self.goal or "")
                used_mock = True
        else:
            log.write("[cyan][Planner mock][/] --mock-planner set, skipping real call")
            self._tasks = mock_planner_decompose(self.goal or "")
            used_mock = True

        # Update task table now that we have real tasks
        for t in self._tasks:
            rk = table.add_row(t.id, t.title[:30], "ready")
            self._task_rows[t.id] = rk

        # Surface acceptance + paths in the log so user can verify the plan
        for t in self._tasks:
            log.write(
                f"[dim]  {t.id} paths:[/] {', '.join(t.allowed_paths) or '(none)'}"
            )
            for crit in t.acceptance[:3]:
                log.write(f"[dim]  {t.id} accept:[/] {crit}")

        label = "mock" if used_mock else "real claude"
        self._set_agent_status(
            "Planner", "done", f"{label}: decomposed into {len(self._tasks)}"
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

            full_prompt = task.prompt + CODER_COMMIT_SUFFIX.format(task_id=task.id)

            async for ev in run_claude_async(
                full_prompt, cwd=str(wt_path), unrestricted=True
            ):
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

            # Capture diff BEFORE cleanup wipes the branch.
            diff = await asyncio.to_thread(self.wm.diff_against_base, task.id)
            self._task_diffs[task.id] = diff
            self._save_diff_artifact(task.id, diff)
            stats = self._diff_stats(diff)
            log.write(
                f"[bold magenta][{task.id}] diff:[/] "
                f"+{stats['added']} -{stats['removed']} in {stats['files']} file(s)"
            )
        finally:
            await self.wm.acleanup(task.id)
            log.write(f"[dim][{task.id}] worktree cleaned[/]")

    # ---------- artifacts ----------
    def _save_diff_artifact(self, task_id: str, diff: str) -> None:
        self._artifacts_dir.mkdir(parents=True, exist_ok=True)
        (self._artifacts_dir / f"{task_id}.diff").write_text(diff)

    @staticmethod
    def _diff_stats(diff: str) -> dict[str, int]:
        added = 0
        removed = 0
        files: set[str] = set()
        for line in diff.splitlines():
            if line.startswith("+++") or line.startswith("---"):
                # +++ b/path  /  --- a/path  -> file marker, count later
                if len(line) > 6:
                    files.add(line[6:])
                continue
            if line.startswith("+"):
                added += 1
            elif line.startswith("-"):
                removed += 1
        files.discard("ev/null")  # /dev/null appears for new files
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
            "",
        ]
        for t in self._tasks:
            diff = self._task_diffs.get(t.id, "")
            stats = self._diff_stats(diff) if diff else {"added": 0, "removed": 0, "files": 0}
            out += [
                f"## {t.id}: {t.title}",
                "",
                f"- **branch**: ai/{t.id}",
                f"- **diff**: +{stats['added']} -{stats['removed']} lines, {stats['files']} file(s)",
                f"- **allowed_paths**: {', '.join(t.allowed_paths) or '(none)'}",
                "",
                "**acceptance criteria:**",
                "",
            ]
            for c in t.acceptance:
                out.append(f"- {c}")
            out += ["", "<details><summary>diff</summary>", "", "```diff", diff[:8000], "```", "", "</details>", ""]
        path = self._artifacts_dir / "summary.md"
        path.write_text("\n".join(out))
        return path

    # ---------- ui helpers ----------
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
    ap.add_argument(
        "--mock-planner",
        action="store_true",
        help="skip the real claude-driven planner, use the cheap mock fallback",
    )
    args = ap.parse_args()

    goal = " ".join(args.goal).strip() or None
    if goal is None:
        PMAgentTUI(goal=None).run()
        return

    repo = _ensure_target_repo(args.repo)
    PMAgentTUI(
        goal=goal,
        repo=repo,
        single=args.single,
        use_real_planner=not args.mock_planner,
    ).run()


if __name__ == "__main__":
    main()
