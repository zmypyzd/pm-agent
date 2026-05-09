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
uv run python -m pm_agent.runner "what is 2+2"
uv run python -m pm_agent.runner "review this code" --role "You are a senior code reviewer."
```

## Day 1 known issues / mitigations

- **[CONFIRMED]** Baseline cost per `claude -p` invocation: ~$0.17 (40k token system prompt overhead).
  Mitigation: use `--append-system-prompt` per role; for production demo consider Anthropic SDK direct (skips Claude Code session loader).
- **[CONFIRMED, OPEN]** Hook inheritance: spawned `claude -p` inherits all `~/.claude/settings.json` hooks.
  Verified: parent session's `laziness-self-report` stop-hook injected into child's output, **replaced the real answer**.
  Mitigation TBD: clean HOME or `--no-settings` flag for child processes.
- **[FIXED 2026-05-09]** `~/.teamagent/hooks/bin-session-start.cjs` no longer errors.
  Was: `Cannot find module 'web-tree-sitter'`. Fix applied: `cd ~/.teamagent && npm i web-tree-sitter`.
