# pm-agent

A multi-agent orchestrator that drives `claude -p` subprocesses toward a user-defined goal, with a live Textual TUI dashboard. Planner decomposes the goal into a file-disjoint task DAG; Coders run in parallel git worktrees; integration merges + tests the result.

**Status**: demo-ready (Day 14 of 14). 3/3 zero-incident dry-runs on the canonical `/health` goal (~50s wall, ~$0.35 each).

**New here? → [`docs/QUICKSTART.md`](docs/QUICKSTART.md)** — 5-minute hands-on guide.

**Stack**: Python 3.11 + Textual + git worktree + Claude Code CLI (`claude -p`).

**Roles**: Planner / Coder (parallel) / Integration.

## Quickstart

```bash
uv sync

# Mock TUI — visual only, free
uv run python -m pm_agent.tui

# Interactive TUI — type goals in the input bar, run back-to-back
uv run python -m pm_agent.tui --interactive --repo /tmp/pm-agent-day7-target

# Single Coder real run
uv run python -m pm_agent.tui --single "say only the word four"

# Full e2e (real Planner + 2 Coders + integration test)
bash docs/demo-commands.sh full_e2e_dry_run

# Three deterministic fault modes (no real LLM Coder calls)
uv run python -m pm_agent.tui --repo /tmp/pm-agent-day7-target \
    --inject-fault planner-yaml "add /version endpoint"
# also: --inject-fault coder-timeout | --inject-fault api-error
```

After a run:

```bash
ls ~/.pm-agent/runs/                                 # one dir per run
cat ~/.pm-agent/runs/<latest>/summary.md             # PR-style report
git -C /tmp/pm-agent-day7-target apply ~/.pm-agent/runs/<latest>/integration.diff
```

## Architecture

| Module | Responsibility |
|---|---|
| `pm_agent/tasks.py` | `CoderTask` dataclass: id / title / prompt / allowed_paths / acceptance |
| `pm_agent/runner.py` | `run_claude_async`: spawns `claude -p` with `--setting-sources project,local` to avoid parent-session hook + system-prompt pollution; async event generator |
| `pm_agent/worktree.py` | `WorktreeManager`: per-task worktree on `ai/<task_id>`, integration worktree on `ai/integration/<run_id>`, base branch auto-detect (master/main), full cleanup in `finally` |
| `pm_agent/planner.py` | `plan(goal, repo)`: real claude call → strict YAML output; backtick-strip retry; up to 3 attempts with error feedback |
| `pm_agent/tui.py` | Textual app. Modes: mock / single / multi-coder real / mock-planner / inject-fault. `@work` coroutines for Planner + parallel Coders + integration. |

## Demo materials

- **Live recording script**: `docs/demo-narrative.md` — 9 beats with talk-time budgets, known-limit Q&A cheat-sheet
- **Copy-paste cheat-sheet**: `docs/demo-commands.sh` — 18 named functions, one per beat
- **SVG snapshots** (`docs/`):
  - `tui-day2-snapshot.svg` — mock 5-panel layout
  - `tui-day3-real-snapshot.svg` — single Coder, real stream, $0.057
  - `tui-day5-multi-snapshot.svg` — 2 parallel Coders via worktree, $0.112
  - `tui-day6-planner-snapshot.svg` — real Planner emitting 2-task YAML
  - `tui-day7-e2e-snapshot.svg` — full e2e: real Planner + 2 Coders + diff capture
  - `tui-day8-integration-snapshot.svg` — integration merge + tests
  - `tui-day10-polish-snapshot.svg` + `tui-day10-e2e-snapshot.svg` — visual polish + verified e2e
  - `tui-day11-fault-{planner-yaml,coder-timeout,api-error}-snapshot.svg` — three deterministic recovery demos
  - `tui-day11-stats-snapshot.svg` — second positive demo (request counter + /stats)

## Implementation notes (load-bearing)

- **`--setting-sources project,local` for spawned `claude -p` is mandatory, not an optimization.** Without it, the parent session's user-level hooks (`laziness-self-report` Stop hook etc.) fire inside the child and overwrite its real answer with a forced self-report; cost also jumps from $0.05 to $0.17 per call because the full ~40k token user-level system prompt is loaded. See `runner.py:isolate=True` default.
- **Coders need `--dangerously-skip-permissions`.** `claude -p` defaults to deny-all tool calls; without this flag a Coder can't Edit/Write/Bash. Safety boundary is the worktree sandbox itself — bad diffs are intercepted by the integration test, not by claude's permission check.
- **Branches are deleted by `finally` but the worktree directory is removed first.** This is intentional: integration needs the branch to still exist after per-task worktree cleanup. See `worktree.py:cleanup_worktree` vs `delete_branch`.
- **Default target repo branch is `master`, not `main`.** WorktreeManager auto-detects.
- **Mock planner is a fallback, not a degraded mode.** `--mock-planner` exists for layout debugging; real Planner is the always-on primary path with 3-attempt retry + mock as last-resort fallback.

## Known limits (see `docs/demo-narrative.md` for one-line answers)

1. **Contract drift between Coders.** Planner pins shared names in acceptance criteria, integration test catches drift in the merge result. Future: explicit `shared_contract` field in Planner output + Reviewer agent.
2. **Local-only.** PoC. Productionization path: push branches to remote, auto-open PRs to GitHub, Reviewer agent contract checks, cross-session run persistence.
3. **2-3 Coder default.** `asyncio.gather` is N-way; only the Planner system prompt caps the task count.
4. **No automatic rate-limit backoff.** Rate-limit events are surfaced (see `tui-day11-fault-api-error-snapshot.svg`) and recorded in `summary.md`, but retry is the user's decision (to avoid burning money on hard caps).
