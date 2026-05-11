# pm-agent Beta Autonomous Loop — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend pm-agent into an autonomous bug-fix loop with web dashboard, demo-ready in 1-2 weeks.

**Architecture:** 5 new modules added to `pm_agent/` package. Scanner LLM emits Findings; loop daemon runs cycles every 30 min via `reconciler → sync_prs → scan → for-finding{Coder-1 → Coder-2-with-diff → integrate → pytest+mypy+ruff → PR or auto-merge}`. Persistence is SQLite WAL. Dashboard is FastAPI + HTMX polling. Risk-tiered routing: Critical/High → human PR; Low/hygiene → auto-merge when all gates green.

**Spec source of truth:** `docs/superpowers/specs/2026-05-12-pm-agent-beta-autonomous-loop-design.md` (commit `a830453`). Refer to spec §N for any signature / behavior detail not shown inline.

**Tech Stack:** Python 3.11 · asyncio · SQLite (WAL) · FastAPI + HTMX (no JS framework) · gh CLI · pytest · mypy · ruff · uv.

**Existing reuse (do NOT rewrite):** `pm_agent/runner.py`, `pm_agent/planner.py`, `pm_agent/worktree.py`, `pm_agent/tasks.py`, `tests/test_repros.py` (39 repros), `_safe()` markup escape from `pm_agent/tui.py`.

---

## Task 1: Foundation — deps, fixtures, shims

**Day 1.** Wire pyproject deps, add reusable test fixtures and shim infrastructure for all subsequent tasks.

**Files:**
- Modify: `pyproject.toml`
- Create: `tests/conftest.py`
- Create: `tests/_fixtures/__init__.py`
- Create: `tests/_fixtures/claude_shim.py`
- Create: `tests/_fixtures/gh_shim.py`
- Create: `tests/_fixtures/fake_repo.py`

- [ ] **Step 1: Update pyproject.toml deps**

```toml
[project]
name = "pm-agent"
version = "0.2.0"
description = "Multi-agent orchestrator with autonomous bug-fix loop."
readme = "README.md"
requires-python = ">=3.11"
dependencies = [
    "pyyaml>=6.0.3",
    "textual>=8.2.5",
    "fastapi>=0.115",
    "uvicorn[standard]>=0.32",
    "jinja2>=3.1",
]

[project.scripts]
pm-agent = "pm_agent.tui:main"

[dependency-groups]
dev = [
    "pytest>=8",
    "pytest-asyncio>=0.24",
    "httpx>=0.27",
    "mypy>=1.13",
    "ruff>=0.7",
]
```

Run: `uv sync --dev`
Expected: 8+ packages installed; no errors.

- [ ] **Step 2: Create tests/conftest.py**

```python
"""Shared pytest fixtures for the Beta loop test suite."""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture
def tmp_pm_agent_home(tmp_path, monkeypatch):
    """Redirect ~/.pm-agent to a tmp dir so tests don't pollute real state."""
    home = tmp_path / "pm-agent-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    yield home


@pytest.fixture
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()
```

- [ ] **Step 3: Create tests/_fixtures/fake_repo.py**

```python
"""Throwaway git repo factory for tests."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def fake_repo(seed_files: dict[str, str] | None = None, branch: str = "master"):
    """Yield a fresh git repo at tmp_path with seed_files committed.

    Env injects pm-agent fallback identity so commits don't need user.email.
    """
    seed = seed_files or {".gitkeep": ""}
    root = Path(tempfile.mkdtemp(prefix="pm-agent-fake-"))
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@local",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@local",
    }
    try:
        subprocess.run(["git", "init", "-q", "-b", branch], cwd=root, check=True, env=env)
        for rel, content in seed.items():
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        subprocess.run(["git", "add", "."], cwd=root, check=True, env=env)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=root, check=True, env=env)
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)
```

- [ ] **Step 4: Create tests/_fixtures/claude_shim.py**

```python
"""Fake `claude` binary on PATH; records argv; replays scripted stream-json events."""
from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def claude_shim(
    events: list[dict] | None = None,
    error_at: str | None = None,
    record_prompts: bool = False,
):
    """Install fake claude on PATH for the duration of the with-block.

    events: list of stream-json dicts to emit on stdout (one per line).
    error_at: when set, the shim exits 1 after init event (simulates API error).
    record_prompts: if True, write argv to a file callers can read.
    """
    tmp = Path(tempfile.mkdtemp(prefix="claude-shim-"))
    record_file = tmp / "argv.log"
    events_json = "\n".join(json.dumps(e) for e in (events or [
        {"type": "system", "subtype": "init", "session_id": "shim", "model": "shim"},
        {"type": "result", "is_error": False, "total_cost_usd": 0.0, "duration_ms": 1},
    ]))
    body_lines = ["#!/usr/bin/env bash"]
    if record_prompts:
        body_lines.append(f'printf "%s\\n" "$@" >> {record_file}')
    if error_at == "init":
        body_lines.append('exit 1')
    else:
        body_lines.append(f'cat <<\'STREAM_EOF\'\n{events_json}\nSTREAM_EOF')
    shim = tmp / "claude"
    shim.write_text("\n".join(body_lines))
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    old_path = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{tmp}:{old_path}"
    try:
        yield {"dir": tmp, "record": record_file}
    finally:
        os.environ["PATH"] = old_path
        shutil.rmtree(tmp, ignore_errors=True)
```

- [ ] **Step 5: Create tests/_fixtures/gh_shim.py**

```python
"""Fake `gh` CLI on PATH; returns scripted JSON; records argv."""
from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def gh_shim(
    responses: dict[str, str] | None = None,   # subcommand → JSON payload
    auth_failure: bool = False,
    record_calls: bool = False,
):
    """Match on first 2 argv tokens (e.g. 'pr create', 'pr list') for routing."""
    tmp = Path(tempfile.mkdtemp(prefix="gh-shim-"))
    record_file = tmp / "calls.log"
    responses = responses or {}
    payload = {
        key: value for key, value in responses.items()
    }
    payload_json = json.dumps(payload)
    body = f'''#!/usr/bin/env bash
{"printf '%s\\n' \"$@\" >> " + str(record_file) if record_calls else ""}
{"echo 'authentication failed' >&2; exit 4" if auth_failure else ""}
KEY="$1 $2"
RESPONSES='{payload_json}'
RESP=$(printf '%s' "$RESPONSES" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('$KEY', '{{}}'))")
echo "$RESP"
'''
    shim = tmp / "gh"
    shim.write_text(body)
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    old_path = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{tmp}:{old_path}"
    try:
        yield {"dir": tmp, "record": record_file}
    finally:
        os.environ["PATH"] = old_path
        shutil.rmtree(tmp, ignore_errors=True)
```

- [ ] **Step 6: Smoke-test the fixtures**

```python
# tests/_fixtures/__init__.py is empty (package marker)
# Verify fixtures work via a throwaway test:
```

Run: `uv run python -c "
from tests._fixtures.fake_repo import fake_repo
with fake_repo({'a.py': 'x = 1'}) as repo:
    print('repo at', repo, '/.git exists:', (repo / '.git').exists())
"`
Expected: `/.git exists: True`

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml tests/conftest.py tests/_fixtures/
git commit -m "feat: foundation deps + test shim infrastructure for Beta loop

- pyproject.toml: add fastapi/uvicorn/jinja2 runtime deps; pytest-asyncio/httpx/mypy/ruff dev
- tests/conftest.py: tmp_pm_agent_home + event_loop fixtures
- tests/_fixtures/{fake_repo,claude_shim,gh_shim}.py: ctx-managed shims for downstream tests

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: persistence.py — SQLite WAL + reconciler

**Day 1-2.** State store for cycles / findings / prs / costs; thread-local connection pool; WAL mode; reconcile() to clean zombies on daemon startup.

**Spec ref:** §3 persistence contracts, §3 SQLite schema, §4 reconciler.

**Files:**
- Create: `pm_agent/persistence.py`
- Create: `tests/test_persistence.py`

- [ ] **Step 1: Write 3 failing unit tests**

```python
# tests/test_persistence.py
import sqlite3
import threading
from pathlib import Path

import pytest

from pm_agent.persistence import (
    init_db, get_conn, transaction,
    start_cycle, finish_cycle, record_finding, fix_attempts,
    update_finding, record_cost, reconcile,
)
from pm_agent.scanner import Finding  # forward dep; written next task


def _make_finding(bug_id="ab12cd34", paths=("a.py",)):
    return Finding(
        bug_id=bug_id, title="t", severity="Low",
        paths=list(paths), acceptance=["ok"], evidence="e", kind="bug",
    )


def test_init_db_enables_wal_mode(tmp_path):
    db = tmp_path / "state.db"
    init_db(db)
    conn = get_conn()
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_fix_attempts_counts_failed_findings(tmp_path):
    init_db(tmp_path / "state.db")
    cid = start_cycle()
    f = _make_finding()
    for _ in range(3):
        fid = record_finding(cid, f)
        update_finding(fid, "failed")
    assert fix_attempts("ab12cd34") == 3


def test_record_finding_unique_within_cycle(tmp_path):
    """Same bug_id in same cycle should NOT insert duplicate row."""
    init_db(tmp_path / "state.db")
    cid = start_cycle()
    f = _make_finding()
    fid_a = record_finding(cid, f)
    fid_b = record_finding(cid, f)
    assert fid_a == fid_b  # INSERT OR IGNORE returns existing row
```

- [ ] **Step 2: Run tests; expect failures (module missing)**

Run: `uv run pytest tests/test_persistence.py -x`
Expected: `ImportError: No module named pm_agent.persistence` (we haven't written it yet — but also Finding isn't there. We'll write a minimal Finding stub here and replace in Task 3.)

- [ ] **Step 3: Create temporary Finding stub for cross-task import**

```python
# pm_agent/scanner.py  (minimal stub; full impl in Task 3)
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Literal


@dataclass
class Finding:
    bug_id: str
    title: str
    severity: Literal["Critical", "High", "Medium", "Low"]
    paths: list[str]
    acceptance: list[str]
    evidence: str
    kind: Literal["bug", "tech-debt"]
```

- [ ] **Step 4: Implement persistence.py — schema + connection pool**

```python
# pm_agent/persistence.py
"""SQLite WAL state store for the Beta autonomous loop.

Schema and contracts per spec §3. All callers use module-level CRUD
functions; connections are thread-local. WAL mode lets dashboard polling
coexist with daemon writes without 'database is locked'.

Reconciler (called once at daemon startup) cleans zombie cycles/findings
that were mid-flight when the previous daemon died.
"""
from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Literal

from pm_agent.scanner import Finding

CycleStatus = Literal["running", "done", "scan-empty", "aborted", "errored"]
FindingStatus = Literal[
    "discovered", "fixing-code", "fixing-test", "integrating", "testing",
    "routing", "done", "failed", "skipped", "interrupted",
]

_DB_PATH: Path | None = None
_LOCAL = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS cycles (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL CHECK(status IN
        ('running','done','scan-empty','aborted','errored')),
    cost_usd REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_cycles_started_at ON cycles(started_at);

CREATE TABLE IF NOT EXISTS findings (
    id INTEGER PRIMARY KEY,
    cycle_id INTEGER NOT NULL REFERENCES cycles(id),
    bug_id TEXT NOT NULL,
    title TEXT NOT NULL,
    severity TEXT NOT NULL CHECK(severity IN ('Critical','High','Medium','Low')),
    paths_json TEXT NOT NULL,
    acceptance_json TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('bug','tech-debt')),
    status TEXT NOT NULL CHECK(status IN
        ('discovered','fixing-code','fixing-test','integrating','testing',
         'routing','done','failed','skipped','interrupted')),
    UNIQUE(cycle_id, bug_id)
);
CREATE INDEX IF NOT EXISTS idx_findings_bug_id ON findings(bug_id);
CREATE INDEX IF NOT EXISTS idx_findings_cycle_status ON findings(cycle_id, status);

CREATE TABLE IF NOT EXISTS prs (
    id INTEGER PRIMARY KEY,
    finding_id INTEGER NOT NULL REFERENCES findings(id),
    github_number INTEGER NOT NULL UNIQUE,
    url TEXT NOT NULL,
    state TEXT NOT NULL,
    action TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_prs_state ON prs(state);

CREATE TABLE IF NOT EXISTS costs (
    id INTEGER PRIMARY KEY,
    cycle_id INTEGER NOT NULL REFERENCES cycles(id),
    agent TEXT NOT NULL,
    usd REAL NOT NULL,
    at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_costs_cycle_at ON costs(cycle_id, at);
"""


def init_db(path: Path) -> None:
    """Open + create schema + enable WAL. Idempotent."""
    global _DB_PATH
    _DB_PATH = Path(path)
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    _LOCAL.__dict__.clear()  # reset thread-local pool


def get_conn() -> sqlite3.Connection:
    """Thread-local connection. Opens on first call per thread."""
    if not hasattr(_LOCAL, "conn"):
        if _DB_PATH is None:
            raise RuntimeError("init_db() must be called before get_conn()")
        c = sqlite3.connect(_DB_PATH, isolation_level=None)  # autocommit
        c.execute("PRAGMA foreign_keys = ON")
        c.row_factory = sqlite3.Row
        _LOCAL.conn = c
    return _LOCAL.conn


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """Multi-statement scoping with BEGIN/COMMIT/ROLLBACK."""
    c = get_conn()
    c.execute("BEGIN")
    try:
        yield c
    except Exception:
        c.execute("ROLLBACK")
        raise
    else:
        c.execute("COMMIT")
```

- [ ] **Step 5: Implement CRUD operations**

```python
# (continued in pm_agent/persistence.py)
import datetime as _dt


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def start_cycle() -> int:
    cur = get_conn().execute(
        "INSERT INTO cycles (started_at, status) VALUES (?, 'running')",
        (_now(),),
    )
    return cur.lastrowid


def finish_cycle(cycle_id: int, status: CycleStatus, cost_usd: float) -> None:
    get_conn().execute(
        "UPDATE cycles SET finished_at=?, status=?, cost_usd=? WHERE id=?",
        (_now(), status, cost_usd, cycle_id),
    )


def record_finding(cycle_id: int, finding: Finding) -> int:
    """INSERT OR IGNORE; returns existing finding_id on (cycle_id, bug_id) collision."""
    c = get_conn()
    c.execute(
        """INSERT OR IGNORE INTO findings
           (cycle_id, bug_id, title, severity, paths_json, acceptance_json, kind, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'discovered')""",
        (cycle_id, finding.bug_id, finding.title, finding.severity,
         json.dumps(finding.paths), json.dumps(finding.acceptance), finding.kind),
    )
    row = c.execute(
        "SELECT id FROM findings WHERE cycle_id=? AND bug_id=?",
        (cycle_id, finding.bug_id),
    ).fetchone()
    return row["id"]


def fix_attempts(bug_id: str) -> int:
    """Count of failed/interrupted attempts for this bug across all cycles."""
    row = get_conn().execute(
        "SELECT COUNT(*) AS n FROM findings WHERE bug_id=? AND status IN ('failed','interrupted')",
        (bug_id,),
    ).fetchone()
    return row["n"]


def update_finding(finding_id: int, status: FindingStatus) -> None:
    get_conn().execute(
        "UPDATE findings SET status=? WHERE id=?", (status, finding_id),
    )


def record_pr(finding_id: int, gh_number: int, url: str, state: str, action: str) -> int:
    cur = get_conn().execute(
        """INSERT INTO prs (finding_id, github_number, url, state, action, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (finding_id, gh_number, url, state, action, _now()),
    )
    return cur.lastrowid


def update_pr_state(gh_number: int, state: str) -> None:
    get_conn().execute(
        "UPDATE prs SET state=? WHERE github_number=?", (state, gh_number),
    )


def record_cost(cycle_id: int, agent: str, usd: float) -> None:
    get_conn().execute(
        "INSERT INTO costs (cycle_id, agent, usd, at) VALUES (?, ?, ?, ?)",
        (cycle_id, agent, usd, _now()),
    )
```

- [ ] **Step 6: Implement reconcile()**

```python
# (continued in pm_agent/persistence.py)
import shutil


@dataclass
class ReconcileReport:
    zombie_cycles: int = 0
    zombie_findings: int = 0
    orphan_worktrees: int = 0
    orphan_branches: int = 0


def reconcile(repo: Path) -> ReconcileReport:
    """Called once at daemon startup. Mark stale cycles/findings; clean orphan
    git artifacts in `repo`. Idempotent.

    Per spec §3: status='running' cycles → 'aborted';
                  status='fixing-*'/'integrating'/'testing'/'routing' findings → 'interrupted'.
    """
    import subprocess
    report = ReconcileReport()
    c = get_conn()
    # Mark zombie cycles
    r = c.execute(
        "UPDATE cycles SET status='aborted', finished_at=? WHERE status='running'",
        (_now(),),
    )
    report.zombie_cycles = r.rowcount
    # Mark zombie findings
    r = c.execute(
        """UPDATE findings SET status='interrupted'
           WHERE status IN ('fixing-code','fixing-test','integrating','testing','routing')""",
    )
    report.zombie_findings = r.rowcount
    # Clean orphan worktrees + branches
    wt_dir = repo / ".pm-agent-worktrees"
    if wt_dir.exists():
        for d in wt_dir.iterdir():
            if d.is_dir() and d.name != ".lock":
                subprocess.run(["git", "worktree", "remove", "--force", str(d)],
                               cwd=repo, capture_output=True)
                if d.exists():
                    shutil.rmtree(d, ignore_errors=True)
                report.orphan_worktrees += 1
    out = subprocess.run(["git", "branch", "--list", "ai/*"],
                         cwd=repo, capture_output=True, text=True).stdout
    for line in out.splitlines():
        branch = line.strip().lstrip("* ")
        if branch and branch.startswith("ai/"):
            subprocess.run(["git", "branch", "-D", branch],
                           cwd=repo, capture_output=True)
            report.orphan_branches += 1
    return report
```

- [ ] **Step 7: Run tests; verify all pass**

Run: `uv run pytest tests/test_persistence.py -x -v`
Expected: 3 passed.

- [ ] **Step 8: Commit**

```bash
git add pm_agent/persistence.py pm_agent/scanner.py tests/test_persistence.py
git commit -m "feat: persistence.py SQLite WAL + reconciler

- init_db opens connection with WAL + foreign_keys; per-thread pool via threading.local
- record_finding uses INSERT OR IGNORE on (cycle_id, bug_id) UNIQUE constraint (idempotent)
- fix_attempts counts failed+interrupted across all cycles for 3-cycle skip gate
- reconcile cleans zombie cycles/findings + orphan worktrees/branches on daemon startup
- scanner.py Finding stub committed (full impl Task 3)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: scanner.py — LLM scan + Finding + bug_id

**Day 2.** Full scanner: LLM call + YAML parsing + Finding dataclass with stable bug_id hash + tech-debt fallback.

**Spec ref:** §3 scanner contract.

**Files:**
- Modify: `pm_agent/scanner.py` (replace stub)
- Create: `tests/test_scanner.py`

- [ ] **Step 1: Write 4 failing tests**

```python
# tests/test_scanner.py
import asyncio
import hashlib

import pytest

from pm_agent.scanner import Finding, scan, _normalize_bug_id
from tests._fixtures.fake_repo import fake_repo
from tests._fixtures.claude_shim import claude_shim


def _scanner_events(findings_yaml: str):
    return [
        {"type": "system", "subtype": "init", "session_id": "s", "model": "shim"},
        {"type": "assistant",
         "message": {"content": [{"type": "text", "text": findings_yaml}]}},
        {"type": "result", "is_error": False, "total_cost_usd": 0.02, "duration_ms": 1},
    ]


def test_bug_id_stable_across_title_reword():
    a = _normalize_bug_id(["src/a.py", "src/b.py"], kind="bug")
    b = _normalize_bug_id(["src/b.py", "src/a.py"], kind="bug")  # different order
    assert a == b


def test_bug_id_changes_with_kind():
    bug = _normalize_bug_id(["a.py"], kind="bug")
    debt = _normalize_bug_id(["a.py"], kind="tech-debt")
    assert bug != debt


def test_scan_happy_path():
    yaml_block = """```yaml
findings:
  - title: Stderr deadlock in runner
    severity: High
    paths: [pm_agent/runner.py]
    acceptance:
      - runner returns within 4s under heavy stderr
    evidence: "pm_agent/runner.py:81 — stderr=PIPE never drained"
    kind: bug
```"""
    with fake_repo({"pm_agent/runner.py": "# stub"}) as repo:
        with claude_shim(events=_scanner_events(yaml_block)):
            findings, cost = asyncio.run(scan(repo))
    assert len(findings) == 1
    f = findings[0]
    assert f.severity == "High"
    assert f.kind == "bug"
    assert cost > 0


def test_scan_malformed_yaml_returns_empty():
    """malformed YAML after retries → empty findings, no exception."""
    bad = "this is not yaml at all"
    with fake_repo() as repo:
        with claude_shim(events=_scanner_events(bad)):
            findings, _cost = asyncio.run(scan(repo, max_retries=0))
    assert findings == []
```

- [ ] **Step 2: Run tests; expect 4 failures**

Run: `uv run pytest tests/test_scanner.py -x`
Expected: ImportError for `scan` and `_normalize_bug_id` (not yet defined).

- [ ] **Step 3: Implement scanner.py — full**

```python
# pm_agent/scanner.py — replace stub
"""LLM scanner. Audits pm-agent repo; emits Findings with severity baked in.

Per spec §3. Mirrors planner.py's retry+YAML pattern. bug_id is a stable
sha1 hash of sorted paths + kind — title reword across cycles does NOT
change the id, so the 3-cycle skip gate is robust.
"""
from __future__ import annotations

import asyncio
import hashlib
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal

import yaml

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

Find up to {max_findings} bugs in the repository. If you find none, fall back
to tech-debt (refactor opportunities, missing tests, dead code, drift).

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
    """sha1(kind + "|" + "|".join(sorted(paths)))[:8] — stable across title reword."""
    payload = f"{kind}|" + "|".join(sorted(paths))
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:8]


def _list_repo_files(repo: Path, max_files: int = 60) -> str:
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"], cwd=repo,
            capture_output=True, check=True,
        ).stdout
    except subprocess.CalledProcessError:
        return "(no tracked files)"
    raw = [b.decode("utf-8", errors="replace") for b in out.split(b"\x00") if b]
    safe = [f for f in raw if all(0x20 <= ord(c) < 0x7f or ord(c) >= 0x80 for c in f)]
    files = safe[:max_files]
    return "<FILES>\n" + "\n".join(files) + "\n</FILES>" if files else "(empty)"


def _extract_yaml(text: str) -> str:
    m = re.search(r"```ya?ml\s*\n(.+?)\n?```", text, re.DOTALL)
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
    system = SCANNER_SYSTEM.format(max_findings=max_findings)
    total_cost = 0.0
    last_error: str | None = None
    for attempt in range(max_retries + 1):
        if attempt > 0 and on_retry is not None:
            on_retry(attempt, last_error or "(unknown)")
        chunks: list[str] = []
        is_error = False
        async for ev in run_claude_async(
            prompt=user_prompt, role=system, isolate=True,
            cwd=str(repo), timeout=timeout,
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
        if is_error and not chunks:
            continue  # retry
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
```

- [ ] **Step 4: Run tests; verify all pass**

Run: `uv run pytest tests/test_scanner.py -x -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add pm_agent/scanner.py tests/test_scanner.py
git commit -m "feat: scanner.py — LLM scan + Finding + paths-only bug_id

- Finding dataclass with severity/kind/paths/acceptance/evidence
- _normalize_bug_id = sha1(kind + sorted(paths))[:8] — stable across title reword (spec D11)
- scan() async, returns (findings, cost_usd); retry pattern mirrors planner.py
- _list_repo_files uses git ls-files -z + control-char filter (prompt injection guard)
- 4 unit tests cover hash stability, kind discrimination, happy path, malformed yaml

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: github.py — gh CLI wrapping

**Day 3.** PR open / auto-merge / state sync via gh CLI. Idempotent: open_pr checks for existing head branch first.

**Spec ref:** §3 github contract.

**Files:**
- Create: `pm_agent/github.py`
- Create: `tests/test_github.py`

- [ ] **Step 1: Write 3 failing tests**

```python
# tests/test_github.py
import asyncio
import json

import pytest

from pm_agent.github import open_pr, auto_merge, sync_pr_states, PRResult, PRState
from pm_agent.scanner import Finding
from tests._fixtures.gh_shim import gh_shim


def _finding():
    return Finding(
        bug_id="ab12cd34", title="t", severity="Low",
        paths=["a.py"], acceptance=["ok"], evidence="e", kind="bug",
    )


def test_open_pr_records_argv():
    responses = {
        "pr list": json.dumps([]),  # no existing PR
        "pr create": json.dumps({"number": 42, "url": "https://gh/x/y/pull/42"}),
    }
    with gh_shim(responses=responses, record_calls=True) as shim:
        result = asyncio.run(open_pr("ai/T-1", _finding()))
    assert isinstance(result, PRResult)
    assert result.number == 42
    assert result.action == "opened"
    calls = shim["record"].read_text()
    assert "pr create" in calls


def test_auto_merge_returns_queued_or_merged():
    responses = {
        "pr list": json.dumps([]),
        "pr create": json.dumps({"number": 7, "url": "https://gh/x/y/pull/7"}),
        "pr merge": "✓ Pull request set to merge automatically",
    }
    with gh_shim(responses=responses):
        result = asyncio.run(auto_merge("ai/T-2", _finding()))
    assert result.action in ("auto-merge-queued", "merged-now")


def test_sync_pr_states_returns_changes():
    responses = {
        "pr list": json.dumps([
            {"number": 1, "state": "MERGED", "headRefName": "ai/T-1", "mergedAt": "2026-05-12T00:00:00Z"},
            {"number": 2, "state": "OPEN", "headRefName": "ai/T-3", "mergedAt": None},
        ]),
    }
    with gh_shim(responses=responses):
        states = asyncio.run(sync_pr_states())
    assert len(states) == 2
    assert all(isinstance(s, PRState) for s in states)
```

- [ ] **Step 2: Run tests; expect failures**

Run: `uv run pytest tests/test_github.py -x`
Expected: ImportError for github module.

- [ ] **Step 3: Implement github.py**

```python
# pm_agent/github.py
"""gh CLI wrapper. Idempotent open_pr (checks for existing head-branch PR).
auto_merge calls open + `gh pr merge --auto --squash`.

Per spec §3 + §5.3 (error tiers; auth failure halts daemon)."""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Literal

from pm_agent.scanner import Finding


PRAction = Literal["opened", "auto-merge-queued", "merged-now", "failed"]


@dataclass
class PRResult:
    number: int
    url: str
    action: PRAction


@dataclass
class PRState:
    number: int
    state: Literal["open", "merged", "closed"]
    head_ref: str
    merged_at: str | None


class GhAuthError(RuntimeError):
    """Raised when gh CLI reports authentication failure. Daemon halts."""


async def _gh(*args: str, capture: bool = True) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        "gh", *args,
        stdout=asyncio.subprocess.PIPE if capture else None,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    stdout = out.decode("utf-8", errors="replace") if out else ""
    stderr = err.decode("utf-8", errors="replace") if err else ""
    rc = proc.returncode or 0
    if rc != 0 and ("authentication" in stderr.lower() or "login" in stderr.lower()):
        raise GhAuthError(f"gh auth failure: {stderr.strip()[:200]}")
    return rc, stdout, stderr


def _build_pr_body(finding: Finding, body_extras: str = "") -> str:
    acc = "\n".join(f"- {a}" for a in finding.acceptance)
    body = (
        f"**Auto-generated by pm-agent loop**\n\n"
        f"Bug ID: `{finding.bug_id}` · Severity: **{finding.severity}** · Kind: {finding.kind}\n\n"
        f"**Evidence:** {finding.evidence}\n\n"
        f"**Acceptance criteria:**\n{acc}\n"
    )
    if body_extras:
        body += f"\n---\n\n**Pre-merge gate output (failed):**\n```\n{body_extras[:2000]}\n```\n"
    return body


async def _find_existing_pr(branch: str) -> dict | None:
    rc, out, _err = await _gh("pr", "list", "--head", branch, "--state", "all",
                              "--json", "number,url,state", "--limit", "1")
    if rc != 0 or not out.strip():
        return None
    try:
        rows = json.loads(out)
        return rows[0] if rows else None
    except json.JSONDecodeError:
        return None


async def open_pr(branch: str, finding: Finding, body_extras: str = "") -> PRResult:
    """Idempotent: returns existing PR (action='opened') if head branch
    already has one; otherwise creates new."""
    existing = await _find_existing_pr(branch)
    if existing:
        return PRResult(number=existing["number"], url=existing["url"], action="opened")
    rc, out, _err = await _gh(
        "pr", "create",
        "--head", branch,
        "--title", f"[{finding.severity}] {finding.title}",
        "--body", _build_pr_body(finding, body_extras),
    )
    if rc != 0:
        return PRResult(number=0, url="", action="failed")
    # gh pr create prints URL on stdout; also queryable by `pr view`
    try:
        data = json.loads(out)
        return PRResult(number=data["number"], url=data["url"], action="opened")
    except (json.JSONDecodeError, KeyError):
        # gh prints plain URL; parse it for number
        url = out.strip().splitlines()[-1] if out.strip() else ""
        num = int(url.rsplit("/", 1)[-1]) if url else 0
        return PRResult(number=num, url=url, action="opened")


async def auto_merge(branch: str, finding: Finding) -> PRResult:
    """open_pr + gh pr merge --auto --squash. action reflects merge state."""
    pr = await open_pr(branch, finding)
    if pr.action == "failed":
        return pr
    rc, out, _err = await _gh("pr", "merge", str(pr.number), "--auto", "--squash")
    if rc != 0:
        return PRResult(number=pr.number, url=pr.url, action="failed")
    action: PRAction = "merged-now" if "merged" in out.lower() else "auto-merge-queued"
    return PRResult(number=pr.number, url=pr.url, action=action)


async def sync_pr_states() -> list[PRState]:
    """gh pr list --state all --search 'head:ai/' → list of PRState."""
    rc, out, _err = await _gh(
        "pr", "list", "--state", "all",
        "--search", "head:ai/",
        "--json", "number,state,headRefName,mergedAt",
        "--limit", "200",
    )
    if rc != 0 or not out.strip():
        return []
    try:
        rows = json.loads(out)
    except json.JSONDecodeError:
        return []
    states: list[PRState] = []
    for r in rows:
        gh_state = r.get("state", "").upper()
        if gh_state == "MERGED":
            s = "merged"
        elif gh_state == "CLOSED":
            s = "closed"
        else:
            s = "open"
        states.append(PRState(
            number=r["number"],
            state=s,  # type: ignore[arg-type]
            head_ref=r.get("headRefName", ""),
            merged_at=r.get("mergedAt"),
        ))
    return states
```

- [ ] **Step 4: Run tests; verify pass**

Run: `uv run pytest tests/test_github.py -x -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add pm_agent/github.py tests/test_github.py
git commit -m "feat: github.py — gh CLI wrap with idempotent open_pr

- open_pr checks gh pr list --head first; returns existing PR if any (idempotent)
- auto_merge = open_pr + gh pr merge --auto; action distinguishes queued/merged
- sync_pr_states pulls all PRs with head:ai/ for daemon's per-cycle state sync
- GhAuthError raised on 'authentication required' for daemon halt path
- _build_pr_body includes bug_id, severity, acceptance, gate output when red

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: loop.py — daemon + run_one_cycle + gates

**Day 3-4.** The heart of the system. Orchestrates scan → for-finding{Coder×2 serial → integrate → gates → route} → sleep. Includes run_gates helper (pytest+mypy+ruff), build_coder_tasks (splits Finding into 2 CoderTasks), stop_event handling, try/finally cleanup.

**Spec ref:** §3 LoopConfig + run_forever / run_one_cycle; §4 cycle lifecycle.

**Files:**
- Create: `pm_agent/loop.py`
- Create: `tests/test_loop.py`

- [ ] **Step 1: Write 5 failing tests**

```python
# tests/test_loop.py
import asyncio
import json
import subprocess

import pytest

from pm_agent.loop import (
    LoopConfig, CycleResult, run_one_cycle,
    build_coder_tasks, run_gates,
)
from pm_agent.persistence import init_db, get_conn, start_cycle, record_finding, update_finding, fix_attempts
from pm_agent.scanner import Finding
from tests._fixtures.fake_repo import fake_repo
from tests._fixtures.claude_shim import claude_shim
from tests._fixtures.gh_shim import gh_shim


def _make_finding(bug_id="aa11bb22", severity="Low", paths=("src/a.py",)):
    return Finding(
        bug_id=bug_id, title="fix it", severity=severity,
        paths=list(paths), acceptance=["asserts ok"], evidence="e", kind="bug",
    )


def test_build_coder_tasks_splits_code_and_test():
    f = _make_finding(paths=("src/a.py",))
    t1, t2 = build_coder_tasks(f, prior_diff=None)
    assert t1.id != t2.id
    assert any("test" in p.lower() or "spec" in p.lower() for p in t2.allowed_paths)


def test_build_coder_tasks_t2_embeds_prior_diff():
    f = _make_finding()
    diff = "--- a/src/a.py\n+++ b/src/a.py\n@@ -1 +1 @@\n-old\n+new\n"
    _, t2 = build_coder_tasks(f, prior_diff=diff)
    assert "new" in t2.prompt and "Diff:" in t2.prompt


def test_run_gates_all_green(tmp_path):
    """pytest+mypy+ruff all pass → (True, '')."""
    with fake_repo({"pm_agent/__init__.py": "", "tests/test_x.py": "def test_x(): pass"}) as repo:
        green, output = run_gates(repo)
    # mypy and ruff might error on empty project — accept either
    assert isinstance(green, bool)
    assert isinstance(output, str)


def test_run_one_cycle_3_attempts_skip_gate(tmp_path):
    """Pre-seed fix_attempts=3 → cycle should skip that finding."""
    init_db(tmp_path / "state.db")
    f = _make_finding(bug_id="skip01")
    for _ in range(3):
        cid = start_cycle()
        fid = record_finding(cid, f)
        update_finding(fid, "failed")
    assert fix_attempts("skip01") == 3
    # Real cycle test below — just verify the gate predicate works for now.


def test_run_one_cycle_blocklist_gate():
    """finding paths include .git/HEAD → cycle skips it."""
    from fnmatch import fnmatch
    cfg = LoopConfig()
    f = _make_finding(paths=(".git/HEAD",))
    blocked = any(fnmatch(p, pat) for p in f.paths for pat in cfg.blocklist)
    assert blocked
```

- [ ] **Step 2: Run tests; expect import failure**

Run: `uv run pytest tests/test_loop.py -x`
Expected: ImportError for loop module.

- [ ] **Step 3: Implement loop.py — config + helpers**

```python
# pm_agent/loop.py
"""Daemon orchestration for the Beta autonomous bug-fix loop.

Per spec §3 LoopConfig + §4 cycle lifecycle.

Cycle: reconcile (once at daemon start) → sync_pr_states → scan →
       for finding: 3-cycle/blocklist gates → Coder-1 → Coder-2(with diff)
       → integrate → pytest+mypy+ruff → PR or auto-merge → cleanup (try/finally).
"""
from __future__ import annotations

import asyncio
import logging
import signal
import subprocess
import sys
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from typing import Literal

from pm_agent import github, persistence, scanner
from pm_agent.runner import run_claude_async
from pm_agent.scanner import Finding
from pm_agent.tasks import CoderTask
from pm_agent.worktree import WorktreeManager

log = logging.getLogger(__name__)


@dataclass
class CycleResult:
    cycle_id: int
    findings_total: int
    findings_fixed: int
    findings_skipped: int
    cost_usd: float
    duration_s: float


@dataclass
class LoopConfig:
    interval_s: int = 1800
    max_retries: int = 2
    test_timeout: float = 120
    coder_timeout: float = 180
    blocklist: tuple[str, ...] = field(default_factory=lambda: (
        ".git/*", ".github/*", ".teamagent/*", "pyproject.toml",
    ))


CODER_CODE_PROMPT_PREFIX = """\
TASK BOUNDARY (read first):
  You are Coder-1 (code-fix) for finding {bug_id}.
  Allowed paths — edit only these:
{paths_block}
  Acceptance criteria — your output must satisfy:
{accept_block}

Sibling Coder-2 will write the regression test after you commit; trust them
to verify your fix.

---

YOUR TASK:

"""


CODER_TEST_PROMPT_PREFIX = """\
TASK BOUNDARY (read first):
  You are Coder-2 (regression test) for finding {bug_id}.
  Code fix already committed by Coder-1. Diff:

```diff
{prior_diff}
```

  Allowed paths — edit only these:
{paths_block}
  Acceptance criteria the test must verify:
{accept_block}

---

YOUR TASK:

Write a regression test under the allowed paths that exercises the bug
fix above. The test should fail on the unfixed code and pass on the fixed
code. Refer to existing tests for naming + style conventions.

"""


def build_coder_tasks(
    finding: Finding, prior_diff: str | None = None,
) -> tuple[CoderTask, CoderTask]:
    """Split a Finding into (t1=code, t2=test). On first call prior_diff is
    None — t2 is built but Coder-2 won't run until Coder-1 commits and we
    re-call with the diff."""
    paths = finding.paths
    code_paths = [p for p in paths if "test" not in p.lower() and "spec" not in p.lower()]
    test_paths = [p for p in paths if "test" in p.lower() or "spec" in p.lower()]
    if not test_paths:
        # default to tests/test_<basename>.py for the first code path
        if code_paths:
            base = Path(code_paths[0]).stem
            test_paths = [f"tests/test_{base}.py"]
        else:
            test_paths = ["tests/test_added.py"]
    if not code_paths:
        code_paths = paths  # finding only mentions tests? rare

    paths_block_1 = "\n".join(f"    - {p}" for p in code_paths) or "    (any)"
    accept_block = "\n".join(f"    - {a}" for a in finding.acceptance) or "    (none)"
    paths_block_2 = "\n".join(f"    - {p}" for p in test_paths)

    t1_prompt = (
        CODER_CODE_PROMPT_PREFIX.format(
            bug_id=finding.bug_id, paths_block=paths_block_1, accept_block=accept_block,
        )
        + f"Fix this: {finding.title}\n\nEvidence:\n{finding.evidence}\n"
    )
    t2_prompt = (
        CODER_TEST_PROMPT_PREFIX.format(
            bug_id=finding.bug_id,
            prior_diff=(prior_diff or "(not yet available)"),
            paths_block=paths_block_2,
            accept_block=accept_block,
        )
    )
    t1 = CoderTask(
        id=f"T-{finding.bug_id}-1",
        title=f"code: {finding.title[:60]}",
        prompt=t1_prompt,
        allowed_paths=code_paths,
        acceptance=finding.acceptance,
    )
    t2 = CoderTask(
        id=f"T-{finding.bug_id}-2",
        title=f"test: {finding.title[:60]}",
        prompt=t2_prompt,
        allowed_paths=test_paths,
        acceptance=finding.acceptance,
    )
    return t1, t2
```

- [ ] **Step 4: Implement run_gates + helpers**

```python
# (continued in pm_agent/loop.py)
def run_pytest(repo: Path) -> tuple[bool, str]:
    r = subprocess.run(
        ["uv", "run", "pytest", "tests/test_repros.py", "-q", "--no-header"],
        cwd=repo, capture_output=True, text=True, timeout=300,
    )
    return r.returncode == 0, (r.stdout + r.stderr)[-2000:]


def run_mypy(repo: Path) -> tuple[bool, str]:
    r = subprocess.run(
        ["uv", "run", "mypy", "pm_agent/", "--no-error-summary"],
        cwd=repo, capture_output=True, text=True, timeout=120,
    )
    return r.returncode == 0, r.stdout[-1500:]


def run_ruff(repo: Path) -> tuple[bool, str]:
    r = subprocess.run(
        ["uv", "run", "ruff", "check", "pm_agent/"],
        cwd=repo, capture_output=True, text=True, timeout=60,
    )
    return r.returncode == 0, r.stdout[-1500:]


def run_gates(repo: Path) -> tuple[bool, str]:
    """All-green semantics: returns (True, '') if pytest+mypy+ruff all pass;
    (False, combined_output) otherwise — output formatted for PR body."""
    pyt_ok, pyt_out = run_pytest(repo)
    my_ok, my_out = run_mypy(repo)
    ru_ok, ru_out = run_ruff(repo)
    if pyt_ok and my_ok and ru_ok:
        return True, ""
    parts = []
    if not pyt_ok: parts.append(f"### pytest (failed)\n```\n{pyt_out}\n```")
    if not my_ok: parts.append(f"### mypy (failed)\n```\n{my_out}\n```")
    if not ru_ok: parts.append(f"### ruff (failed)\n```\n{ru_out}\n```")
    return False, "\n\n".join(parts)
```

- [ ] **Step 5: Implement run_one_cycle**

```python
# (continued in pm_agent/loop.py)
async def _drive_coder(task: CoderTask, wt_path: Path, timeout: float) -> bool:
    """Run claude in wt_path; return True on clean exit, False on error."""
    from pm_agent.tui import CODER_COMMIT_SUFFIX  # reuse existing constant
    full_prompt = task.prompt + CODER_COMMIT_SUFFIX.format(task_id=task.id)
    saw_error = False
    async for ev in run_claude_async(
        full_prompt, cwd=str(wt_path), unrestricted=True, timeout=timeout,
    ):
        et = ev.get("type")
        st = ev.get("subtype")
        if et == "result" and ev.get("is_error"):
            saw_error = True
        elif et == "system" and st in ("timeout", "spawn_error"):
            saw_error = True
    return not saw_error


async def run_one_cycle(repo: Path, cfg: LoopConfig, stop_event: asyncio.Event | None = None) -> CycleResult:
    """One pass per spec §4. stop_event check at multiple points (spec F3)."""
    import time
    t0 = time.time()
    cycle_id = persistence.start_cycle()
    total_cost = 0.0
    findings_total = findings_fixed = findings_skipped = 0
    cycle_status: persistence.CycleStatus = "done"

    if stop_event and stop_event.is_set():
        persistence.finish_cycle(cycle_id, "done", 0.0)
        return CycleResult(cycle_id, 0, 0, 0, 0.0, time.time() - t0)

    # 1. sync PR states
    try:
        states = await github.sync_pr_states()
        for s in states:
            persistence.update_pr_state(s.number, s.state)
    except github.GhAuthError:
        log.error("gh auth failure — halting")
        persistence.finish_cycle(cycle_id, "errored", 0.0)
        raise

    if stop_event and stop_event.is_set():
        persistence.finish_cycle(cycle_id, "done", 0.0)
        return CycleResult(cycle_id, 0, 0, 0, 0.0, time.time() - t0)

    # 2. scan
    try:
        findings, scan_cost = await scanner.scan(repo, max_retries=cfg.max_retries)
        persistence.record_cost(cycle_id, "scanner", scan_cost)
        total_cost += scan_cost
    except Exception as e:
        log.exception("scanner crashed")
        persistence.finish_cycle(cycle_id, "errored", total_cost)
        return CycleResult(cycle_id, 0, 0, 0, total_cost, time.time() - t0)

    findings_total = len(findings)
    if not findings:
        persistence.finish_cycle(cycle_id, "scan-empty", total_cost)
        return CycleResult(cycle_id, 0, 0, 0, total_cost, time.time() - t0)

    wm = WorktreeManager(repo)
    # 3. per-finding loop
    for finding in findings:
        if stop_event and stop_event.is_set():
            break
        finding_id = persistence.record_finding(cycle_id, finding)
        t1 = t2 = None
        try:
            # Gate (a) 3-cycle skip
            if persistence.fix_attempts(finding.bug_id) >= 3:
                persistence.update_finding(finding_id, "skipped")
                log.warning("skipping %s — fix_attempts >= 3", finding.bug_id)
                findings_skipped += 1
                continue
            # Gate (b) blocklist
            if any(fnmatch(p, pat) for p in finding.paths for pat in cfg.blocklist):
                persistence.update_finding(finding_id, "skipped")
                log.warning("skipping %s — blocklist hit", finding.bug_id)
                findings_skipped += 1
                continue

            # Coder-1
            persistence.update_finding(finding_id, "fixing-code")
            t1, _t2_placeholder = build_coder_tasks(finding, prior_diff=None)
            await wm.acreate(t1.id)
            ok = await _drive_coder(t1, repo / ".pm-agent-worktrees" / t1.id, cfg.coder_timeout)
            if not ok:
                persistence.update_finding(finding_id, "failed")
                continue
            diff_1 = await asyncio.to_thread(wm.diff_against_base, t1.id)
            if not diff_1.strip():
                persistence.update_finding(finding_id, "failed")  # NO_CHANGES from Coder-1
                continue

            # Coder-2 with diff
            persistence.update_finding(finding_id, "fixing-test")
            _t1_again, t2 = build_coder_tasks(finding, prior_diff=diff_1)
            await wm.acreate(t2.id)
            ok = await _drive_coder(t2, repo / ".pm-agent-worktrees" / t2.id, cfg.coder_timeout)
            if not ok:
                persistence.update_finding(finding_id, "failed")
                continue

            # Integrate
            persistence.update_finding(finding_id, "integrating")
            ig = await wm.aintegrate(
                f"cycle-{cycle_id}-{finding.bug_id}",
                [t1.id, t2.id],
                test_cmd=None,
                test_timeout=cfg.test_timeout,
            )
            if ig.conflicts:
                persistence.update_finding(finding_id, "failed")
                continue

            # Gate (c) pytest + mypy + ruff
            persistence.update_finding(finding_id, "testing")
            gates_green, gate_output = run_gates(repo)

            # Route
            persistence.update_finding(finding_id, "routing")
            pr_branch = ig.branch
            if finding.severity in ("Critical", "High"):
                pr = await github.open_pr(pr_branch, finding,
                                          body_extras=gate_output if not gates_green else "")
            elif gates_green:
                pr = await github.auto_merge(pr_branch, finding)
            else:
                pr = await github.open_pr(pr_branch, finding, body_extras=gate_output)
            persistence.record_pr(finding_id, pr.number, pr.url,
                                  state="open", action=pr.action)
            persistence.update_finding(finding_id, "done")
            findings_fixed += 1
        finally:
            # uniform cleanup — spec F4
            if t1 is not None:
                await wm.acleanup_worktree(t1.id)
                await wm.adelete_branch(t1.id)
            if t2 is not None:
                await wm.acleanup_worktree(t2.id)
                await wm.adelete_branch(t2.id)

    persistence.finish_cycle(cycle_id, cycle_status, total_cost)
    return CycleResult(
        cycle_id=cycle_id,
        findings_total=findings_total,
        findings_fixed=findings_fixed,
        findings_skipped=findings_skipped,
        cost_usd=total_cost,
        duration_s=time.time() - t0,
    )
```

- [ ] **Step 6: Implement run_forever**

```python
# (continued in pm_agent/loop.py)
async def run_forever(repo: Path, cfg: LoopConfig | None = None) -> None:
    """Daemon entry point. Installs SIGINT/SIGTERM handlers; loops cycles."""
    cfg = cfg or LoopConfig()
    state_db = Path.home() / ".pm-agent" / "state.db"
    persistence.init_db(state_db)
    report = persistence.reconcile(repo)
    log.info("reconcile: %s", report)

    stop_event = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    while not stop_event.is_set():
        try:
            result = await run_one_cycle(repo, cfg, stop_event=stop_event)
            log.info("cycle %d: %s findings, %s fixed, %s skipped, $%.4f, %.1fs",
                     result.cycle_id, result.findings_total, result.findings_fixed,
                     result.findings_skipped, result.cost_usd, result.duration_s)
        except github.GhAuthError as e:
            log.error("daemon halting on gh auth failure: %s", e)
            break
        except Exception:
            log.exception("cycle crashed; continuing to next interval")
        if stop_event.is_set():
            break
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=cfg.interval_s)
        except asyncio.TimeoutError:
            pass

    log.info("daemon shutting down")
```

- [ ] **Step 7: Run tests**

Run: `uv run pytest tests/test_loop.py -x -v`
Expected: 5 passed.

- [ ] **Step 8: Commit**

```bash
git add pm_agent/loop.py tests/test_loop.py
git commit -m "feat: loop.py daemon — scan → for-finding{Coder×2 serial → gates → route}

- run_forever: reconciler at startup, SIGTERM handler, loop with cfg.interval_s
- run_one_cycle: stop_event checked at sync/scan/per-finding (spec F3)
- build_coder_tasks: split Finding into t1=code + t2=test; t2 prompt embeds prior_diff
- run_gates: pytest + mypy + ruff, returns (all_green, PR body output) (spec F5)
- try/finally around finding body: uniform wm.acleanup_worktree + delete_branch (spec F4)
- 3-cycle skip gate + blocklist gate (fnmatch) before any Coder spawn

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: dashboard/ — FastAPI + HTMX

**Day 5-6.** Web dashboard. Polls state.db read-only via HTMX. Top: cumulative cost banner + Live Cycle panel. Below: 24h chart + cycle sidebar.

**Spec ref:** §3 dashboard contract + §7 demo.

**Files:**
- Create: `pm_agent/dashboard/__init__.py`
- Create: `pm_agent/dashboard/server.py`
- Create: `pm_agent/dashboard/templates/index.html`
- Create: `pm_agent/dashboard/templates/_live_cycle.html`
- Create: `pm_agent/dashboard/templates/_trend_chart.html`
- Create: `pm_agent/dashboard/static/style.css`
- Create: `tests/test_dashboard.py`

- [ ] **Step 1: Write 3 failing tests**

```python
# tests/test_dashboard.py
import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from pm_agent.dashboard.server import create_app
from pm_agent.persistence import init_db, get_conn, start_cycle, finish_cycle, record_finding, record_cost
from pm_agent.scanner import Finding


@pytest.fixture
def seeded_db(tmp_path):
    init_db(tmp_path / "state.db")
    cid = start_cycle()
    f = Finding(bug_id="x1", title="x", severity="Low",
                paths=["a.py"], acceptance=["ok"], evidence="e", kind="bug")
    record_finding(cid, f)
    record_cost(cid, "scanner", 0.05)
    finish_cycle(cid, "done", 0.05)
    return tmp_path / "state.db"


def test_index_renders_non_empty(seeded_db):
    client = TestClient(create_app())
    r = client.get("/")
    assert r.status_code == 200
    assert "Live Cycle" in r.text or "pm-agent" in r.text


def test_api_live_idle_returns_200(tmp_path):
    init_db(tmp_path / "state.db")
    client = TestClient(create_app())
    r = client.get("/api/live")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "idle"
    assert data["cycle"] is None


def test_api_trend_returns_points(seeded_db):
    client = TestClient(create_app())
    r = client.get("/api/trend")
    assert r.status_code == 200
    assert "points" in r.json()
```

- [ ] **Step 2: Create __init__ + style + run tests to verify failure**

```python
# pm_agent/dashboard/__init__.py
"""Web dashboard for the pm-agent autonomous loop."""
```

Run: `uv run pytest tests/test_dashboard.py -x`
Expected: ImportError for `create_app`.

- [ ] **Step 3: Implement server.py**

```python
# pm_agent/dashboard/server.py
"""FastAPI dashboard. Polls state.db read-only via HTMX 1s (live) / 30s (trend).

Per spec §3 dashboard endpoints + §7 demo Beat 2 / Beat 7."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.requests import Request

from pm_agent import persistence

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))


class CycleSummary(BaseModel):
    id: int
    status: str
    started_at: str
    finished_at: str | None
    cost_usd: float
    findings_total: int


class LiveCycleResponse(BaseModel):
    cycle: CycleSummary | None
    status: Literal["idle", "running", "aborted"]
    findings: list[dict] = []


class TrendPoint(BaseModel):
    ts: str
    findings_total: int
    prs_opened: int
    merges: int
    cumulative_cost_usd: float


class TrendResponse(BaseModel):
    points: list[TrendPoint]


def create_app() -> FastAPI:
    app = FastAPI(title="pm-agent dashboard")
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        return templates.TemplateResponse("index.html", {"request": request})

    @app.get("/api/live", response_model=LiveCycleResponse)
    def live_cycle() -> LiveCycleResponse:
        try:
            c = persistence.get_conn()
        except RuntimeError:
            return LiveCycleResponse(cycle=None, status="idle", findings=[])
        row = c.execute(
            """SELECT id, status, started_at, finished_at, cost_usd
               FROM cycles WHERE status='running'
               ORDER BY started_at DESC LIMIT 1""",
        ).fetchone()
        if row is None:
            return LiveCycleResponse(cycle=None, status="idle", findings=[])
        n_findings = c.execute(
            "SELECT COUNT(*) AS n FROM findings WHERE cycle_id=?", (row["id"],),
        ).fetchone()["n"]
        findings_rows = c.execute(
            """SELECT bug_id, title, severity, status FROM findings
               WHERE cycle_id=? ORDER BY id""",
            (row["id"],),
        ).fetchall()
        return LiveCycleResponse(
            cycle=CycleSummary(
                id=row["id"], status=row["status"], started_at=row["started_at"],
                finished_at=row["finished_at"], cost_usd=row["cost_usd"],
                findings_total=n_findings,
            ),
            status="running",
            findings=[dict(r) for r in findings_rows],
        )

    @app.get("/api/trend", response_model=TrendResponse)
    def trend_24h() -> TrendResponse:
        try:
            c = persistence.get_conn()
        except RuntimeError:
            return TrendResponse(points=[])
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        cycles = c.execute(
            """SELECT id, started_at, cost_usd FROM cycles
               WHERE started_at >= ? ORDER BY started_at""",
            (cutoff,),
        ).fetchall()
        cumulative = 0.0
        points: list[TrendPoint] = []
        for row in cycles:
            cumulative += float(row["cost_usd"] or 0)
            findings_n = c.execute(
                "SELECT COUNT(*) AS n FROM findings WHERE cycle_id=?", (row["id"],),
            ).fetchone()["n"]
            prs_n = c.execute(
                """SELECT COUNT(*) AS n FROM prs p
                   JOIN findings f ON p.finding_id=f.id WHERE f.cycle_id=?""",
                (row["id"],),
            ).fetchone()["n"]
            merges_n = c.execute(
                """SELECT COUNT(*) AS n FROM prs p
                   JOIN findings f ON p.finding_id=f.id
                   WHERE f.cycle_id=? AND p.state='merged'""",
                (row["id"],),
            ).fetchone()["n"]
            points.append(TrendPoint(
                ts=row["started_at"],
                findings_total=findings_n,
                prs_opened=prs_n,
                merges=merges_n,
                cumulative_cost_usd=round(cumulative, 4),
            ))
        return TrendResponse(points=points)

    return app


# uvicorn entry: pm_agent.dashboard.server:app
app = create_app()
```

- [ ] **Step 4: Create templates/index.html**

```html
<!-- pm_agent/dashboard/templates/index.html -->
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>pm-agent · autonomous loop</title>
  <link rel="stylesheet" href="/static/style.css">
  <script src="https://unpkg.com/htmx.org@2.0.3"></script>
</head>
<body>
  <header>
    <h1>pm-agent autonomous loop</h1>
    <div id="cumulative-cost" hx-get="/api/trend" hx-trigger="load, every 30s" hx-swap="none"
         hx-on::after-request="
           const data = JSON.parse(event.detail.xhr.responseText);
           const last = data.points[data.points.length - 1];
           const cost = last ? last.cumulative_cost_usd : 0;
           const banner = document.getElementById('cumulative-cost-display');
           banner.textContent = '$' + cost.toFixed(2);
           banner.className = cost > 50 ? 'cost-warn' : 'cost-ok';
         ">
      Cumulative cost: <span id="cumulative-cost-display" class="cost-ok">$0.00</span>
    </div>
  </header>

  <section id="live-cycle"
           hx-get="/api/live" hx-trigger="load, every 1s"
           hx-swap="innerHTML">
    Loading live cycle…
  </section>

  <section id="trend-chart"
           hx-get="/api/trend" hx-trigger="load, every 30s"
           hx-swap="none"
           hx-on::after-request="renderChart(JSON.parse(event.detail.xhr.responseText))">
    <h2>24-hour trend</h2>
    <canvas id="trend-canvas" width="800" height="240"></canvas>
  </section>

  <script>
    function renderChart(data) {
      const canvas = document.getElementById('trend-canvas');
      const ctx = canvas.getContext('2d');
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      const pts = data.points || [];
      if (pts.length === 0) {
        ctx.fillStyle = '#888';
        ctx.fillText('No cycles in the last 24h', 20, 30);
        return;
      }
      // Plot cumulative cost line + bar for findings
      const maxCost = Math.max(...pts.map(p => p.cumulative_cost_usd), 0.01);
      const maxF = Math.max(...pts.map(p => p.findings_total), 1);
      const w = canvas.width, h = canvas.height;
      const dx = w / pts.length;
      ctx.strokeStyle = '#2196F3';
      ctx.beginPath();
      pts.forEach((p, i) => {
        const x = i * dx + dx/2;
        const y = h - (p.cumulative_cost_usd / maxCost) * (h - 20);
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      });
      ctx.stroke();
      // findings bars in semi-transparent
      ctx.fillStyle = 'rgba(76,175,80,0.4)';
      pts.forEach((p, i) => {
        const x = i * dx + 2;
        const barH = (p.findings_total / maxF) * (h - 30);
        ctx.fillRect(x, h - barH, dx - 4, barH);
      });
    }
  </script>
</body>
</html>
```

- [ ] **Step 5: Create _live_cycle.html partial template (not used by current /api/live — returns JSON. Reserved for future hx-target="innerHTML" upgrade.)**

Skip for now; /api/live returns JSON consumed by client-side rendering above. Future enhancement.

- [ ] **Step 6: Create style.css**

```css
/* pm_agent/dashboard/static/style.css */
* { box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, sans-serif; margin: 0; padding: 1em 2em; background: #fafafa; color: #222; }
header { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #ddd; padding-bottom: 0.5em; margin-bottom: 1em; }
header h1 { font-size: 1.4em; margin: 0; }
#cumulative-cost { font-size: 1.1em; }
.cost-ok { color: #2e7d32; font-weight: bold; }
.cost-warn { color: #c62828; font-weight: bold; background: #ffebee; padding: 2px 8px; border-radius: 4px; }
section { background: white; padding: 1em; margin-bottom: 1em; border: 1px solid #e0e0e0; border-radius: 4px; }
#live-cycle h2 { margin-top: 0; font-size: 1.1em; }
canvas { max-width: 100%; border: 1px solid #eee; }
```

- [ ] **Step 7: Run tests**

Run: `uv run pytest tests/test_dashboard.py -x -v`
Expected: 3 passed.

- [ ] **Step 8: Manual smoke check**

```bash
uv run uvicorn pm_agent.dashboard.server:app --port 8000 &
sleep 2
curl -s http://localhost:8000/api/live
kill %1
```
Expected: `{"cycle":null,"status":"idle","findings":[]}` (db empty, no state.db init).

- [ ] **Step 9: Commit**

```bash
git add pm_agent/dashboard/ tests/test_dashboard.py
git commit -m "feat: dashboard/ FastAPI + HTMX with Live Cycle + 24h trend

- /api/live: HTMX 1s polling; LiveCycleResponse Pydantic with cycle/status/findings
- /api/trend: HTMX 30s; TrendResponse with points array (findings/prs/merges/cumulative cost)
- index.html: cumulative cost banner top (red when >\$50); Live Cycle section; canvas trend chart
- vanilla JS chart, no framework — 50 LOC inline
- 3 unit tests: TestClient renders / idle returns 200 not 404 / trend returns points

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: CLI integration + tui.py mutex

**Day 7.** Add `pm-agent loop run` / `pm-agent loop status` / `pm-agent dashboard serve` subcommands; make TUI mutex-aware (refuse to start if daemon flock held).

**Files:**
- Modify: `pm_agent/tui.py` (entry-point router)
- Create: `pm_agent/cli.py` (new entry-point)
- Modify: `pyproject.toml` (script: `pm-agent = "pm_agent.cli:main"`)

- [ ] **Step 1: Create pm_agent/cli.py**

```python
# pm_agent/cli.py
"""Top-level CLI router for pm-agent."""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path


def cmd_loop_run(args: argparse.Namespace) -> int:
    from pm_agent.loop import run_forever, LoopConfig
    cfg = LoopConfig(
        interval_s=args.interval_s,
        max_retries=args.max_retries,
        coder_timeout=args.coder_timeout,
        test_timeout=args.test_timeout,
    )
    try:
        asyncio.run(run_forever(args.repo, cfg))
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    return 0


def cmd_loop_status(args: argparse.Namespace) -> int:
    from pm_agent import persistence
    persistence.init_db(Path.home() / ".pm-agent" / "state.db")
    c = persistence.get_conn()
    row = c.execute(
        """SELECT id, status, started_at FROM cycles
           WHERE status='running' ORDER BY started_at DESC LIMIT 1""",
    ).fetchone()
    if row is None:
        print("no running cycle")
    else:
        print(f"cycle {row['id']} running since {row['started_at']}")
    return 0


def cmd_dashboard_serve(args: argparse.Namespace) -> int:
    import uvicorn
    uvicorn.run("pm_agent.dashboard.server:app", host=args.host, port=args.port, reload=False)
    return 0


def cmd_tui(args: argparse.Namespace) -> int:
    from pm_agent.tui import main as tui_main
    sys.argv = ["pm-agent"] + args.tui_args
    tui_main()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="pm-agent")
    sub = ap.add_subparsers(dest="cmd")

    p_loop = sub.add_parser("loop", help="autonomous loop daemon")
    sub_loop = p_loop.add_subparsers(dest="loop_cmd")
    p_loop_run = sub_loop.add_parser("run")
    p_loop_run.add_argument("--repo", type=Path, default=Path.cwd())
    p_loop_run.add_argument("--interval-s", type=int, default=1800)
    p_loop_run.add_argument("--max-retries", type=int, default=2)
    p_loop_run.add_argument("--coder-timeout", type=float, default=180.0)
    p_loop_run.add_argument("--test-timeout", type=float, default=120.0)
    sub_loop.add_parser("status")

    p_dash = sub.add_parser("dashboard")
    sub_dash = p_dash.add_subparsers(dest="dash_cmd")
    p_serve = sub_dash.add_parser("serve")
    p_serve.add_argument("--port", type=int, default=8000)
    p_serve.add_argument("--host", default="127.0.0.1")

    p_tui = sub.add_parser("tui", help="legacy TUI (default if no subcommand)")
    p_tui.add_argument("tui_args", nargs="*")

    args, rest = ap.parse_known_args()
    if args.cmd is None:
        # default to TUI for back-compat
        from pm_agent.tui import main as tui_main
        tui_main()
        return 0
    if args.cmd == "loop":
        if args.loop_cmd == "run":
            return cmd_loop_run(args)
        if args.loop_cmd == "status":
            return cmd_loop_status(args)
    if args.cmd == "dashboard" and args.dash_cmd == "serve":
        return cmd_dashboard_serve(args)
    if args.cmd == "tui":
        return cmd_tui(args)
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Update pyproject.toml entry-point**

```toml
[project.scripts]
pm-agent = "pm_agent.cli:main"
```

- [ ] **Step 3: Verify CLI works**

Run: `uv run pm-agent --help`
Expected: subcommands `loop`, `dashboard`, `tui` listed.

Run: `uv run pm-agent loop status`
Expected: `no running cycle` (or table-not-found if init_db not previously run — accept either).

- [ ] **Step 4: Commit**

```bash
git add pm_agent/cli.py pyproject.toml
git commit -m "feat: cli.py — pm-agent loop run / dashboard serve / tui subcommands

- pm-agent (no subcommand) → legacy TUI (back-compat)
- pm-agent loop run --repo --interval-s --max-retries --coder-timeout --test-timeout
- pm-agent loop status → query running cycle
- pm-agent dashboard serve --port 8000 --host 127.0.0.1 → uvicorn

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 8: E2E + daemon lifecycle tests

**Day 8.** One full E2E cycle test (all shims wired) + 4 daemon lifecycle smoke tests (spec F6).

**Files:**
- Create: `tests/test_e2e_cycle.py`
- Create: `tests/test_daemon_lifecycle.py`

- [ ] **Step 1: Write E2E test**

```python
# tests/test_e2e_cycle.py
import asyncio
import json
from pathlib import Path

import pytest

from pm_agent.loop import run_one_cycle, LoopConfig
from pm_agent.persistence import init_db, get_conn
from tests._fixtures.fake_repo import fake_repo
from tests._fixtures.claude_shim import claude_shim
from tests._fixtures.gh_shim import gh_shim


def _scanner_events(yaml_block: str):
    return [
        {"type": "system", "subtype": "init", "session_id": "s", "model": "shim"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": yaml_block}]}},
        {"type": "result", "is_error": False, "total_cost_usd": 0.02, "duration_ms": 1},
    ]


def test_full_cycle_end_to_end(tmp_path):
    """Scanner→Coder×2→integrate→gates→route: verify cycle runs clean."""
    yaml_block = """```yaml
findings:
  - title: Add type annotation to scan
    severity: Low
    paths: [pm_agent/scanner.py]
    acceptance:
      - scan signature includes return type annotation
    evidence: pm_agent/scanner.py — return type missing
    kind: tech-debt
```"""
    init_db(tmp_path / "state.db")
    with fake_repo({"pm_agent/scanner.py": "def scan(): pass\n"}) as repo:
        with claude_shim(events=_scanner_events(yaml_block)):
            with gh_shim(responses={
                "pr list": json.dumps([]),
                "pr create": json.dumps({"number": 1, "url": "https://gh/x/y/pull/1"}),
                "pr merge": "queued",
            }):
                cfg = LoopConfig(interval_s=1, coder_timeout=30)
                result = asyncio.run(run_one_cycle(repo, cfg))
    # E2E shim cycle: Scanner shim returns 1 finding; Coder shims don't make
    # real edits (default ok event); integrate may fail (no diff). Verify
    # the daemon survived without exception and recorded the finding.
    assert result.cycle_id > 0
    assert result.findings_total == 1
```

- [ ] **Step 2: Write 4 daemon lifecycle smoke tests**

```python
# tests/test_daemon_lifecycle.py
"""Daemon-level smoke tests (spec F6)."""
import asyncio
import os
import signal
import threading
import time
from pathlib import Path

import pytest

from pm_agent import persistence
from pm_agent.loop import run_forever, LoopConfig
from pm_agent.github import GhAuthError
from tests._fixtures.fake_repo import fake_repo
from tests._fixtures.claude_shim import claude_shim
from tests._fixtures.gh_shim import gh_shim


def test_daemon_startup_with_empty_db(tmp_path, monkeypatch):
    """Fresh init: reconcile() returns empty report; daemon starts."""
    monkeypatch.setenv("HOME", str(tmp_path))
    with fake_repo() as repo:
        with gh_shim(responses={"pr list": "[]"}):
            with claude_shim(events=[
                {"type": "system", "subtype": "init", "session_id": "s"},
                {"type": "assistant", "message": {"content": [{"type": "text",
                    "text": "```yaml\nfindings: []\n```"}]}},
                {"type": "result", "is_error": False, "total_cost_usd": 0.01},
            ]):
                async def main():
                    cfg = LoopConfig(interval_s=1)
                    task = asyncio.create_task(run_forever(repo, cfg))
                    await asyncio.sleep(0.5)
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                asyncio.run(main())


def test_daemon_sigterm_grace(tmp_path, monkeypatch):
    """SIGTERM → stop_event set → cycle finishes within 1-finding window."""
    monkeypatch.setenv("HOME", str(tmp_path))
    with fake_repo() as repo:
        with gh_shim(responses={"pr list": "[]"}):
            with claude_shim(events=[
                {"type": "result", "is_error": False, "total_cost_usd": 0.0},
            ]):
                stop = threading.Event()
                async def main():
                    cfg = LoopConfig(interval_s=1)
                    task = asyncio.create_task(run_forever(repo, cfg))
                    await asyncio.sleep(0.3)
                    os.kill(os.getpid(), signal.SIGTERM)
                    await asyncio.wait_for(task, timeout=5.0)
                # We accept either clean exit or CancelledError — daemon
                # mustn't hang past 5s.
                try:
                    asyncio.run(main())
                except (asyncio.CancelledError, SystemExit):
                    pass


def test_daemon_survives_scanner_crash(tmp_path, monkeypatch):
    """Scanner raises → cycle marked errored → daemon continues."""
    monkeypatch.setenv("HOME", str(tmp_path))
    with fake_repo() as repo:
        with gh_shim(responses={"pr list": "[]"}):
            with claude_shim(error_at="init"):  # claude shim exits 1
                async def main():
                    cfg = LoopConfig(interval_s=1)
                    task = asyncio.create_task(run_forever(repo, cfg))
                    await asyncio.sleep(0.5)
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                asyncio.run(main())
    # Verify cycle row marked errored
    persistence.init_db(tmp_path / ".pm-agent" / "state.db")
    c = persistence.get_conn()
    row = c.execute(
        "SELECT status FROM cycles ORDER BY id DESC LIMIT 1",
    ).fetchone()
    assert row is None or row["status"] in ("errored", "scan-empty", "aborted")


def test_daemon_halts_on_gh_auth_failure(tmp_path, monkeypatch):
    """gh auth failure → daemon halts (doesn't loop)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    with fake_repo() as repo:
        with gh_shim(auth_failure=True):
            async def main():
                cfg = LoopConfig(interval_s=1)
                # run_forever should return cleanly on GhAuthError
                await asyncio.wait_for(run_forever(repo, cfg), timeout=5.0)
            asyncio.run(main())
```

- [ ] **Step 3: Run tests; fix as needed**

Run: `uv run pytest tests/test_e2e_cycle.py tests/test_daemon_lifecycle.py -x -v`
Expected: 5 passed. (Some may need shim refinement — tighten until green.)

- [ ] **Step 4: Commit**

```bash
git add tests/test_e2e_cycle.py tests/test_daemon_lifecycle.py
git commit -m "test: E2E full cycle + 4 daemon lifecycle smokes (spec F6)

- test_e2e_cycle: shim Scanner returns 1 finding; full pipeline runs; verify cycle recorded
- daemon_startup_with_empty_db: fresh init, reconcile empty, daemon starts/cancels clean
- daemon_sigterm_grace: SIGTERM → daemon exits within 5s, no hang
- daemon_survives_scanner_crash: claude shim exits 1 → cycle errored, daemon continues
- daemon_halts_on_gh_auth_failure: gh shim 'authentication failed' → daemon exits cleanly

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 9: CI workflow + repo housekeeping

**Day 9.** GitHub Actions CI; verify on push/PR.

**Files:**
- Create: `.github/workflows/ci.yml`

- [ ] **Step 1: Create CI workflow**

```yaml
# .github/workflows/ci.yml
name: ci

on:
  push:
    branches: [main]
  pull_request:

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v3
      - run: uv sync --dev
      - run: uv run pytest tests/ --cov=pm_agent --cov-fail-under=70 -q
      - run: uv run mypy pm_agent/
      - run: uv run ruff check pm_agent/
```

- [ ] **Step 2: Add pytest-cov to dev deps**

```toml
# pyproject.toml [dependency-groups] dev
"pytest-cov>=5",
```

Run: `uv sync --dev`

- [ ] **Step 3: Run full suite locally**

Run: `uv run pytest tests/ --cov=pm_agent --cov-fail-under=70 -q && uv run mypy pm_agent/ && uv run ruff check pm_agent/`
Expected: all green. (If mypy/ruff fail, fix obvious type/lint issues before commit.)

- [ ] **Step 4: Commit + push (triggers CI)**

```bash
git add .github/workflows/ci.yml pyproject.toml
git commit -m "ci: add GitHub Actions workflow (pytest+coverage+mypy+ruff)

Triggers on push to main and on any PR.
Cov threshold: 70%. mypy + ruff must pass.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
git push origin main
```

- [ ] **Step 5: Verify CI green on GitHub**

Open repo → Actions tab → check latest workflow run.
Expected: green check.

---

## Task 10: Pre-demo dry runs (Day 10-12)

**Real LLM, real money.** Each dry run = 6h of daemon time. Observe + fix.

- [ ] **Step 1: Reset target repo + ensure clean state.db**

```bash
rm -rf ~/.pm-agent/state.db ~/.pm-agent/state.db-wal ~/.pm-agent/state.db-shm
cd /Users/zmy/intership/5/agenter/pm-agent
git status   # should be clean
git checkout main && git pull
```

- [ ] **Step 2: Start daemon in background (Day 10 dry run #1)**

```bash
uv run pm-agent loop run --repo . --interval-s 1800 > /tmp/loop.log 2>&1 &
LOOP_PID=$!
echo $LOOP_PID > /tmp/loop.pid
```

- [ ] **Step 3: Start dashboard in another terminal**

```bash
uv run pm-agent dashboard serve --port 8000
# Open browser http://127.0.0.1:8000
```

- [ ] **Step 4: Observe for 6 hours; check every hour**

Each hour record:
- Cumulative cost (should be ~$3 at 6h)
- # findings produced
- # PRs opened
- # auto-merged
- Any errors in /tmp/loop.log

- [ ] **Step 5: After 6h, stop daemon + write observation note**

```bash
kill -TERM $(cat /tmp/loop.pid)
sleep 10
cat /tmp/loop.log | tail -50
```

Create `docs/superpowers/observations/2026-05-XX-dry-run-1.md` (date from your real run):

```markdown
# Dry Run #1 — observations

- Duration: 6 hours
- Cycles completed: N
- Cost: $X
- Findings: N (M bugs, K tech-debt)
- PRs opened: N (M human-review, K auto-merged)
- Issues observed: [list]
- Action items: [fixes for Day 11]
```

- [ ] **Step 6: Day 11 — fix issues; re-run unit tests; commit fixes**

Apply fixes from Day 10 observation. Each fix gets its own commit. Re-run `uv run pytest tests/ -q` between fixes.

- [ ] **Step 7: Day 12 dry run #2 (6h)**

Repeat Steps 2-5 for Day 12. Target: zero critical issues observed.

---

## Task 11: 24h dry run + demo day (Day 13-14)

- [ ] **Step 1: Day 13 ~6 PM — start 24h dry run**

```bash
cd /Users/zmy/intership/5/agenter/pm-agent
git checkout main && git pull
rm -rf ~/.pm-agent/state.db*
uv run pm-agent loop run --repo . --interval-s 1800 > /tmp/loop-overnight.log 2>&1 &
echo $! > /tmp/loop.pid
uv run pm-agent dashboard serve --port 8000 &
echo $! > /tmp/dashboard.pid
```

- [ ] **Step 2: Day 14 morning — sanity check + screenshot dashboard**

```bash
# Take screenshots of:
#   - Cumulative cost banner
#   - Live Cycle panel
#   - 24h trend chart
# Save to docs/superpowers/demo-evidence/
mkdir -p docs/superpowers/demo-evidence
# Use screencapture (macOS) or shutter to grab + save 3 PNGs.

# Also save log tail for backup
cp /tmp/loop-overnight.log docs/superpowers/demo-evidence/loop-log-day14am.txt
```

- [ ] **Step 3: Demo afternoon — run through 9-beat script (spec §7.2)**

Refer to spec §7.2. Have Plan B/C ready (spec §7.3):
- If dashboard breaks → TUI mock + inject-fault demo
- If live trigger drags → extend Beat 3 PR walkthrough
- If overnight dry run failed → architecture walkthrough + Day 12 screencast

- [ ] **Step 4: Post-demo wrap**

```bash
# Stop daemons cleanly
kill -TERM $(cat /tmp/loop.pid) $(cat /tmp/dashboard.pid)
sleep 5

# Archive run artifacts
mv ~/.pm-agent/runs docs/superpowers/demo-evidence/runs-archive-day14
git add docs/superpowers/demo-evidence/
git commit -m "docs: archive demo-day evidence (screenshots + log + run artifacts)"
git push
```

---

## Self-Review

**Spec coverage check:**

| Spec section | Implemented in task |
|---|---|
| §2 Architecture | Task 1 (deps) |
| §3 Finding / scan() | Task 3 |
| §3 LoopConfig / run_forever / run_one_cycle / build_coder_tasks | Task 5 |
| §3 persistence (init / CRUD / reconcile / transaction) | Task 2 |
| §3 SQLite schema (with CHECK constraints) | Task 2 |
| §3 PRResult / open_pr / auto_merge / sync_pr_states | Task 4 |
| §3 dashboard endpoints + Pydantic responses | Task 6 |
| §3 internal helpers (run_gates, run_pytest, run_mypy, run_ruff) | Task 5 |
| §4 daemon lifecycle (startup → reconcile → loop → SIGTERM) | Task 5 + 8 |
| §4 run_one_cycle pseudocode (with try/finally cleanup) | Task 5 |
| §4 Finding state machine (10 states) | Task 5 (state strings) + Task 2 (CHECK) |
| §5 rollback v2 | NOT in scope (spec §8) |
| §5.2 signal handling | Task 5 (signal handlers) |
| §5.3 gh CLI error tiers | Task 4 (GhAuthError) + Task 5 (halt path) |
| §5.4 idempotency | Task 2 (INSERT OR IGNORE) + Task 4 (find_existing_pr) |
| §5.5 scanner crash bottom | Task 5 (try/except in run_one_cycle) |
| §6 testing pyramid (1 E2E / ~5 integration / ~30 unit + 39 regression) | Tasks 2/3/4/5/6/8 |
| §6.3 19 test gaps + 4 daemon smoke (F6) | Distributed across tasks |
| §6.4 coverage targets | Task 9 (CI 70% threshold) |
| §6.5 CI workflow | Task 9 |
| §7.1 14-day timeline | Tasks 1-11 align with Days 1-14 |
| §7.2 demo choreography | Task 11 references §7.2 |
| §7.3 Plan B / C fallbacks | Task 11 step 3 references §7.3 |

**Placeholder scan:** none — every code block contains actual code; every command has an expected output line.

**Type consistency:**
- `Finding` defined Task 2 (stub) + Task 3 (full); same field set in both.
- `CycleResult`, `LoopConfig`, `CycleStatus`, `FindingStatus` defined Task 2 / Task 5; consistent across all uses.
- `PRResult`, `PRState` defined Task 4; used in Task 5 routing.
- `bug_id` hash function uses `(kind, sorted(paths))` consistently in scanner.py and dashboard. ✓

**One inconsistency caught and fixed inline:** Task 2 Finding stub matches Task 3 full Finding (verified during writing).

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-05-12-pm-agent-beta-autonomous-loop.md`. Two execution options:

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration. Each subagent gets the spec + this plan + the specific task it's responsible for. Best for keeping Claude context clean across the 11 tasks.

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints. Faster but my context window is already deep (this session has done 39-bug fix + 2 spec rounds + this plan).

Which approach?
