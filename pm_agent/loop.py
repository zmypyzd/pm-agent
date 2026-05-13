"""Daemon orchestration for the Beta autonomous bug-fix loop.

Per spec §3 LoopConfig + §4 cycle lifecycle.

Cycle: reconcile (once at daemon start) → sync_pr_states → scan →
       for finding: gates (3-cycle / blocklist) → Coder-1 → Coder-2(with diff)
       → integrate → pytest+mypy+ruff → PR or auto-merge → cleanup (try/finally).
"""
from __future__ import annotations

import asyncio
import logging
import signal
import subprocess
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path

from pm_agent import github, persistence, scanner
from pm_agent.runner import run_claude_async
from pm_agent.scanner import Finding
from pm_agent.tasks import CoderTask
from pm_agent.worktree import WorktreeManager

log = logging.getLogger(__name__)

STATE_DB: Path = Path.home() / ".pm-agent" / "state.db"


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
        if code_paths:
            base = Path(code_paths[0]).stem
            test_paths = [f"tests/test_{base}.py"]
        else:
            test_paths = ["tests/test_added.py"]
    if not code_paths:
        code_paths = paths

    paths_block_1 = "\n".join(f"    - {p}" for p in code_paths) or "    (any)"
    accept_block = "\n".join(f"    - {a}" for a in finding.acceptance) or "    (none)"
    paths_block_2 = "\n".join(f"    - {p}" for p in test_paths)

    t1_prompt = (
        CODER_CODE_PROMPT_PREFIX.format(
            bug_id=finding.bug_id, paths_block=paths_block_1, accept_block=accept_block,
        )
        + f"Fix this: {finding.title}\n\nEvidence:\n{finding.evidence}\n"
    )
    t2_prompt = CODER_TEST_PROMPT_PREFIX.format(
        bug_id=finding.bug_id,
        prior_diff=(prior_diff if prior_diff is not None else "(not yet available)"),
        paths_block=paths_block_2,
        accept_block=accept_block,
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
    if not pyt_ok:
        parts.append(f"### pytest (failed)\n```\n{pyt_out}\n```")
    if not my_ok:
        parts.append(f"### mypy (failed)\n```\n{my_out}\n```")
    if not ru_ok:
        parts.append(f"### ruff (failed)\n```\n{ru_out}\n```")
    return False, "\n\n".join(parts)


async def _drive_coder(task: CoderTask, wt_path: Path, timeout: float) -> tuple[bool, float]:
    """Run claude in wt_path; return (clean_exit, cost_usd)."""
    from pm_agent.tui import CODER_COMMIT_SUFFIX
    full_prompt = task.prompt + CODER_COMMIT_SUFFIX.format(task_id=task.id)
    saw_error = False
    cost = 0.0
    async for ev in run_claude_async(
        full_prompt, cwd=str(wt_path), unrestricted=True, timeout=timeout,
    ):
        et = ev.get("type")
        st = ev.get("subtype")
        if et == "result":
            cost += float(ev.get("total_cost_usd") or 0.0)
            if ev.get("is_error"):
                saw_error = True
        elif et == "system" and st in ("timeout", "spawn_error"):
            saw_error = True
    return (not saw_error), cost


async def run_one_cycle(
    repo: Path,
    cfg: LoopConfig,
    stop_event: asyncio.Event | None = None,
) -> CycleResult:
    """One pass per spec §4. stop_event checked at multiple points (spec F3)."""
    import time
    t0 = time.time()
    cycle_id = persistence.start_cycle()
    total_cost = 0.0
    findings_total = findings_fixed = findings_skipped = 0
    broke_early = False
    cycle_status: persistence.CycleStatus = "done"
    cycle_finished = False  # guard against double finish_cycle

    def _ensure_finished(status: persistence.CycleStatus) -> None:
        """Idempotent: only finish_cycle once per cycle."""
        nonlocal cycle_finished
        if not cycle_finished:
            try:
                persistence.finish_cycle(cycle_id, status, total_cost)
            except Exception:
                log.exception("finish_cycle failed for cycle %s", cycle_id)
            cycle_finished = True

    try:
        if stop_event and stop_event.is_set():
            _ensure_finished("done")
            return CycleResult(cycle_id, 0, 0, 0, 0.0, time.time() - t0)

        # 1. sync PR states
        try:
            states = await github.sync_pr_states()
            for s in states:
                persistence.update_pr_state(s.number, s.state)
        except github.GhAuthError:
            log.error("gh auth failure — halting")
            _ensure_finished("errored")
            raise

        if stop_event and stop_event.is_set():
            _ensure_finished("done")
            return CycleResult(cycle_id, 0, 0, 0, 0.0, time.time() - t0)

        # 2. scan
        try:
            findings, scan_cost = await scanner.scan(repo, max_retries=cfg.max_retries)
            persistence.record_cost(cycle_id, "scanner", scan_cost)
            total_cost += scan_cost
        except Exception:
            log.exception("scanner crashed")
            _ensure_finished("errored")
            return CycleResult(cycle_id, 0, 0, 0, total_cost, time.time() - t0)

        findings_total = len(findings)
        if not findings:
            _ensure_finished("scan-empty")
            return CycleResult(cycle_id, 0, 0, 0, total_cost, time.time() - t0)

        wm = WorktreeManager(repo)
        # 3. per-finding loop
        for finding in findings:
            if stop_event and stop_event.is_set():
                broke_early = True
                break
            finding_id = persistence.record_finding(cycle_id, finding)
            t1: CoderTask | None = None
            t2: CoderTask | None = None
            ig = None
            try:
                # Gate (0) BUG-R5-2: duplicate-PR skip. Cheap query — if an
                # OPEN PR already exists for this bug_id (re-found by a
                # later cycle's scanner), don't spawn the Coder pipeline.
                # Saves a full Coder-1+Coder-2+integration spend per
                # duplicate and prevents PR sprawl on the remote. Runs
                # BEFORE the 3-attempt / blocklist gates so it short-
                # circuits as early as possible.
                existing_pr = persistence.find_open_pr_for_bug(finding.bug_id)
                if existing_pr is not None:
                    persistence.update_finding(finding_id, "skipped")
                    log.info(
                        "skipping %s — open PR #%d already covers this bug",
                        finding.bug_id, existing_pr,
                    )
                    findings_skipped += 1
                    continue
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
                ok, c1_cost = await _drive_coder(t1, repo / ".pm-agent-worktrees" / t1.id, cfg.coder_timeout)
                persistence.record_cost(cycle_id, "coder-1", c1_cost)
                total_cost += c1_cost
                if not ok:
                    persistence.update_finding(finding_id, "failed")
                    continue
                diff_1 = await asyncio.to_thread(wm.diff_against_base, t1.id)
                if not diff_1.strip():
                    persistence.update_finding(finding_id, "failed")  # NO_CHANGES
                    continue

                # Coder-2 with diff
                persistence.update_finding(finding_id, "fixing-test")
                _t1_again, t2 = build_coder_tasks(finding, prior_diff=diff_1)
                await wm.acreate(t2.id)
                ok, c2_cost = await _drive_coder(t2, repo / ".pm-agent-worktrees" / t2.id, cfg.coder_timeout)
                persistence.record_cost(cycle_id, "coder-2", c2_cost)
                total_cost += c2_cost
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

                # Gate (c) pytest + mypy + ruff — run against integration worktree
                persistence.update_finding(finding_id, "testing")
                gates_green, gate_output = await asyncio.to_thread(run_gates, ig.worktree_path)

                # Route
                persistence.update_finding(finding_id, "routing")
                pr_branch = ig.branch
                # Push the integration branch so gh pr create can reference
                # it. No-op when no origin is configured — downstream
                # gh pr create then fails and the R4-1 guard below marks
                # the finding failed. A real push failure (auth, network,
                # protected ref) marks the finding failed immediately,
                # without spending more on retries.
                push_ok, push_err = await github.push_branch_to_origin(repo, pr_branch)
                if not push_ok:
                    log.warning(
                        "git push failed for %s: %s — marking finding %s failed",
                        pr_branch, push_err, finding.bug_id,
                    )
                    persistence.update_finding(finding_id, "failed")
                    continue
                if finding.severity in ("Critical", "High"):
                    pr = await github.open_pr(
                        pr_branch, finding,
                        body_extras=gate_output if not gates_green else "",
                    )
                elif gates_green:
                    pr = await github.auto_merge(pr_branch, finding)
                else:
                    pr = await github.open_pr(pr_branch, finding, body_extras=gate_output)
                # BUG-R4-1: a failed gh-pr-create returns
                # PRResult(action="failed", number=0). Previously we still
                # called record_pr (collision on UNIQUE github_number when
                # a second PR also failed) and update_finding(..., "done")
                # and bumped findings_fixed — counting the failure as a fix.
                # Treat failed PR routing as a finding-level failure.
                if pr.action == "failed":
                    persistence.update_finding(finding_id, "failed")
                    continue
                persistence.record_pr(
                    finding_id, pr.number, pr.url, state="open", action=pr.action,
                )
                persistence.update_finding(finding_id, "done")
                findings_fixed += 1
            except Exception:
                log.exception("finding %s raised unexpected exception; marking failed", finding.bug_id)
                try:
                    persistence.update_finding(finding_id, "failed")
                except Exception:
                    pass  # persistence itself broken; finally still runs cleanup
                findings_skipped += 1  # count as skipped so totals balance
            finally:
                # uniform cleanup — spec F4
                # R3-A-02: shield each cleanup step from CancelledError so a
                # SIGTERM that lands mid-finally cannot skip the remaining
                # steps and leak worktrees/branches. CancelledError inherits
                # from BaseException in 3.11+, so plain `except Exception`
                # would let it propagate past the first cleanup. We collect
                # all cleanup coroutine factories, run each under a per-step
                # shield with its own try/except, then re-raise CancelledError
                # at the end if any step saw one — so the outer cycle still
                # marks itself 'aborted' via the existing handler.
                from collections.abc import Awaitable
                from typing import Callable

                cleanups: list[tuple[Callable[[], Awaitable[None]], str]] = []
                if t1 is not None:
                    t1_id = t1.id
                    cleanups.append((lambda: wm.acleanup_worktree(t1_id), "cleanup t1 worktree"))
                    cleanups.append((lambda: wm.adelete_branch(t1_id), "cleanup t1 branch"))
                if t2 is not None:
                    t2_id = t2.id
                    cleanups.append((lambda: wm.acleanup_worktree(t2_id), "cleanup t2 worktree"))
                    cleanups.append((lambda: wm.adelete_branch(t2_id), "cleanup t2 branch"))
                if ig is not None:
                    ig_local = ig
                    cleanups.append((lambda: wm.acleanup_integration(ig_local), "cleanup integration"))

                cancelled = False
                for factory, label in cleanups:
                    try:
                        await asyncio.shield(factory())
                    except asyncio.CancelledError:
                        cancelled = True
                    except Exception:
                        log.exception("%s failed for %s", label, finding.bug_id)
                if cancelled:
                    # All cleanups attempted; now honor the cancellation so
                    # the outer try/except in run_one_cycle marks the cycle
                    # 'aborted' and finish_cycle still runs.
                    raise asyncio.CancelledError()

        if broke_early:
            cycle_status = "aborted"
        _ensure_finished(cycle_status)
        return CycleResult(
            cycle_id=cycle_id,
            findings_total=findings_total,
            findings_fixed=findings_fixed,
            findings_skipped=findings_skipped,
            cost_usd=total_cost,
            duration_s=time.time() - t0,
        )
    except asyncio.CancelledError:
        # spec §5.2 / F3: cancellation must finish_cycle before propagating
        _ensure_finished("aborted")
        raise  # re-raise so caller sees cancellation
    finally:
        # Defensive: any other path that exited without finish_cycle
        _ensure_finished("aborted")


async def run_forever(repo: Path, cfg: LoopConfig | None = None) -> None:
    """Daemon entry point. Installs SIGINT/SIGTERM handlers; loops cycles."""
    cfg = cfg or LoopConfig()
    state_db = STATE_DB
    persistence.init_db(state_db)
    report = persistence.reconcile(repo)
    log.info("reconcile: %s", report)

    stop_event = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass  # Windows doesn't support signal handlers in asyncio

    while not stop_event.is_set():
        try:
            result = await run_one_cycle(repo, cfg, stop_event=stop_event)
            log.info(
                "cycle %d: %d findings, %d fixed, %d skipped, $%.4f, %.1fs",
                result.cycle_id, result.findings_total, result.findings_fixed,
                result.findings_skipped, result.cost_usd, result.duration_s,
            )
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
