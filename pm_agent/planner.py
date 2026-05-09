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
- Do NOT use backticks (`) anywhere inside string values. Backticks are
  illegal as the first non-whitespace character of a YAML scalar and will
  break the parser.
- Do NOT use markdown formatting (no **, no _, no [], no `). Plain prose only.
- If a string contains a colon, quote it with single quotes.
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


async def plan(goal: str, repo: Path) -> tuple[list[CoderTask], float]:
    """Run the planner. Returns (tasks, planner_cost_usd).

    Raises PlannerError on parse/validation failure.
    """
    context = build_repo_context(repo)
    user_prompt = PLANNER_USER.format(goal=goal, files=context)
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

    text = "".join(chunks)
    if not text.strip():
        raise PlannerError("planner returned empty response")

    yaml_block = extract_yaml(text)
    tasks = parse_tasks(yaml_block)
    validate_disjoint(tasks)
    return tasks, cost
