# pm-agent

A multi-agent orchestrator that drives Claude Code subprocesses toward a user-defined goal, with a live TUI dashboard showing each agent's state.

**Status**: day 1 skeleton.

**Deadline**: 2 weeks.

**Stack**: Python 3.11 + Textual + git worktree + Claude Code CLI (`claude -p`).

**Roles**: Planner / Coder (parallel) / Reviewer.

**Design doc**: `~/.gstack/projects/agenter/zmy-no-git-design-20260509-110211.md`

**Source PRD** (deprecated, scope reduced): `../multi_agent_product_orchestrator_prd.md`

## Quickstart

```bash
uv sync

# CLI subprocess runner (day 1)
uv run python -m pm_agent.runner "what is 2+2"
uv run python -m pm_agent.runner "review this code" --role "You are a senior code reviewer."

# TUI mock mode (day 2 — fake data, q to quit)
uv run python -m pm_agent.tui

# TUI single-coder real mode (day 3-4)
uv run python -m pm_agent.tui --single "say only the word four"

# TUI multi-coder + real Planner (day 6 — Planner LLM call decomposes goal into YAML)
uv run python -m pm_agent.tui "Add JSONL logger for each claude call"

# Skip the real Planner for cheap iteration:
uv run python -m pm_agent.tui "demo" --mock-planner

# Day-7 end-to-end demo: prepares a target repo and runs full Planner -> 2 Coders -> diff
mkdir -p /tmp/pm-agent-day7-target && cd /tmp/pm-agent-day7-target
git init -q && echo init > README.md && git add . && git commit -q -m init
# (or copy the seed server.py + tests/test_server.py used in the day-7 verification)
cd -
uv run python -m pm_agent.tui --repo /tmp/pm-agent-day7-target "Add /health endpoint to handle_request returning {status: ok} as a dict, plus a test"

# After the run completes:
ls ~/.pm-agent/runs/                         # one dir per run
cat ~/.pm-agent/runs/<latest>/summary.md     # PR-style report with diffs
git -C /tmp/pm-agent-day7-target apply ~/.pm-agent/runs/<latest>/T-1.diff
git -C /tmp/pm-agent-day7-target apply ~/.pm-agent/runs/<latest>/T-2.diff
```

Snapshots:
- `docs/tui-day2-snapshot.svg` — mock layout
- `docs/tui-day3-real-snapshot.svg` — single-coder, 4 events, $0.057
- `docs/tui-day5-multi-snapshot.svg` — 2 parallel coders (mock plan), 8 events, $0.112
- `docs/tui-day6-planner-snapshot.svg` — real Planner emits 2 file-disjoint YAML tasks
- `docs/tui-day7-e2e-snapshot.svg` — full run: real Planner + 2 unrestricted Coders editing real code, diffs captured to artifacts dir

Architecture:
- `pm_agent/tasks.py` — shared `CoderTask` schema (id / title / prompt / allowed_paths / acceptance)
- `pm_agent/runner.py` — `run_claude_async(prompt, role, isolate, cwd, unrestricted)` async event stream; `unrestricted=True` adds `--dangerously-skip-permissions` for Coders that need to Edit/Write/Bash
- `pm_agent/worktree.py` — `WorktreeManager.{create,cleanup,acreate,acleanup,diff_against_base}`; auto-detects base branch (master/main)
- `pm_agent/planner.py` — `plan(goal, repo)` calls claude with strict YAML system prompt; parser tolerates fenced/naked YAML, strips backticks defensively, rejects `{}`/`[]` literals via prompt rule
- `pm_agent/tui.py` — Textual app. Planner runs first (with mock fallback on PlannerError); `asyncio.gather` over `_stream_one(task)` per Coder; each Coder commits in its worktree, diff is captured before cleanup; final `summary.md` written to `~/.pm-agent/runs/<run-id>/`

## Day 1 known issues / mitigations

- **[FIXED 2026-05-09]** Hook inheritance + cost (single fix solved both).
  Was: spawned `claude -p` inherited `~/.claude/settings.json` hooks (laziness-self-report
  Stop hook, teamagent SessionStart hook), and loaded the full ~40k token user-level
  system prompt. The laziness Stop hook **replaced the child's real answer** with a
  forced self-report re-emission. Cost was ~$0.17/call.
  Fix: pass `--setting-sources project,local` when spawning. This skips user-level
  settings (where the hooks live) but keeps keychain auth and model defaults.
  After fix: clean output, `$0.056`/call (-68%), 2.7s/call (-71%).
  Implemented in `runner.py` as `isolate=True` (default).
  Alternative (not used): `--bare` — even more aggressive, but requires `ANTHROPIC_API_KEY`
  env var because it bypasses keychain.
- **[FIXED 2026-05-09]** `~/.teamagent/hooks/bin-session-start.cjs` no longer errors.
  Was: `Cannot find module 'web-tree-sitter'`. Fix applied: `cd ~/.teamagent && npm i web-tree-sitter`.
