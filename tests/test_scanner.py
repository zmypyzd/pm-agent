"""Unit tests for pm_agent.scanner — LLM scan + bug_id stability."""
from __future__ import annotations

import asyncio

import pytest

from pm_agent.scanner import Finding, scan, _normalize_bug_id
from tests._fixtures.fake_repo import fake_repo
from tests._fixtures.claude_shim import claude_shim


def _scanner_events(findings_yaml: str):
    """Build stream-json events that look like a Scanner LLM response."""
    return [
        {"type": "system", "subtype": "init", "session_id": "s", "model": "shim"},
        {"type": "assistant",
         "message": {"content": [{"type": "text", "text": findings_yaml}]}},
        {"type": "result", "is_error": False, "total_cost_usd": 0.02, "duration_ms": 1},
    ]


def test_bug_id_stable_across_path_order():
    """sorted(paths) means order of paths in the input doesn't change bug_id."""
    a = _normalize_bug_id(["src/a.py", "src/b.py"], kind="bug")
    b = _normalize_bug_id(["src/b.py", "src/a.py"], kind="bug")
    assert a == b


def test_bug_id_changes_with_kind():
    """kind is part of the hash — bug vs tech-debt on same paths gets distinct id."""
    bug = _normalize_bug_id(["a.py"], kind="bug")
    debt = _normalize_bug_id(["a.py"], kind="tech-debt")
    assert bug != debt


def test_scan_happy_path():
    """Scanner shim returns 1 finding → scan() parses it correctly + returns cost."""
    yaml_block = """```yaml
findings:
  - title: Stderr deadlock in runner
    severity: High
    paths:
      - pm_agent/runner.py
    acceptance:
      - runner returns within 4s under heavy stderr
    evidence: |
      pm_agent/runner.py:81 — stderr=PIPE never drained
    kind: bug
```"""
    with fake_repo({"pm_agent/runner.py": "# stub"}) as repo:
        with claude_shim(events=_scanner_events(yaml_block)):
            findings, cost = asyncio.run(scan(repo))
    assert len(findings) == 1
    f = findings[0]
    assert f.severity == "High"
    assert f.kind == "bug"
    assert f.bug_id  # non-empty
    assert cost > 0


def test_scan_malformed_yaml_returns_empty():
    """malformed YAML after exhausting retries → empty findings list (no exception)."""
    bad = "this is not yaml at all"
    with fake_repo() as repo:
        with claude_shim(events=_scanner_events(bad)):
            findings, _cost = asyncio.run(scan(repo, max_retries=0))
    assert findings == []
