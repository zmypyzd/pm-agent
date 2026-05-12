"""LLM scanner. Audits pm-agent repo; emits Findings with severity baked in.

Per spec §3. Mirrors planner.py's retry+YAML pattern. bug_id is a stable
sha1 hash of sorted paths + kind — title reword across cycles does NOT
change the id, so the 3-cycle skip gate is robust.
"""
from __future__ import annotations

import hashlib
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

import yaml  # type: ignore[import-untyped]

from pm_agent.runner import run_claude_async


Severity = Literal["Critical", "High", "Medium", "Low"]
Kind = Literal["bug", "tech-debt"]


@dataclass
class Finding:
    bug_id: str
    title: str
    severity: Severity
    paths: list[str]
    acceptance: list[str]
    evidence: str
    kind: Kind


SCANNER_SYSTEM = """\
You are an autonomous code auditor for the pm-agent repository.

Your ONLY output is one ```yaml fenced block. No prose. Do NOT call tools.

Find up to {max_findings} bugs in the repository.{fallback_hint}

YAML rules — non-negotiable:
- No backticks, no curly braces, no square brackets inside string values.
- Plain English only for evidence/acceptance.
- For multi-line strings use | block style.

Schema:
```yaml
findings:
  - title: one-line description
    severity: Critical | High | Medium | Low
    paths:
      - relative/path/from/repo/root.py
    acceptance:
      - testable verification 1
      - testable verification 2
    evidence: |
      file:line and short reason
    kind: bug | tech-debt
```
"""


_USER_PROMPT = """\
REPO ROOT: {repo}
TRACKED FILES:
{files}

Produce the YAML now.
"""


def _normalize_bug_id(paths: list[str], kind: Kind) -> str:
    """sha1(kind + RS + RS.join(sorted(paths)))[:8].

    Uses ASCII 0x1e (Record Separator) which is not a valid char in POSIX
    filenames, preventing collisions like ['a|b'] vs ['a', 'b'] that the
    earlier '|' separator would conflate.
    """
    rs = "\x1e"
    payload = f"{kind}{rs}" + rs.join(sorted(paths))
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:8]


def _list_repo_files(repo: Path, max_files: int = 60) -> str:
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=repo,
            capture_output=True,
            check=True,
        ).stdout
    except subprocess.CalledProcessError:
        return "(no tracked files)"
    raw = [b.decode("utf-8", errors="replace") for b in out.split(b"\x00") if b]

    def _safe(name: str) -> bool:
        return all(0x20 <= ord(c) < 0x7f or ord(c) >= 0x80 for c in name)

    safe = [f for f in raw if _safe(f)]
    files = safe[:max_files]
    return "<FILES>\n" + "\n".join(files) + "\n</FILES>" if files else "(empty)"


def _extract_yaml(text: str) -> str:
    """Pull YAML out of a fenced block; tolerate naked YAML as fallback.

    Tries ```yaml first, then plain ``` (planner.py pattern), then raw text.
    """
    m = re.search(r"```ya?ml\s*\n(.+?)\n?```", text, re.DOTALL)
    if m:
        return m.group(1)
    m = re.search(r"```\s*\n(.+?)\n?```", text, re.DOTALL)
    if m:
        return m.group(1)
    return text.strip()


def _parse_findings(yaml_text: str) -> list[Finding]:
    data = yaml.safe_load(yaml_text)
    if not isinstance(data, dict) or "findings" not in data:
        return []
    out: list[Finding] = []
    for entry in data.get("findings") or []:
        if not isinstance(entry, dict):
            continue
        try:
            paths = [str(p) for p in (entry.get("paths") or []) if str(p).strip()]
            if not paths:
                continue
            kind = str(entry.get("kind", "bug"))
            if kind not in ("bug", "tech-debt"):
                kind = "bug"
            sev = str(entry.get("severity", "Medium"))
            if sev not in ("Critical", "High", "Medium", "Low"):
                sev = "Medium"
            out.append(Finding(
                bug_id=_normalize_bug_id(paths, kind),  # type: ignore[arg-type]
                title=str(entry.get("title", "untitled")).strip(),
                severity=sev,  # type: ignore[arg-type]
                paths=paths,
                acceptance=[str(a) for a in (entry.get("acceptance") or [])],
                evidence=str(entry.get("evidence", "")),
                kind=kind,  # type: ignore[arg-type]
            ))
        except (KeyError, TypeError, ValueError):
            continue
    return out


async def scan(
    repo: Path,
    *,
    max_findings: int = 5,
    timeout: float = 120.0,
    fallback_to_tech_debt: bool = True,
    max_retries: int = 2,
    on_retry: Callable[[int, str], None] | None = None,
) -> tuple[list[Finding], float]:
    """Per spec §3. Returns (findings, cost_usd). Bug-first; tech-debt fallback
    is implemented by the system prompt — Scanner LLM is told to fall back
    if it can't find bugs."""
    user_prompt = _USER_PROMPT.format(repo=repo, files=_list_repo_files(repo))
    fallback_hint = (
        " If you find none, fall back to tech-debt (refactor opportunities, "
        "missing tests, dead code, drift)."
        if fallback_to_tech_debt else ""
    )
    system = SCANNER_SYSTEM.format(
        max_findings=max_findings,
        fallback_hint=fallback_hint,
    )
    total_cost = 0.0
    last_error: str | None = None
    for attempt in range(max_retries + 1):
        if attempt > 0 and on_retry is not None:
            on_retry(attempt, last_error or "(unknown)")
        chunks: list[str] = []
        is_error = False
        async for ev in run_claude_async(
            prompt=user_prompt,
            role=system,
            isolate=True,
            cwd=str(repo),
            timeout=timeout,
        ):
            et = ev.get("type")
            if et == "assistant":
                for part in ev.get("message", {}).get("content", []):
                    if part.get("type") == "text":
                        chunks.append(part["text"])
            elif et == "result":
                total_cost += float(ev.get("total_cost_usd") or 0.0)
                if ev.get("is_error"):
                    is_error = True
                    last_error = str(ev.get("result") or "api error")
            elif et == "system":
                st = ev.get("subtype")
                if st in ("timeout", "spawn_error"):
                    is_error = True
                    last_error = f"{st}: {ev.get('error') or ev.get('elapsed_s')}"
        if is_error:
            continue  # retry on any API error, even if partial chunks landed
        text = "".join(chunks)
        if not text.strip():
            last_error = "empty response"
            continue
        try:
            findings = _parse_findings(_extract_yaml(text))
            return findings, total_cost
        except yaml.YAMLError as e:
            last_error = str(e)
            continue
    return [], total_cost
