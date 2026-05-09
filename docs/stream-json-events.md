# `claude -p --output-format stream-json` event reference

Captured: 2026-05-09. Claude Code 2.1.137. Command: `claude -p "hello" --output-format stream-json --verbose`.

Output is **NDJSON** (one JSON object per line). Parse line-by-line.

## Event types observed

| `type`            | `subtype`         | When                                              | Key fields |
|-------------------|-------------------|---------------------------------------------------|------------|
| `system`          | `hook_started`    | A SessionStart/Stop/etc hook begins               | `hook_id`, `hook_name`, `hook_event`, `session_id` |
| `system`          | `hook_response`   | Hook finishes                                     | `hook_id`, `output`, `stdout`, `stderr`, `exit_code`, `outcome` |
| `system`          | `init`            | Session initialized                               | `cwd`, `tools[]`, `mcp_servers[]`, `model`, `permissionMode`, `slash_commands[]`, `agents[]`, `skills[]`, `plugins[]` |
| `system`          | `notification`    | System notification (e.g. stop-hook-error)        | `key`, `text`, `priority` |
| `assistant`       | (none)            | LLM response                                      | `message.content[]`, `message.usage{input_tokens, output_tokens, cache_*}` |
| `user`            | (none)            | User message; `isSynthetic:true` for hook injects | `message.content[]` |
| `rate_limit_event`| (none)            | Rate limit info                                   | `rate_limit_info{status, resetsAt, rateLimitType, overageStatus}` |
| `result`          | `success`/`error` | Final result (last event)                         | `duration_ms`, `total_cost_usd`, `num_turns`, `result`, `usage`, `terminal_reason` |

## Common fields

- `session_id`: stable across all events in one session
- `uuid`: unique per event
- `parent_tool_use_id`: nested in tool use chains

## Cost accounting

The final `result` event has `total_cost_usd` directly. No need to compute from token usage.

For "hello" → "Hello! How can I help you today?":
- `total_cost_usd`: 0.173902
- `duration_ms`: 11433
- `num_turns`: 2 (synthetic stop-hook injected a turn)

## Cost driver: cache vs raw input

`usage.cache_creation_input_tokens` (22463) and `cache_read_input_tokens` (18048) dominate.
The "raw" `input_tokens` was just 12. **Default Claude Code session loads ~40k tokens of system prompt** (skills, plugins, MCP, settings).

## Hook pollution

Spawned `claude -p` inherits `~/.claude/settings.json` hooks. Observed events include:
- 5 SessionStart hooks fired
- 1 hook errored: `Cannot find module 'web-tree-sitter'` from `/Users/zmy/.teamagent/hooks/bin-session-start.cjs`
- 1 stop-hook synthetic injection (`laziness-self-report` block required)

For autonomous agents, these will fire on every spawn. Plan: either filter them out of the parsed stream, or run `claude -p` with a clean HOME.

## Parser strategy for pm-agent

```python
# Pseudocode
for line in proc.stdout:
    event = json.loads(line)
    if event["type"] == "system" and event["subtype"] == "init":
        # capture session_id, model, tools
    elif event["type"] == "assistant":
        # stream message text to TUI
    elif event["type"] == "system" and event["subtype"] in ("hook_started", "hook_response"):
        # filter out, or show in a separate "noise" panel
    elif event["type"] == "result":
        # capture total_cost_usd, duration_ms; agent done
```
