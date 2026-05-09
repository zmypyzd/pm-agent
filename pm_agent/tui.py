"""TUI for pm-agent.

Two modes:

  1. Mock mode (no argv): the day-2 skeleton with mock data + ticker.
     Useful for verifying layout without burning API calls.
       uv run python -m pm_agent.tui

  2. Real mode (goal as argv): day-3/4 wiring. Spawns one `claude -p`
     subprocess via run_claude_async(), streams parsed events into the
     right panel, updates cost/duration in the footer, marks Coder-1 as
     running/done in the center panel.
       uv run python -m pm_agent.tui "what is 2+2 in one word"

Five panels (per design doc):
  top    — goal + progress bar
  left   — task table
  center — agent status cards
  right  — RichLog stream of subprocess output
  bottom — cost / events / elapsed

q to quit.
"""
from __future__ import annotations

import sys
import time
from datetime import timedelta

from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Label, ProgressBar, RichLog, Static

from pm_agent.runner import run_claude_async

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


class PMAgentTUI(App):
    """Day-3/4 wiring: mock mode + real-claude streaming mode."""

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
    _event_count: int = 0
    _cost_usd: float = 0.0
    _streamed_text: str = ""

    def __init__(self, goal: str | None = None) -> None:
        super().__init__()
        self.goal = goal
        self.is_mock = goal is None

    def compose(self) -> ComposeResult:
        display_goal = self.goal if self.goal else f"{GOAL_MOCK}"
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
        table.add_columns("ID", "Task", "Status")
        if self.is_mock:
            for row in TASKS_MOCK:
                table.add_row(*row)
        else:
            table.add_row("T-1", (self.goal or "")[:40], "running")
        log = self.query_one("#logs", RichLog)
        if self.is_mock:
            log.write("[green]pm-agent TUI started (mock mode)[/]")
            log.write(f"[dim]goal:[/] {GOAL_MOCK}")
            self.set_interval(0.8, self._tick)
        else:
            log.write("[green]pm-agent TUI started (real mode)[/]")
            log.write(f"[dim]goal:[/] {self.goal}")
            self._stream_real()

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
    async def _stream_real(self) -> None:
        log = self.query_one("#logs", RichLog)
        progress = self.query_one("#progress", ProgressBar)
        footer = self.query_one("#footer-bar", Static)

        # Reset agent cards: only Coder-1 active for single-subprocess demo.
        self._set_agent_status("Planner",  "done",    "Goal accepted")
        self._set_agent_status("Coder-1",  "running", "Streaming claude -p")
        self._set_agent_status("Coder-2",  "idle",    "(parallel agents — day 5+)")
        self._set_agent_status("Reviewer", "idle",    "(needs result — day 5+)")

        log.write(f"[cyan]→ spawning claude -p[/] {self.goal!r}")

        async for ev in run_claude_async(self.goal or ""):
            self._event_count += 1
            et, st = ev.get("type"), ev.get("subtype")

            if et == "system" and st == "init":
                sid = (ev.get("session_id") or "")[:8]
                log.write(f"[dim]session={sid} model={ev.get('model')}[/]")
            elif et == "assistant":
                for part in ev.get("message", {}).get("content", []):
                    if part.get("type") == "text":
                        text = part["text"]
                        self._streamed_text += text
                        log.write(text)
            elif et == "result":
                cost = ev.get("total_cost_usd")
                dur = ev.get("duration_ms")
                self._cost_usd = float(cost) if cost is not None else 0.0
                log.write(
                    f"[bold green]✓ result[/] cost=${self._cost_usd:.4f} "
                    f"dur={dur}ms turns={ev.get('num_turns')}"
                )
                progress.update(progress=100)
                self._set_agent_status("Coder-1", "done", "Subprocess finished")
            elif et == "system" and st == "notification":
                log.write(f"[yellow]⚠ {ev.get('text')}[/]")

            elapsed = time.time() - self._start_time
            footer.update(
                self._footer_text(self._cost_usd, self._event_count, elapsed, "events")
            )

        log.write("[bold green]done[/]")

    def _set_agent_status(self, name: str, status: str, action: str) -> None:
        try:
            card = self.query_one(f"#agent-{name}", Static)
        except Exception:
            return
        card.update(f"[bold]{name}[/]\n[dim]{action}[/]")
        # Replace status class
        for cls in ("status-running", "status-done", "status-idle", "status-blocked"):
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


def main() -> None:
    goal = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else None
    PMAgentTUI(goal=goal).run()


if __name__ == "__main__":
    main()
