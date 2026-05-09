"""`claude -p` subprocess runner with stream-json parsing.

Two flavors share one cmd builder:

  - run_claude_async() — async generator, yields parsed events one by one.
    Used by the TUI worker. Keeps Textual's event loop responsive.
  - run_claude() — sync wrapper. Collects events into a RunResult.
    Used by the CLI.

Both default to isolate=True, which adds --setting-sources project,local.
That skips user-level ~/.claude/settings.json (where global hooks live —
laziness-self-report, teamagent, etc) so spawned children produce clean
output and run ~3x cheaper than the default Claude Code session loader.

Usage:
    python -m pm_agent.runner "what is 2+2"
    python -m pm_agent.runner "review this diff" --role "Senior code reviewer."
"""
from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from dataclasses import dataclass
from typing import AsyncIterator


@dataclass
class RunResult:
    session_id: str | None
    text: str
    cost_usd: float | None
    duration_ms: int | None
    exit_code: int


def _build_cmd(prompt: str, role: str | None, isolate: bool) -> list[str]:
    cmd = ["claude", "-p", prompt, "--output-format", "stream-json", "--verbose"]
    if isolate:
        # Skip user-level settings (~/.claude/settings.json). Drops parent's
        # global hooks; keeps keychain auth and model defaults. ~70% cost &
        # latency reduction per call vs default loader.
        cmd += ["--setting-sources", "project,local"]
    if role:
        cmd += ["--append-system-prompt", role]
    return cmd


async def run_claude_async(
    prompt: str,
    role: str | None = None,
    isolate: bool = True,
    cwd: str | None = None,
) -> AsyncIterator[dict]:
    """Spawn claude -p and yield each parsed stream-json event as a dict.

    `cwd` selects the working directory for the subprocess — the orchestrator
    points each Coder at its own git worktree so concurrent edits don't
    collide. Caller drives consumption rate. Errors during parse are
    swallowed so a malformed line doesn't kill the stream.
    """
    cmd = _build_cmd(prompt, role, isolate)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    assert proc.stdout is not None
    while True:
        raw = await proc.stdout.readline()
        if not raw:
            break
        line = raw.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue
    await proc.wait()


def run_claude(
    prompt: str, role: str | None = None, isolate: bool = True
) -> RunResult:
    """Sync runner. Streams events to stdout/stderr and returns a summary."""
    cmd = _build_cmd(prompt, role, isolate)
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1
    )
    assert proc.stdout is not None

    session_id: str | None = None
    chunks: list[str] = []
    final: dict | None = None

    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue

        et, st = ev.get("type"), ev.get("subtype")
        if et == "system" and st == "init":
            session_id = ev.get("session_id")
            print(f"[init] session={session_id} model={ev.get('model')}", file=sys.stderr)
        elif et == "assistant":
            for part in ev.get("message", {}).get("content", []):
                if part.get("type") == "text":
                    chunks.append(part["text"])
                    print(part["text"], end="", flush=True)
        elif et == "result":
            final = ev

    proc.wait()
    print()
    return RunResult(
        session_id=session_id,
        text="".join(chunks),
        cost_usd=(final or {}).get("total_cost_usd"),
        duration_ms=(final or {}).get("duration_ms"),
        exit_code=proc.returncode,
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("prompt")
    ap.add_argument("--role", help="role-specific append-system-prompt")
    args = ap.parse_args()

    r = run_claude(args.prompt, args.role)
    cost = f"${r.cost_usd:.4f}" if r.cost_usd is not None else "?"
    dur = f"{r.duration_ms}ms" if r.duration_ms is not None else "?"
    print(f"[done] cost={cost} duration={dur} exit={r.exit_code}", file=sys.stderr)


if __name__ == "__main__":
    main()
