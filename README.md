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

# subprocess runner (day 1)
uv run python -m pm_agent.runner "what is 2+2"
uv run python -m pm_agent.runner "review this code" --role "You are a senior code reviewer."

# TUI skeleton (day 2 — mock data only, q to quit)
uv run python -m pm_agent.tui
```

Day 2 TUI snapshot: `docs/tui-day2-snapshot.svg`

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
