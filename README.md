# pm-agent

A multi-agent orchestrator that drives `claude -p` subprocesses toward a user-defined goal, with a live Textual TUI dashboard. Planner decomposes the goal into a file-disjoint task DAG; Coders run in parallel git worktrees; integration merges + tests the result.

**Status**: demo-ready (Day 14 of 14). 3/3 zero-incident dry-runs on the canonical `/health` goal (~50s wall, ~$0.35 each).

**Stack**: Python 3.11 + Textual + git worktree + Claude Code CLI (`claude -p`).

**Roles**: Planner / Coder (parallel) / Integration.

## Install (one line)

```bash
uv tool install git+https://github.com/zmypyzd/pm-agent
```

That's it. `pm-agent` is now a global command — no clone, no `cd`, no virtualenv.

Prerequisites that pip can't bundle:

| Tool | Install |
|---|---|
| `uv` | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| Claude CLI | `npm i -g @anthropic-ai/claude-code` (then `claude` once to log in) |
| `git` | macOS ships it; Linux `apt install git` |

Upgrade later: `uv tool install git+https://github.com/zmypyzd/pm-agent --reinstall`.

## 30-second demo

```bash
pm-agent demo
```

Zero flags. Creates `/tmp/pm-agent-day7-target` from scratch and runs the canonical
`/health` endpoint goal end-to-end through the TUI (~50-100s, ~$0.35). Watch
2 Coders work in parallel; integration merges + tests their diffs.

**New here? → [`docs/QUICKSTART.md`](docs/QUICKSTART.md)** — the 5-minute hands-on guide.

## Desktop pet (no CLI)

A small always-on-top duck that displays live cycle state and runs the
same actions from a right-click menu — no terminal needed once it's
launched.

```bash
# Build the .app (one-time, ~5 min fresh / ~12s incremental)
cd desktop && npm install && npm run tauri build -- --bundles app dmg

# Ad-hoc sign so macOS doesn't quarantine on first launch.
APP=src-tauri/target/release/bundle/macos/pm-agent-pet.app
codesign --force --deep --sign - "$APP"

# Install
cp -R "$APP" /Applications/

# Optional: launch on every login
./scripts/autostart.sh install
```

Built outputs:
- `desktop/src-tauri/target/release/bundle/macos/pm-agent-pet.app` (10 MB)
- `desktop/src-tauri/target/release/bundle/dmg/pm-agent-pet_0.1.0_aarch64.dmg` (3.8 MB, drag-to-Applications installer)

Both are ad-hoc signed (`codesign -s -`). Distribution to other Macs
still requires an Apple Developer ID; ad-hoc signing only suppresses
the Gatekeeper warning for the building machine.

What it does:
- Frameless transparent always-on-top window, draggable, ~10 MB bundle.
- Right-click → Run demo / Start loop / Open dashboard / Quit.
- Reads `~/.pm-agent/state.db` every 2s; badge reflects live state
  (`▶ Nf / $X.XX` while a cycle is running, `✓ $X.XX` when done, etc.).
- Speaks via speech bubbles on transitions: cycle started, PR opened,
  cycle done (happy bounce), cycle errored (sad droop, priority alert).
- Drifts to a "sleeping" pose with floating Zz when there's no
  state.db.
- Window position persists across launches.

## Other entry points

```bash
# Mock TUI — visual only, free
pm-agent tui

# Interactive TUI — type goals in the input bar, run back-to-back
pm-agent tui --interactive --repo /tmp/pm-agent-day7-target

# Autonomous bug-hunt loop daemon
pm-agent loop preflight     # 30s readiness check
pm-agent loop run           # start the daemon
pm-agent loop report        # PR-style summary of the run

# Three deterministic fault modes (no real LLM Coder calls)
pm-agent tui --repo /tmp/pm-agent-day7-target \
    --inject-fault planner-yaml "add /version endpoint"
# also: --inject-fault coder-timeout | --inject-fault api-error
```

## Developing on this repo

```bash
git clone https://github.com/zmypyzd/pm-agent && cd pm-agent
uv sync                                   # local venv with dev deps
uv run pytest tests/                      # run the test suite
uv run python -m pm_agent.tui             # run from source without install
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
