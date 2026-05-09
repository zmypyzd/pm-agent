"""TUI skeleton for pm-agent (day 2, task 1).

Five panels per design doc:
  top    — goal + progress bar
  left   — task list / DAG
  center — agent status cards
  right  — live log stream
  bottom — cost / tokens / elapsed time

Mock data only at this stage. Real subprocess wiring lands day 3-4.
A 0.8s ticker advances mock log + progress so we can confirm the TUI
isn't frozen and refresh works.

Run: `uv run python -m pm_agent.tui`     (q to quit)
"""
from __future__ import annotations

import time
from datetime import timedelta

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Label, ProgressBar, RichLog, Static

GOAL_MOCK = "Add room invite link API to werewolf platform"

AGENTS_MOCK = [
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


class PMAgentTUI(App):
    """Day-2 skeleton — mock data only, demonstrates layout + ticker."""

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

    #footer-bar {
        height: 3;
        background: $secondary 40%;
        padding: 0 1;
    }
    """

    BINDINGS = [("q", "quit", "Quit")]

    _start_time: float = 0.0
    _tick_counter: int = 0

    def compose(self) -> ComposeResult:
        with Vertical(id="goal-bar"):
            yield Label(f"Goal: {GOAL_MOCK}", id="goal")
            yield ProgressBar(total=100, show_eta=False, id="progress")
        with Horizontal(id="main"):
            with Vertical(id="left"):
                yield Static("Tasks", classes="panel-title")
                yield DataTable(id="tasks", show_header=True, zebra_stripes=True)
            with Vertical(id="center"):
                yield Static("Active Agents", classes="panel-title")
                for name, status, action in AGENTS_MOCK:
                    yield Static(
                        f"[bold]{name}[/]\n[dim]{action}[/]",
                        classes=f"agent-card status-{status}",
                    )
            with Vertical(id="right"):
                yield Static("Live Log", classes="panel-title")
                yield RichLog(id="logs", wrap=True, highlight=True, markup=True)
        yield Static(self._footer_text(0.0, 0, 0.0), id="footer-bar")

    def on_mount(self) -> None:
        self._start_time = time.time()
        table = self.query_one("#tasks", DataTable)
        table.add_columns("ID", "Task", "Status")
        for tid, title, status in TASKS_MOCK:
            table.add_row(tid, title, status)
        log = self.query_one("#logs", RichLog)
        log.write("[green]pm-agent TUI started[/]")
        log.write(f"[dim]goal:[/] {GOAL_MOCK}")
        self.set_interval(0.8, self._tick)

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
        footer.update(self._footer_text(cost, tokens, elapsed))

    @staticmethod
    def _footer_text(cost: float, tokens: int, elapsed: float) -> str:
        elapsed_str = str(timedelta(seconds=int(elapsed)))
        return (
            f"cost: [bold green]${cost:.4f}[/]   "
            f"tokens: [bold cyan]{tokens:,}[/]   "
            f"elapsed: [bold]{elapsed_str}[/]   "
            f"|   [dim]q to quit[/]"
        )


def main() -> None:
    PMAgentTUI().run()


if __name__ == "__main__":
    main()
