# Round 5 — Dry-run #3 follow-ups

> 2h dry-run #3 on `--repo .` (2026-05-13) was the first end-to-end
> success path: 3 real PRs landed on the remote, $4.59, 0 errors.
> Round 5 was opened to file the two ergonomic follow-ups from that
> observation. Net result: 1 fixed, 1 withdrawn.
> Dry-run observation: `docs/superpowers/observations/2026-05-13-dry-run-3.md`.

## Headline

**1 fixed, 1 withdrawn, 0 net-new debt.**

R5 did not need its own dry-run wave because both candidate issues
were observable in D3's existing artifacts.

## Test gate before / after R5

| Phase | Tests | mypy (CI scope) | ruff (CI scope) |
|---|---|---|---|
| Before R5 (post R4) | 153 | clean | clean |
| After R5 fix wave   | 158 | clean | clean |

5 net-new tests (4 persistence + 1 loop structural assert), all in
the R5-2 fix.

## Disposition

| ID | Severity | Title | Outcome | Commit |
|---|---|---|---|---|
| R5-1 | Low | `auto_merge` may exit 0 on a fresh repo without actually queueing | **Withdrawn — not reproducible** | — |
| R5-2 | Low | Same `bug_id` re-found across cycles opens duplicate PRs | **Fixed** | `d895f17` |

## R5-1 — why withdrawn

Initial observation (from D3 notes):

> `gh pr merge --auto --squash` on a repo with no branch protection
> exits 0 without enabling auto-merge; the PR shows OPEN. The loop
> can't distinguish "queued successfully" from "no-op declined."

Closer inspection of D3's `pm-agent loop report`:

- 3 PRs opened, 0 auto-merged, 3 marked `human-review`.
- All 3 PRs took the `gates_failed → open_pr` route, **not**
  `gates_green → auto_merge`. The auto_merge branch never executed.
- The "auto_merge can't tell queued from declined" failure mode was
  inferred from documentation, not from a real D3 trace.

This is a hypothetical bug that would only matter on a future
`gates_green` PR against a fresh GitHub repo. Not worth a fix until
we see it bite. Re-open if a real run produces a `gates_green` PR
that silently sits OPEN without auto-merge actually being queued.

## R5-2 — what was fixed

D3 cycle 1 and cycle 3 both surfaced bug_id `35a1d02f` (BUGS file
sprawl), and both produced live PRs against the same upstream
location. `open_pr`'s idempotency key was the head branch, which
embeds `cycle_id` — different cycles got different branches, gh saw
no existing PR to reuse, and ~$0.8 was burned re-running the full
Coder-1 + Coder-2 + integration pipeline on a duplicate finding.

Fix (commit `d895f17`):

- `persistence.find_open_pr_for_bug(bug_id) -> int | None` joins
  `findings → prs` and returns the newest `github_number` where
  `state = 'open'`. Merged/closed PRs return None so the bug is
  eligible for a fresh attempt next cycle.
- `loop.run_one_cycle` calls the helper at the *very top* of the
  per-finding loop, before fix_attempts and blocklist gates. Non-None
  return → finding marked skipped with a log line referencing the
  existing PR number; the Coder pipeline never spawns.

Tests added (in `d895f17`):

- persistence: no-match returns None
- persistence: open PR for bug returns its number
- persistence: merged or closed PR is not blocking (returns None)
- persistence: multiple open PRs (pre-fix state) returns newest
- loop: structural assert — gate exists and runs before Coder spawn

No new repro under `reproductions/r5/` because the failure mode is
cross-cycle (requires two real scanner runs to reproduce) — the
persistence-level unit tests pin the contract instead.

## What Round 5 told us

- **Filing discipline matters.** R5-1 looked plausible from a
  documentation reading but didn't survive a trace re-check. Future
  rule: a finding from a dry-run observation needs at least one
  concrete artifact in the run record before it gets a fix scoped.
- **Cross-cycle idempotency is a new design surface.** Pre-R5, the
  loop treated each cycle as independent. R5-2 introduced the first
  state-aware skip — same pattern will likely reappear for
  "previously-merged-but-regression" detection.
- **No dry-run wave was needed.** Both R5 issues lived inside D3's
  existing data. Saves $5-$10 vs running R5-specific dogfood.

## Decision

- [x] R5-2 fixed; tests pin the contract.
- [x] R5-1 withdrawn; documented above so the next operator who sees
      auto_merge-OPEN can compare against the trace and decide if
      it's a new occurrence or the same hypothetical.
- [x] No follow-up dry-run scheduled. Re-open Round 6 only if a
      real run produces a `gates_green` PR that fails to auto-merge.
