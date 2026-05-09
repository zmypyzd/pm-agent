"""Minimal `claude -p` subprocess runner with stream-json parsing.

Day 1 spike: prove we can spawn claude non-interactively, parse NDJSON
event-by-event, extract assistant text and final cost. This is the kernel
the rest of the orchestrator will be built on.

Usage:
    python -m pm_agent.runner "what is 2+2"
    python -m pm_agent.runner "review this diff" --role "You are a code reviewer."
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass


@dataclass
class RunResult:
    session_id: str | None
    text: str
    cost_usd: float | None
    duration_ms: int | None
    exit_code: int


def run_claude(
    prompt: str, role: str | None = None, isolate: bool = True
) -> RunResult:
    cmd = ["claude", "-p", prompt, "--output-format", "stream-json", "--verbose"]
    if isolate:
        # Skip user-level settings (~/.claude/settings.json). This drops the
        # parent session's hooks (laziness-self-report, teamagent SessionStart,
        # etc) without affecting auth (keychain still works) or model defaults.
        # Side benefit: ~70% cost & latency reduction per call vs full settings.
        cmd += ["--setting-sources", "project,local"]
    if role:
        cmd += ["--append-system-prompt", role]

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
