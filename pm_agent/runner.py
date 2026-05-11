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


def _build_cmd(
    prompt: str, role: str | None, isolate: bool, unrestricted: bool
) -> list[str]:
    cmd = ["claude", "-p", prompt, "--output-format", "stream-json", "--verbose"]
    if isolate:
        # Skip user-level settings (~/.claude/settings.json). Drops parent's
        # global hooks; keeps keychain auth and model defaults. ~70% cost &
        # latency reduction per call vs default loader.
        cmd += ["--setting-sources", "project,local"]
    if unrestricted:
        # Required for Coder agents: `claude -p` defaults to deny-all on
        # tool calls. Without this flag a Coder cannot Edit/Write/Bash
        # inside its sandboxed worktree. Safe in our model because each
        # Coder runs with cwd pinned to a fresh git worktree on its own
        # branch — blast radius is the worktree, not the host.
        cmd += ["--dangerously-skip-permissions"]
    if role:
        cmd += ["--append-system-prompt", role]
    return cmd


async def run_claude_async(
    prompt: str,
    role: str | None = None,
    isolate: bool = True,
    cwd: str | None = None,
    unrestricted: bool = False,
    timeout: float | None = None,
) -> AsyncIterator[dict]:
    """Spawn claude -p and yield each parsed stream-json event as a dict.

    `cwd` selects the working directory for the subprocess. `unrestricted=True`
    enables tool calls without permission prompts. `timeout` (seconds) caps
    total wall-clock; on expiry we SIGTERM the process, wait briefly, SIGKILL
    if still alive, and yield a synthetic
    {"type":"system","subtype":"timeout","elapsed_s": <t>} event before
    returning. The caller can treat that as task failure.

    Caller drives consumption rate. Errors during parse are swallowed so
    a malformed line doesn't kill the stream.

    Implementation notes:
    - stderr is sent to DEVNULL, not PIPE (BUG-001 / 049). Buffering claude's
      verbose stderr in a PIPE that nobody reads deadlocks the child as
      soon as it writes >64 KB.
    - Cancellation (TUI worker.cancel() / SIGINT) is honoured by terminating
      the child in the finally block (BUG-010). Without that, the child
      keeps running and burning tokens after the user quit.
    - OSError on spawn (e.g. claude not on PATH, ARG_MAX overrun) is
      converted to a synthetic system event so callers don't see the
      raw exception (BUG-032).
    """
    cmd = _build_cmd(prompt, role, isolate, unrestricted)
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=cwd,
        )
    except (FileNotFoundError, OSError) as e:
        yield {
            "type": "system",
            "subtype": "spawn_error",
            "error": f"{type(e).__name__}: {e}",
        }
        return
    assert proc.stdout is not None

    loop = asyncio.get_event_loop()
    deadline = None if timeout is None else loop.time() + timeout
    timed_out = False
    cancelled = False

    try:
        while True:
            if deadline is not None:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    timed_out = True
                    break
                try:
                    raw = await asyncio.wait_for(
                        proc.stdout.readline(), timeout=remaining
                    )
                except asyncio.TimeoutError:
                    timed_out = True
                    break
            else:
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
    except asyncio.CancelledError:
        cancelled = True
        raise
    finally:
        # Always actively kill the child if it's still running — covers
        # timeout, normal break, AND cancellation paths uniformly.
        if proc.returncode is None:
            try:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    proc.kill()
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=2.0)
                    except asyncio.TimeoutError:
                        pass  # zombie — let the OS reap it
            except ProcessLookupError:
                pass

    if timed_out and not cancelled:
        yield {
            "type": "system",
            "subtype": "timeout",
            "elapsed_s": timeout,
            "exit_code": proc.returncode,
        }


def run_claude(
    prompt: str,
    role: str | None = None,
    isolate: bool = True,
    unrestricted: bool = False,
) -> RunResult:
    """Sync runner. Streams events to stdout/stderr and returns a summary.

    stderr is DEVNULL'd for the same reason as the async path (BUG-001 / 049):
    an un-drained stderr PIPE deadlocks claude as soon as it logs >64 KB.
    """
    cmd = _build_cmd(prompt, role, isolate, unrestricted)
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, bufsize=1,
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
