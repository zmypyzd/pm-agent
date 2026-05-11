# pm-agent Beta — Autonomous Bug-Fix Loop

**Status**: draft (post-brainstorm)
**Date**: 2026-05-12
**Author**: zmy
**Audience**: implementation by zmy + Claude Code; demo target 安子岩

---

## 0. Summary

Extend pm-agent (Day-14 MVP, 39 latent bugs fixed) into a **Beta-grade autonomous
bug-fix loop** that meta-scans the pm-agent repo, fixes findings via the
existing Planner+Coder+Integration pipeline, routes by severity (Critical/High
→ human-review PR; Low/hygiene → auto-merge after triple-gate), and surfaces
24h trend + live activity in a web dashboard.

Goal: demo to mentor in 1-2 weeks; budget ~$100 total; feature completeness
> engineering completeness.

This is NOT production-grade autonomous code maintenance. Trust boundary is
risk-tiered: critical changes always require human review. Auto-merged path
covers only Low/hygiene findings that pass pytest + mypy + ruff.

---

## 1. Captured Requirements

| Dimension | Decision |
|---|---|
| Audience | 1 mentor, internship demo |
| Timeline | 1-2 weeks build + 1 day demo |
| Budget | ~$100 total (build + demo); demo quality > cost |
| Work source | meta — Scanner audits pm-agent itself |
| Scanner scope | Bugs first; fall back to tech-debt (refactor, hygiene, missing tests, drift) when bug well is dry |
| Trust tier | Critical/High → open PR for human review; Low/hygiene → auto-merge after pytest+mypy+ruff green |
| Demo form factor | Dual-screen — real-time view via web dashboard (Live Cycle panel), with 24h trend chart |
| Safety gates | (1) pytest 39 repros + mypy + ruff all green for auto-merge; (2) blocklist on .git/.github/.teamagent/pyproject critical fields; (3) skip + alert if same bug fails 3 cycles |
| Approach | A1 — bake new modules into existing pm-agent package |

---

## 2. Architecture

### Package growth (5 new modules + dashboard subdir)

```
pm_agent/
├── tasks.py / runner.py / planner.py / worktree.py / tui.py   (existing, untouched at the core)
├── scanner.py          (NEW) LLM scans pm-agent itself, emits Findings with severity baked in
├── loop.py             (NEW) daemon: scan → for-bug{Coder-1 serial → Coder-2 with diff → integrate → triple-gate → PR or auto-merge}
├── persistence.py      (NEW) SQLite at ~/.pm-agent/state.db, WAL mode, reconciler entry
├── github.py           (NEW) gh CLI wrap: open_pr / auto_merge / sync_pr_states
└── dashboard/          (NEW) FastAPI + HTMX, Live Cycle panel + 24h trend chart
    ├── server.py
    ├── templates/index.html
    └── static/
```

**Rationale**: HANDOFF §15's deferred items (Reviewer agent, push-to-remote,
cross-session persistence, shared_contract) all collapse into these modules.
Decision D1 folded the originally-separate `risk.py` into `scanner.py` — the
LLM emits a `severity` field with each Finding, so a second classifier was
redundant.

### CLI surface

- `pm-agent` — existing Day-14 TUI (unchanged; mutex with daemon via flock)
- `pm-agent loop run [--max-retries N --test-timeout SEC --interval-s SEC]` — start daemon
- `pm-agent loop status` — query current cycle state
- `pm-agent loop trigger-now [--inject scanner-empty | --seed-tech-debt]` — dev command
- `pm-agent dashboard serve --port 8000` — start web UI

### Module dependency graph

```
                     ┌──────────────┐
                     │   loop.py    │ daemon
                     └──────┬───────┘
        ┌───────────────────┼─────────────────┐
        ▼                   ▼                 ▼
  scanner.py          persistence.py     github.py
  (LLM call)          (SQLite WAL)       (gh CLI)
        │                   ▲                 │
        │                   │                 │
        └─►runner.py◄───────┤                 │
                            │                 │
              ┌─────────────┘                 │
              │                               │
       dashboard/server.py◄───── read-only ──┘
                            │
                       (HTMX polls)
```

### Single point of failure

- `~/.pm-agent/state.db` is the only persistent state. No backup, no
  replication. Acceptable for a 1-2 week demo. v2 path: periodic SQLite dump
  to user-supplied backup location.
- gh CLI auth failure halts daemon — see §5.3 for error semantics.

---

## 3. Component Contracts

### `pm_agent/scanner.py`

```python
@dataclass
class Finding:
    bug_id: str              # sha1(f"{kind}|" + "|".join(sorted(paths)))[:8]  (D11+§2 revision)
    title: str               # human-readable, may drift across cycles
    severity: Literal["Critical", "High", "Medium", "Low"]
    paths: list[str]
    acceptance: list[str]
    evidence: str            # short snippet / file:line / repro hint
    kind: Literal["bug", "tech-debt"]

async def scan(
    repo: Path,
    *,
    max_findings: int = 5,
    timeout: float = 120.0,
    fallback_to_tech_debt: bool = True,
    on_retry: Callable[[int, str], None] | None = None,
) -> tuple[list[Finding], float]:  # returns (findings, cost_usd) — Section 2 contract revision
    """LLM scans repo via runner.run_claude_async, parses YAML output (planner-style retry),
    bugs-first then tech-debt fallback when bugs = 0."""
```

Reuses: `runner.run_claude_async`, `planner.extract_yaml` + `parse_tasks` retry pattern.

### `pm_agent/loop.py`

```python
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
    blocklist: tuple[str, ...] = (".git/*", ".github/*", ".teamagent/*", "pyproject.toml")
    # ↑ each entry is a fnmatch glob; matched against Finding.paths;
    #   ANY match in ANY path → skip whole finding + alert.

async def run_forever(repo: Path, cfg: LoopConfig) -> None: ...
async def run_one_cycle(repo: Path, cfg: LoopConfig) -> CycleResult: ...

def build_coder_tasks(
    finding: Finding, prior_diff: str | None = None,
) -> tuple[CoderTask, CoderTask]:
    """First call (prior_diff=None) returns (t1=code, t2=test-with-no-diff-yet).
    After Coder-1 commits, call again with prior_diff to get t2 with the diff
    embedded in its prompt — Coder-2 sees the fix before writing the test."""
```

### `pm_agent/persistence.py`

```python
def init_db(path: Path) -> None:    # PRAGMA journal_mode=WAL; foreign_keys=ON; create tables.
def get_conn() -> sqlite3.Connection:    # thread-local; dashboard uses read-only flag

@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:    # multi-statement scoping

# CRUD
def start_cycle() -> int: ...
def finish_cycle(cycle_id: int, status: CycleStatus, cost_usd: float) -> None: ...
def record_finding(cycle_id: int, finding: Finding) -> int: ...    # INSERT OR IGNORE, returns existing finding_id on collision
def fix_attempts(bug_id: str) -> int: ...
def update_finding(finding_id: int, status: FindingStatus) -> None: ...
def record_pr(finding_id: int, gh_number: int, url: str, state: str) -> int: ...
def update_pr_state(gh_number: int, state: str) -> None: ...
def record_cost(cycle_id: int, agent: str, usd: float) -> None: ...

# Recovery
def reconcile(repo: Path) -> ReconcileReport: ...    # called once on daemon startup

CycleStatus = Literal["running", "done", "scan-empty", "aborted", "errored"]
FindingStatus = Literal["discovered", "fixing-code", "fixing-test", "integrating",
                        "testing", "routing", "done", "failed", "skipped", "interrupted"]
```

### SQLite schema

```sql
CREATE TABLE cycles (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL CHECK(status IN ('running','done','scan-empty','aborted','errored')),
    cost_usd REAL NOT NULL DEFAULT 0    -- denormalized cache; authoritative is SUM(costs)
);
CREATE INDEX idx_cycles_started_at ON cycles(started_at);

CREATE TABLE findings (
    id INTEGER PRIMARY KEY,
    cycle_id INTEGER NOT NULL REFERENCES cycles(id),
    bug_id TEXT NOT NULL,
    title TEXT NOT NULL,
    severity TEXT NOT NULL,
    paths_json TEXT NOT NULL,
    acceptance_json TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    UNIQUE(cycle_id, bug_id)
);
CREATE INDEX idx_findings_bug_id ON findings(bug_id);
CREATE INDEX idx_findings_cycle_status ON findings(cycle_id, status);

CREATE TABLE prs (
    id INTEGER PRIMARY KEY,
    finding_id INTEGER NOT NULL REFERENCES findings(id),
    github_number INTEGER NOT NULL UNIQUE,
    url TEXT NOT NULL,
    state TEXT NOT NULL,
    action TEXT NOT NULL,   -- 'opened'|'auto-merge-queued'|'merged-now'|'failed'
    created_at TEXT NOT NULL
);
CREATE INDEX idx_prs_state ON prs(state);

CREATE TABLE costs (
    id INTEGER PRIMARY KEY,
    cycle_id INTEGER NOT NULL REFERENCES cycles(id),
    agent TEXT NOT NULL,    -- 'scanner'|'coder-1'|'coder-2'|'integrate'
    usd REAL NOT NULL,
    at TEXT NOT NULL
);
CREATE INDEX idx_costs_cycle_at ON costs(cycle_id, at);
```

### `pm_agent/github.py`

```python
@dataclass
class PRResult:
    number: int
    url: str
    action: Literal["opened", "auto-merge-queued", "merged-now", "failed"]

@dataclass
class PRState:
    number: int
    state: Literal["open", "merged", "closed"]
    head_ref: str
    merged_at: str | None

async def open_pr(branch: str, finding: Finding, body_extras: str = "") -> PRResult:
    """gh pr create --title ... --body (acceptance + bug_id + body_extras).
    Idempotent: checks `gh pr list --head $branch` first; if exists, updates body."""

async def auto_merge(branch: str, finding: Finding) -> PRResult:
    """open_pr + `gh pr merge --auto --squash`. action reflects whether GH
    merged now (all checks green) or queued (waiting on protection)."""

async def sync_pr_states() -> list[PRState]:
    """gh pr list --state all --search 'head:ai/' --json … — sync persistence.prs.
    Called at start of each cycle. Catches mentor-side merges/closes."""
```

### `pm_agent/dashboard/server.py`

```python
class CycleSummary(BaseModel):
    id: int
    status: str
    started_at: str
    cost_usd: float
    findings_total: int

class LiveCycleResponse(BaseModel):
    cycle: CycleSummary | None
    status: Literal["idle", "running", "aborted"]
    findings: list[dict] = []      # live finding statuses

class TrendResponse(BaseModel):
    points: list[dict]             # {ts, findings_total, prs_opened, merges, cumulative_cost_usd}

app = FastAPI()

@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse: ...   # Live Cycle panel TOP, 24h chart below, cumulative cost in red banner

@app.get("/api/live", response_model=LiveCycleResponse)
def live_cycle() -> LiveCycleResponse: ...    # HTMX 1s polling; 200 + idle when empty

@app.get("/api/trend", response_model=TrendResponse)
def trend_24h() -> TrendResponse: ...         # HTMX 30s polling
```

---

## 4. Cycle Lifecycle

### Daemon lifecycle

```
pm-agent loop run
       │
       ▼
┌─────────────────────────┐
│ startup                 │
│  - persistence.init_db()│   PRAGMA WAL
│  - persistence.reconcile│   clean zombie cycle/finding/worktree
│  - WorktreeManager(repo)│   acquire flock
└──────────┬──────────────┘
           ▼
       ┌──────┐
   ┌──►│ idle │  sleep cfg.interval_s
   │   └──┬───┘
   │      ▼
   │  ┌──────────────┐
   │  │ cycle starts │  persistence.start_cycle() → cycle_id
   │  └──┬───────────┘
   │     ▼ run_one_cycle()
   │     │
   │  ┌──────────────┐
   │  │ cycle ends   │  persistence.finish_cycle(cycle_id, status, cost)
   │  └──┬───────────┘
   └─────┘  loop until SIGTERM (graceful) or stop_event set
```

### `run_one_cycle()` flow

```
1. github.sync_pr_states()         ← pick up external merges/closes
2. findings, scan_cost = scanner.scan(repo)
   persistence.record_cost(cycle_id, "scanner", scan_cost)

3. for finding in findings:
       persistence.record_finding(cycle_id, finding)

       # Gate (a): 3-cycle skip — uses finding.bug_id (paths+kind hash)
       if persistence.fix_attempts(finding.bug_id) >= 3:
           update_finding(status="skipped"); alert; continue

       # Gate (b): meta blocklist — fnmatch
       if any(fnmatch(p, pat) for p in finding.paths for pat in cfg.blocklist):
           update_finding(status="skipped"); alert; continue

       # Build CoderTasks. Coder-1 first (code fix), Coder-2 second (with diff).
       t1, _ = build_coder_tasks(finding, prior_diff=None)
       wm.create(t1.id)
       run Coder-1 → if fail: update_finding("failed"); continue
       diff_1 = wm.diff_against_base(t1.id)
       wm.cleanup_worktree(t1.id)
       if not diff_1.strip():
           # Coder-1 produced NO_CHANGES — nothing for Coder-2 to test against.
           # Don't proceed to Coder-2 with an empty fix; mark finding failed early.
           update_finding("failed"); wm.delete_branch(t1.id); continue

       _, t2 = build_coder_tasks(finding, prior_diff=diff_1)
       wm.create(t2.id)
       run Coder-2 → if fail: update_finding("failed"); wm.delete_branch(t1); continue
       wm.cleanup_worktree(t2.id)

       ig = wm.integrate(cycle_id, [t1.id, t2.id])
       if ig.conflicts:
           update_finding("failed"); wm.delete_branch(t1); wm.delete_branch(t2); continue

       # Gate (c): auto-merge requires pytest + mypy + ruff
       gates_green = run_pytest(repo) and run_mypy(repo) and run_ruff(repo)

       if finding.severity in {"Critical", "High"}:
           pr = github.open_pr(branch=t1.id, finding=finding,
                               body_extras=fail_output_if_any(gates_green))
       elif gates_green:
           pr = github.auto_merge(branch=t1.id, finding=finding)
       else:
           # Low/Medium severity but gates red → open PR for human review,
           # body includes failed gate output so reviewer sees why
           pr = github.open_pr(branch=t1.id, finding=finding,
                               body_extras=fail_output(pytest=..., mypy=..., ruff=...))

       persistence.record_pr(finding.id, pr.number, pr.url, pr.action)
       update_finding("done")

       # Clean branches regardless of outcome — keeps long-running daemon tidy
       wm.delete_branch(t1.id)
       wm.delete_branch(t2.id)

4. return CycleResult(...)
```

### Finding state machine

```
                  ┌──────────┐
        record    │discovered│
       ───────►   └────┬─────┘
                       │
                       │ gate (a)/(b) hit
                       ├───────────────►┌──────┐
                       │                │skipped│
                       │                └──────┘
                       ▼
                  ┌──────────┐
                  │fixing-code│ Coder-1 running
                  └────┬─────┘
                       │ fail → [failed]
                       ▼
                  ┌──────────┐
                  │fixing-test│ Coder-2 running (sees diff_1)
                  └────┬─────┘
                       │ fail → [failed]
                       ▼
                  ┌─────────────┐
                  │integrating  │ wm.integrate
                  └────┬────────┘
                       │ conflict → [failed]
                       ▼
                  ┌─────────────┐
                  │testing      │ pytest + mypy + ruff
                  └────┬────────┘
                       │
                       ▼
                  ┌─────────────┐
                  │routing      │ severity + gate result
                  └────┬────────┘
                ┌──────┴──────┐
                ▼             ▼
        ┌─────────────┐ ┌──────────────────┐
        │ pr-opened   │ │auto-merge-queued │
        └──────┬──────┘ └────────┬─────────┘
               └────────┬────────┘
                        ▼ (synced next cycle via github.sync_pr_states)
                    merged / closed / open
                                ▼
                            ┌──────┐
                            │ done │
                            └──────┘

interrupted ← reconciler sets this on restart for findings caught mid-flight.
              Counts toward fix_attempts.
```

---

## 5. Safety & Error Handling

### 5.1 Rollback (out of scope, v2)

No automatic rollback for auto-merged commits. If mentor finds regression
post-merge: manual `git revert <sha>`. dashboard sync_pr_states detects
reverted commits and labels accordingly.

### 5.2 Signal handling

| Signal | Behavior |
|---|---|
| SIGINT / SIGTERM | Set `stop_event`. The per-finding `for` loop checks `stop_event.is_set()` at the top of each iteration; current finding runs to completion (don't interrupt Coder), subsequent findings skipped. `finish_cycle()` records the partial cycle as `status='done'` (it completed gracefully, just shorter than planned). No new cycles started after stop_event. Daemon exits clean. |
| SIGKILL | Process dies; state.db has zombie rows; reconciler cleans on next startup. |

### 5.3 gh CLI failure tiers

| Error | retry | halt daemon | UX |
|---|---|---|---|
| Rate limit (HTTP 403) | wait reset + 60s | no | dashboard shows "throttled until HH:MM" |
| Network (DNS / timeout) | 3× exponential backoff (5/15/45s) | no | dashboard log warning |
| Auth (401 / "gh auth login") | no | **YES** | dashboard red banner + notification hook |
| Other (5xx / 422) | 1 retry | yes after retry | dashboard log; skip finding |

### 5.4 Idempotency

| Operation | Idempotent? | Mechanism |
|---|---|---|
| `record_finding(cycle_id, finding)` | YES | UNIQUE(cycle_id, bug_id) + INSERT OR IGNORE |
| `github.open_pr(branch, finding)` | YES (via head-branch lookup) | `gh pr list --head $branch` first; update body if exists |
| `github.auto_merge(branch, finding)` | YES | same head-branch lookup |
| `wm.create(task_id)` | YES | preclean (Phase 4 fix) |
| `record_cost(cycle_id, agent, usd)` | NO (additive by design) | sum semantics; not idempotent |

### 5.5 Scanner crash bottom

```python
async def run_one_cycle:
    try:
        findings, scan_cost = await scanner.scan(repo, ...)
    except Exception as e:
        persistence.finish_cycle(cycle_id, status="errored", cost_usd=0)
        log.error("scanner crashed: %s", e)
        return    # next cycle tries again; daemon survives
```

5 consecutive `errored` cycles → dashboard prominent banner.

### 5.6 Concurrent external git operations

demo scope: single user, single machine — assume no concurrent push to
master. Defensive: integrate uses `git fetch origin master` so base is
origin/master not local HEAD. Conflicts during integrate → finding fail →
retry next cycle.

### 5.7 Error summary table

| Source | Type | Blast radius | UX |
|---|---|---|---|
| Coder timeout | finding fail | single finding | `failed:timeout` |
| Coder API error | finding fail | single finding | `failed:api_error` |
| Integrate conflict | finding fail | single finding | `failed:merge_conflict` |
| Pytest red | route to PR (no auto) | single finding | PR body includes pytest output |
| Mypy/ruff red | route to PR | single finding | PR body includes lint/type output |
| Scanner crash | cycle errored | single cycle | dashboard log + 5-streak banner |
| gh rate limit | cycle pauses | full cycle | dashboard "throttled until" |
| gh auth failure | **daemon halt** | permanent | dashboard red + manual recovery |
| SIGKILL | wait for reconciler | until restart | reconciler reports N zombies |
| Disk full | daemon exit fail-fast | full daemon | log + exit |

---

## 6. Testing Strategy

### 6.1 Test pyramid

```
              ▲ E2E (1)
            ▲▲▲ Integration (~5)
        ▲▲▲▲▲▲▲▲▲ Unit (~30)
       ────────────────
       Regression net (39 existing repros)   ← already shipped
```

### 6.2 Directory

```
tests/
├── test_repros.py            (existing — 39 bug repros)
├── test_scanner.py
├── test_loop.py
├── test_persistence.py
├── test_github.py
├── test_dashboard.py
├── test_e2e_cycle.py
├── conftest.py
└── _fixtures/
    ├── claude_shim.py        (extends reproductions/_harness::claude_shim)
    ├── gh_shim.py
    └── fake_repo.py
```

### 6.3 19 test gaps (from eng-review D7)

| Module | Gap | Fixture |
|---|---|---|
| scanner | happy path: 3 findings | claude_shim stream mode, scripted YAML |
| scanner | malformed YAML → retry | claude_shim first bad / then good |
| scanner | empty repo → tech-debt fallback | claude_shim returns 0 bugs, verify fallback call |
| scanner | bug_id stability across title reword | pure unit: same paths+kind → same hash |
| loop | full cycle E2E | conftest setup_all_shims |
| loop | reconciler clears zombies | pre-seed state.db with running/fixing rows |
| loop | 3-cycle skip gate | pre-seed fix_attempts(bug_id)=3 → assert skip |
| loop | meta blocklist gate | finding.paths=[".git/HEAD"] → assert skip, no attempt count |
| loop | pytest red blocks auto-merge | shim pytest exit=1 → assert PR route |
| loop | mypy/ruff red blocks auto-merge | shim mypy/ruff exit=1 → assert PR route |
| loop | Coder-1 fail → finding fail | claude_shim T-1 phase api_error → no half-ship |
| loop | Coder-2 sees diff_1 | claude_shim captures prompt → assert "Diff:" contains diff_1 |
| persistence | WAL mode init verify | post-init PRAGMA returns "wal" |
| persistence | concurrent writes | thread test: 2 threads same bug_id → 1 row |
| persistence | fix_attempts counts | record_finding 3× same bug_id → returns 3 |
| github | open_pr argv shape | gh_shim record argv → assert `pr create --title ...` |
| github | auto_merge PRResult.action | gh_shim variants → assert action in {merged-now, queued, failed} |
| github | sync_pr_states sync | gh_shim mixed states → assert persistence reflects |
| dashboard | / renders non-empty | TestClient + seeded cycle → 200 + "Live Cycle" |
| dashboard | /api/live idle state | TestClient empty db → 200 {cycle:null, status:idle} not 404 |
| dashboard | /api/trend perf | seed 1000 rows → response < 100ms |

### 6.4 Coverage targets

| Module | Target |
|---|---|
| scanner.py | 80% |
| loop.py | 75% |
| persistence.py | 90% |
| github.py | 85% |
| dashboard/server.py | 70% (HTML templates via snapshot) |

### 6.5 CI

New `.github/workflows/ci.yml` (created manually at build time; NOT a target
of loop's auto-merge — blocklist protects it from runtime modification):

```yaml
name: ci
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v3
      - run: uv sync --dev
      - run: uv run pytest tests/ --cov=pm_agent --cov-fail-under=75
      - run: uv run mypy pm_agent/
      - run: uv run ruff check pm_agent/
```

### 6.6 LLM eval

Not in scope. pm-agent has no prompt-template under continuous scanner
modification. Planner system prompt was hardened during the 39-bug fix phase.

---

## 7. Demo Orchestration

### 7.1 Build timeline (Days 1-14)

| Day | Work | Verification |
|---|---|---|
| 1-2 | scanner.py + persistence.py | unit tests + WAL init |
| 3-4 | loop.py + reconciler + serial Coder logic | unit + first mock E2E |
| 5 | github.py (gh_shim first) | unit |
| 6-7 | dashboard/ FastAPI + HTMX templates | TestClient + manual browser open |
| 8-9 | finish 19 unit + 1 E2E + CI workflow | `uv run pytest` green; CI green |
| 10 | pre-demo dry run #1 (~6h real LLM) | observation summary |
| 11 | fix issues from #1 | repros still green |
| 12 | pre-demo dry run #2 (~6h) | 0 critical issues |
| 13 | overnight dry run starts (~6pm Day 13) — runs ~14-16h | Day 14 morning dashboard sanity check |
| 14 | **demo day** | demo @ Day 14 afternoon → 14-20h of cycle data, 1-2 live cycles |

### 7.2 Demo-day choreography (9 beats)

```
Beat 1  Opening (30s)
        Open dashboard browser.
        "This is its state after running since ~8pm last night."
        24h chart should show ~25 data points + cumulative cost ~$15.

Beat 2  Live Cycle top panel (45s)
        Point to current running cycle (if mid-cycle).
        "Right now: scan found 3 findings; on #2, Coder-1 committed,
         Coder-2 writing test."
        If idle: wait for next 30min tick OR trigger via Beat 5.

Beat 3  GitHub PR list (90s)
        Switch to GitHub /pulls.
        "Auto-merged 7 Low-severity last night; these 3 High are awaiting you."
        Open a High PR: "Body includes acceptance + failed gate output
         if relevant — you see why it routed to review."
        Open an auto-merged: show description + commit graph.

Beat 4  Architecture walkthrough (90s)
        Switch to IDE or ASCII diagram.
        Point to scanner / loop / persistence / github / dashboard.
        Surface key design decisions:
          - reconciler (crash-safe)
          - triple gate (pytest + mypy + ruff)
          - 3-cycle skip
          - Coder serial (Coder-2 sees Coder-1's diff)

Beat 5  Live trigger (60s, optional)
        `pm-agent loop trigger-now`
        Watch dashboard Live Cycle: scan → Coder-1 → Coder-2 → integrate → route.
        Risk: API latency may push to 3-5 min. Have Beat 5b ready.

Beat 5b Fallback if live trigger drags:
        `pm-agent loop trigger-now --inject scanner-empty`
        "Scanner found 0 bugs → auto-falls back to tech-debt → found N improvements."

Beat 6  Failure recovery (60s)
        Switch to dashboard cycle history.
        Point to a failed cycle: "Coder-2 timed out here. Finding marked failed,
         not shipped; next cycle retries."
        Point to a skipped finding: "This bug failed 3 cycles in a row.
         3-cycle gate skipped it and alerted."

Beat 7  Cost transparency (30s)
        Point to dashboard cumulative cost banner: $XX.XX.
        "14 hours, 25 cycles, ~$0.60/cycle average, 9 PRs shipped."

Beat 8  Known limits (45s) — say these proactively:
          - LLM-on-LLM blindspot: complex concurrency bugs may stay invisible
          - 39 repros are regression, not correctness oracle → that's why mypy + ruff layered in
          - Auto-merge limited to Low/hygiene; important decisions remain yours
          - Single-machine demo; no production supervision/rollback

Beat 9  Q&A
```

### 7.3 Plan B / Plan C

| Failure | Fallback | Switch time |
|---|---|---|
| Dashboard browser broken | TUI mode + `--inject-fault planner-yaml` | 30s |
| Live trigger stuck on API | Extend Beat 3 (PR walkthrough) | immediate |
| Overnight dry run failed | Architecture walkthrough + dev screenshots | 5 min |
| Daemon won't start | Pre-recorded screencast of dry run #2 | immediate |

### 7.4 Pre-demo deliverables (must exist before Day 14)

- Pre-demo dry run #2 full screencast saved
- Dashboard screenshots (cumulative cost, Live Cycle, 24h chart) saved
- Pre-staged 3 PRs in case 24h dry run produces nothing (gray-area —
  only use if real run is fully dead)

### 7.5 Budget allocation

| Phase | Estimate |
|---|---|
| Days 1-9 build (unit/integration via shim) | ~$2 |
| Day 10 dry run #1 (6h) | ~$3 |
| Day 11 fixes | ~$1 |
| Day 12 dry run #2 (6h) | ~$3 |
| Day 13 24h dry run | ~$15-25 |
| Day 14 live trigger (1-2 cycles) | ~$1-3 |
| Buffer | ~$30-50 |
| **Total** | **~$55-85 (under $100 cap)** |

### 7.6 Seed bug source

Scanner finds work organically in the 1000+ LOC of new modules added during
build (Days 1-9). No artificial seeding needed. Dev safety valve:
`pm-agent loop trigger-now --seed-tech-debt` forces tech-debt fallback if
scanner hits 0 bugs.

### 7.7 Success criteria

| Dimension | Minimum | Ideal |
|---|---|---|
| 24h trend shows continuous activity | required | 25+ cycles, 8+ PRs |
| Live Cycle panel populated | required | dynamic progress visible |
| ≥ 1 auto-merged PR | required | 5+ auto-merged |
| ≥ 1 human-review PR | required | 2-3 High severity |
| Cost transparency | required | < $25 |
| Architecture explains cleanly | required | mentor doesn't interrupt |
| Live trigger works | nice | ✓ |
| Failure recovery demo | nice | ✓ |

---

## 8. NOT in Scope

- Webhook-based GitHub PR state sync (polling at cycle start is enough)
- Read-only TUI watch mode (dashboard Live Cycle panel covers it)
- Third-agent Test-Validator (serial Coder-1 → Coder-2 with diff is enough)
- LLM-based bug dedup (paths+kind hash is enough for 3-cycle skip)
- Per-cycle cost cap (user rejected; dashboard cumulative cost is the gate)
- Webhook / push notifications during demo (manual dashboard refresh)
- Seed-bug injection mechanism (organic scanner work is sufficient)
- Plugin / multi-repo extension (Approach A1 single-package; v2)
- Automatic rollback of bad auto-merges (v2; manual `git revert` for now)
- Daemon supervision (systemd/launchd) — v2; manual restart for demo

---

## 9. What Already Exists (reused)

| Existing module | Reuse pattern |
|---|---|
| `planner.py` retry loop + YAML parse + system prompt skeleton | scanner.py mirrors structure |
| `runner.py` run_claude_async / DEVNULL stderr / cancel safety | scanner + loop call directly |
| `worktree.py` create / integrate / acleanup_worktree / flock | loop reuses fix pipeline whole — no rewrite |
| `tasks.py` CoderTask | Finding → CoderTask transform target shape |
| `tests/test_repros.py` pytest harness | core of auto-merge gate (+1 of 3) |
| `_safe()` markup escape helper | dashboard templates / log path |
| BUGS.md / reproductions/ format | scanner YAML output mirrors |
| `--inject-fault` patterns (planner-yaml / coder-timeout / api-error) | loop borrows for dev `--inject` switch |

---

## 10. Decisions Log

| # | Decision | Section |
|---|---|---|
| D1 | risk.py folded into scanner.py | §3 |
| D2 | reconciler runs at daemon startup, clears zombies | §3, §4 |
| D3 | SQLite WAL mode + wrapped connection pool | §3 |
| D4 / D11 | bug_id = sha1(kind + sorted(paths))[:8] | §3 |
| D5 | sync_pr_states (renamed from sync_open_prs) at cycle start | §3, §4 |
| D6 / D10 | Serial Coder-1 then Coder-2 with diff visible | §3, §4 |
| D7 | 19 specific test gaps listed in spec | §6 |
| D8 | Skip codex outside voice, use subagent | (process) |
| D9 | Auto-merge gate = pytest + mypy + ruff (triple) | §4 |
| D12 | Dashboard top "Live Cycle" panel | §3, §7 |
| §2 contract revisions: (1) scan returns (list, cost); (2) on_retry hook; (3) bug_id includes kind; (4) init_db returns None + @transaction; (5) PRResult.action; (6) Pydantic dashboard responses; (7) blocklist fnmatch semantics | §3 |
| Section 3 deltas: (8) build_coder_tasks(prior_diff=); (9) interrupted counts fix_attempts; (10) Coder-2 fail → whole finding fail; (11) wm.delete_branch after each finding; (12) cycle.status enum; (13) sync_pr_states rename; (14) gates-red PR body includes failed output | §4 |

---

## 11. Open Questions (deferred to writing-plans phase)

- Exact prompt text for scanner.py system message (chaos-qa-hunter-style; tune in build phase)
- Dashboard CSS / layout details (deferred to UI implementation)
- gh_shim API surface specifics (build-phase decision)
- Test seed data generators (covered partially in §6.3)
