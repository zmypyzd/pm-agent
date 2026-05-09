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
uv run pm-agent run "<your dev goal here>"
```

## Day 1 known issues / mitigations

- `claude -p "hello"` baseline cost: $0.17 (40k token system prompt overhead).
  Mitigation: use `--append-system-prompt` per role, not default; consider Anthropic SDK direct.
- Hook inheritance: spawned `claude -p` inherits all `~/.claude/settings.json` hooks.
  Mitigation TBD: clean HOME or `--no-settings` flag.
- `~/.teamagent/hooks/bin-session-start.cjs` is broken (missing `web-tree-sitter`).
  Pollutes stream-json output. Fix: `cd ~/.teamagent && npm i web-tree-sitter`, or disable hook.
