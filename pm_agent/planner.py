"""Real Planner agent.

Calls claude with a tight system prompt that forces YAML-only output, then
parses into CoderTask list and validates that allowed_paths are disjoint
across tasks. Disjoint paths is the core safety property — without it,
multiple Coders editing the same file in separate worktrees produces
unmergeable conflicts.

On any parse / validation failure, raises PlannerError. The TUI catches
this and falls back to mock_planner_decompose so the demo never fully
deadlocks.

Day-7 hardening items (not in scope here):
- proper glob-overlap detection (currently exact-string match only)
- retry loop with parser-error feedback to claude
- richer repo context (file contents, not just paths)
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Callable

import yaml

from pm_agent.runner import run_claude_async
from pm_agent.tasks import CoderTask


class PlannerError(RuntimeError):
    pass


PLANNER_SYSTEM = """\
You are a project-planning agent for a multi-coder code-edit orchestrator.

Output rules — non-negotiable:
- Your ONLY output is one ```yaml fenced block. No prose. No explanation.
  No markdown headers. No "Here is..." preface. No "Hope this helps" suffix.
- Do NOT call any tools. Do NOT read files. Use only the context provided.

YAML string rules (critical — your output is parsed by yaml.safe_load):
- Do NOT use backticks (`) anywhere inside string values.
- Do NOT use curly braces { } inside string values. YAML reads them as
  inline mappings. Describe in plain English instead. Write
  "a dict with key status equal to ok" not '{"status": "ok"}'.
- Do NOT use square brackets [ ] inside string values. YAML reads them as
  inline lists.
- Do NOT use JSON literal values inside acceptance criteria. Describe
  shapes in plain English.
- Do NOT use markdown formatting (no **, no _, no `).
- If a string contains a colon, single-quote the entire string.
- For multi-line strings (like the prompt field) use the | block style.

Decomposition rules:
- Produce EXACTLY 2 parallel subtasks for Coder agents.
- The two tasks MUST be file-disjoint: their allowed_paths cannot overlap.
  Two coders edit different sets of files concurrently in separate git
  worktrees; overlap = unmergeable conflicts.
- Each task's prompt must be self-contained: the Coder receives only that
  prompt, with no other goal context. Re-state the relevant goal fragment
  inside the prompt.
- Each acceptance item must be testable by a Reviewer: name a file, command,
  or specific behavior. Not "code is clean" or "looks correct".
- Acceptance items are plain English describing how to verify, with no
  embedded code blocks, no backticks, no markdown.

Schema (follow exactly):
```yaml
tasks:
  - id: T-1
    title: short one-line title
    prompt: |
      multi-line self-contained instruction for the Coder
      can span as many lines as needed
    allowed_paths:
      - path/glob/one
      - path/glob/two
    acceptance:
      - first testable criterion in plain prose
      - second testable criterion in plain prose
  - id: T-2
    title: ...
    prompt: |
      ...
    allowed_paths:
      - ...
    acceptance:
      - ...
```
"""


PLANNER_USER = """\
GOAL: {goal}

REPO TRACKED FILES:
{files}

Produce the YAML now.
"""


PLANNER_RETRY_PREFIX = """\
Your previous YAML output failed to parse with this error:

  {error}

Re-emit the YAML, fixing that specific issue. Same goal and context apply.
Remember the rules: no backticks, no curly braces, no square brackets, no
markdown inside string values.

"""


def build_repo_context(repo: Path, max_files: int = 60) -> str:
    try:
        out = subprocess.run(
            ["git", "ls-files"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except subprocess.CalledProcessError:
        return "(no tracked files — fresh repo)"
    files = out.splitlines()[:max_files]
    return "\n".join(files) if files else "(empty repo)"


def extract_yaml(text: str) -> str:
    """Pull YAML out of a fenced block; tolerate naked YAML as a fallback."""
    m = re.search(r"```ya?ml\s*\n(.*?)\n```", text, re.DOTALL)
    if m:
        return m.group(1)
    m = re.search(r"```\s*\n(.*?)\n```", text, re.DOTALL)
    if m:
        return m.group(1)
    return text.strip()


def parse_tasks(yaml_text: str) -> list[CoderTask]:
    try:
        data = yaml.safe_load(yaml_text)
    except yaml.YAMLError:
        # Defensive retry: backticks (markdown leak) are the most common
        # cause of YAML parse failures in LLM output. Strip and try again.
        cleaned = yaml_text.replace("`", "")
        try:
            data = yaml.safe_load(cleaned)
        except yaml.YAMLError as e:
            raise PlannerError(
                f"YAML parse failed (even after backtick strip): {e}"
            ) from e
    if not isinstance(data, dict) or "tasks" not in data:
        raise PlannerError("expected top-level mapping with key 'tasks'")
    raw = data["tasks"]
    if not isinstance(raw, list) or not raw:
        raise PlannerError("'tasks' must be a non-empty list")
    out: list[CoderTask] = []
    for i, t in enumerate(raw):
        if not isinstance(t, dict):
            raise PlannerError(f"task #{i} is not a mapping")
        try:
            out.append(
                CoderTask(
                    id=str(t["id"]),
                    title=str(t["title"]),
                    prompt=str(t["prompt"]),
                    allowed_paths=list(t.get("allowed_paths") or []),
                    acceptance=list(t.get("acceptance") or []),
                )
            )
        except KeyError as e:
            raise PlannerError(f"task #{i} missing required field {e}") from e
    return out


def validate_disjoint(tasks: list[CoderTask]) -> None:
    """Reject overlapping allowed_paths across tasks (exact match)."""
    claimed: dict[str, str] = {}
    for t in tasks:
        if not t.allowed_paths:
            raise PlannerError(f"{t.id} has no allowed_paths")
        if not t.acceptance:
            raise PlannerError(f"{t.id} has no acceptance criteria")
        for path in t.allowed_paths:
            if path in claimed and claimed[path] != t.id:
                raise PlannerError(
                    f"path conflict: {path!r} claimed by both "
                    f"{claimed[path]!r} and {t.id!r}"
                )
            claimed[path] = t.id


async def _call_planner_once(
    goal: str, repo: Path, retry_feedback: str | None = None
) -> tuple[str, float]:
    """One LLM call. Returns (raw_text, cost_usd). No parsing."""
    context = build_repo_context(repo)
    user_prompt = PLANNER_USER.format(goal=goal, files=context)
    if retry_feedback:
        user_prompt = PLANNER_RETRY_PREFIX.format(error=retry_feedback) + user_prompt
    chunks: list[str] = []
    cost: float = 0.0
    async for ev in run_claude_async(
        prompt=user_prompt,
        role=PLANNER_SYSTEM,
        isolate=True,
        cwd=str(repo),
    ):
        et = ev.get("type")
        if et == "assistant":
            for part in ev.get("message", {}).get("content", []):
                if part.get("type") == "text":
                    chunks.append(part["text"])
        elif et == "result":
            cost = float(ev.get("total_cost_usd") or 0.0)
    return "".join(chunks), cost


async def plan(
    goal: str,
    repo: Path,
    max_retries: int = 2,
    on_retry: "Callable[[int, str], None] | None" = None,
) -> tuple[list[CoderTask], float]:
    """Run the planner with up to `max_retries` self-correcting attempts.

    On a PlannerError, the next attempt prepends the error message to the
    user prompt, asking claude to fix it. Cost accumulates across attempts.
    `on_retry(attempt_num, last_error)` fires before each retry attempt
    (1-indexed). Final failure raises PlannerError with cumulative context.
    """
    total_cost = 0.0
    last_error: str | None = None
    for attempt in range(max_retries + 1):
        if attempt > 0 and on_retry is not None:
            on_retry(attempt, last_error or "(unknown)")
        text, cost = await _call_planner_once(goal, repo, retry_feedback=last_error)
        total_cost += cost
        if not text.strip():
            last_error = "planner returned empty response"
            continue
        try:
            yaml_block = extract_yaml(text)
            tasks = parse_tasks(yaml_block)
            validate_disjoint(tasks)
            return tasks, total_cost
        except PlannerError as e:
            last_error = str(e)
            if attempt == max_retries:
                raise PlannerError(
                    f"planner failed after {attempt + 1} attempt(s). "
                    f"Last error: {e}"
                ) from e
    # Unreachable due to raise above, but mypy/etc want it.
    raise PlannerError(f"planner exhausted retries: {last_error}")
