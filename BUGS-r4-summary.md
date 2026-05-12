# Round 4 — Dry-run #1 findings + fixes

> 2h dogfood dry-run on `--repo .` surfaced 2 real bugs in the Beta
> autonomous loop. Both fixed within the same day with permanent repros.
> Run date: 2026-05-12. Dry-run observation note: `docs/superpowers/observations/2026-05-12-dry-run-1.md`.

## Headline

**2 net-new findings; 2 High/Medium fixed; 0 archived.**

Both fixes have a permanent repro under `reproductions/r4/bug_R4_*.py`
that exits 1 (NOT-REPRODUCED) on the fixed codebase. `pytest tests/`
auto-discovers them via `tests/test_repros.py`, so any regression will
flip them back to exit 0.

## Test gate before / after R4

| Phase | Tests | mypy (CI scope) | ruff (CI scope) |
|---|---|---|---|
| Before R4 (post R3) | 146 | clean (9 mods) | clean |
| After R4 fix wave | 150 | clean (9 mods) | clean |

4 net-new tests (2 repros + 2 unit tests).

## Fixed (2)

| ID | Severity | Title | Commit | Repro |
|---|---|---|---|---|
| R4-1 | High | `prs.github_number` UNIQUE collides on 2nd failed `gh pr create` per cycle | `4bda663` | `bug_R4_1_pr_unique_on_failed_creates.py` |
| R4-2 | Medium | `pm-agent loop report` coder cost is always N/A (agent name mismatch `coder` vs `coder-1`/`coder-2`) | `b3b8fc7` | `bug_R4_2_coder_cost_agent_mismatch.py` |

## What Round 4 told us

- **The cycle-loop's per-finding error isolation works** — the UNIQUE
  crash never propagated past the finding it hit, even when it fired
  5× across 3 cycles. R3-A-02 `asyncio.shield` cleanup paid off here.
- **Defense in depth on persistence layer is cheap** — the R4-1 fix
  added a `gh_number <= 0` guard in `record_pr` *and* the call-site
  guard in `loop.py`. Either alone would prevent the symptom, but
  having both means a future caller that forgets the action check
  cannot recreate the bug.
- **Tests can lie when they don't match reality** — `test_report.py`
  seeded `agent='coder'` (singular), exactly the key the production
  bug looked up. The test passed because seed + bug shared the same
  wrong assumption. Lesson: when adding cost-aggregation tests in
  the future, seed the same agent names the production code writes.
- **First-cycle bug density on `--repo .` is much higher than seed**
  — 5 findings/cycle on pm-agent dogfood vs the runbook's ~1-2
  expected on the seed repo. Updated the cost estimate in the
  observation note; next dry-run should either pre-stipulate a higher
  budget or default `--repo` to the seed.

## Preflight side-fix (separate)

Also fixed during launch preflight (commit `1e998c2`):

- **`check_gh_auth` 5s timeout** was too tight for macOS Keychain
  unlock latency on the first `gh auth status` call after login.
  Widened to 15s. Not counted in the R4 fix tally because it didn't
  surface from a real-LLM cycle — operator hit it before the daemon
  even started.

## Reproducibility

```bash
cd /Users/zmy/intership/5/agenter/pm-agent
git log --oneline e175b18..HEAD          # R4 commits
uv run pytest tests/ -q                   # 150 passed
for f in reproductions/r4/bug_*.py; do
  uv run python "$f" >/dev/null 2>&1
  echo "$(basename "$f"): exit=$?"
done
# both must print: exit=1
```

## Decision

- [x] Block on R4-1 + R4-2 cleared.
- [x] Both fixes verified by repro + unit test + full pytest.
- [ ] Ready to schedule dry-run #2 (run from `feat/beta-autonomous-loop`
      with R4 fixes applied; same `--repo .` for like-for-like compare).
- [ ] Ready to merge `feat/beta-autonomous-loop` → `main` once dry-run
      #2 produces a clean report.
