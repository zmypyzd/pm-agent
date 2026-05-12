# Round 3 Chaos QA — Summary

> Adversarial review of the 5 new Beta modules added in Tasks 1-9.
> Run date: 2026-05-12. 3 parallel Opus hunters, 4 parallel fixers.

## Headline

**35 net-new findings; 10 High fixed; 25 Medium/Low archived for future rounds.**

All 10 fixed bugs have a permanent repro under `reproductions/r3/bug_R3_*.py`
that exits 1 (NOT-REPRODUCED) on the fixed codebase. The full pytest suite
auto-discovers them, so any regression will flip them back to exit 0.

## Test gate before / after R3

| Phase | Tests | mypy | ruff |
|---|---|---|---|
| Before R3 (post Task 9 + scaffolding) | 120 | clean (9 mods) | clean |
| After R3 fix wave | 146 | clean (9 mods) | clean |

26 net-new tests (10 repros + 16 regression unit tests across all 4 fixers).

## Fixed (10)

| ID | Severity | Title | Commit | Repro |
|---|---|---|---|---|
| R3-A-01 + C-07 | High | `--interval-s 0` busy-loop / no validation on numeric flags | `ef44c7c` | `bug_R3_A_01.py` |
| R3-A-02 | High | Per-finding `finally` cleanup leaks worktree+branch when SIGTERM lands mid-cleanup | `a8e7b70` | `bug_R3_A_02.py` |
| R3-B-03 | High | `auto_merge` mis-classifies "already enabled" stderr as failure | `722bcc8` | `bug_R3_B_03.py` |
| R3-B-05 | High | `_gh` subprocess inherits stdin → 30s hang on interactive prompts | `722bcc8` | `bug_R3_B_05.py` |
| R3-C-01 + C-02 | High | Dashboard Live Cycle + cumulative banner read `cycles.cost_usd` (zero during running cycle) instead of summing `costs` table | `a968e87` | `bug_R3_C_01.py` |
| R3-C-03 | High | Findings list ORDER BY id ASC → operator sees oldest 50, hides newest | `a968e87` | `bug_R3_C_03.py` |
| R3-C-04 | Medium | `cmd_loop_run` always returned 130, even on graceful shutdown | `ef44c7c` | `bug_R3_C_04.py` |
| R3-C-05 | High | `--db ~/foo` created a literal `~/` directory in CWD (no expanduser) | `ef44c7c` | `bug_R3_C_05.py` |
| R3-C-06 | High | `dashboard serve` standalone never called `init_db` → endpoints reported "idle" forever | `ef44c7c` | `bug_R3_C_06.py` |

## Archived (25) — file but don't fix this session

These are Medium/Low; they're known and tracked, but fixing them now is scope
creep before Tasks 10/11 dry-runs. See `BUGS-r3-A.md`, `BUGS-r3-B.md`,
`BUGS-r3-C.md` for full triage notes.

| Source | IDs |
|---|---|
| Hunter A (persistence + loop) | R3-A-03, A-04, A-05, A-07, A-08, A-09, A-10, A-11, A-12 |
| Hunter B (github) | R3-B-01, B-02, B-04, B-06, B-07, B-08, B-09, B-10, B-11, B-12, B-13 |
| Hunter C (dashboard + CLI) | R3-C-08, C-09, C-10, C-11, C-12 |

If dry-run #1 surfaces any of these archived issues in real-LLM behavior,
prioritize the matching ID into a follow-up wave on Day 11.

## What Round 3 told us about the Beta code

- **Cancellation safety** is non-obvious: the Task 8 fix handled cycle-level
  CancelledError, but per-finding cleanup needed the same treatment
  (R3-A-02). When CancelledError can interleave with cleanup awaits, every
  `try/finally` chain in the daemon needs `asyncio.shield` per step.
- **Cost surface is bifurcated**: `cycles.cost_usd` (write-on-finish) +
  `costs` table (write-per-agent) co-exist. The dashboard read the wrong one
  for live data. Same lesson applies to any future "show me what's
  happening RIGHT NOW" feature.
- **CLI argparse hygiene** is its own attack surface — defaults `type=int`
  and `type=Path` leak unsafe values through to the daemon. Three of the
  10 fixes were argparse validators.
- **gh CLI is more dynamic than the wrapper assumed**: auth-marker matching
  and queued-state phrasing both surface as a moving target. The
  consolidation of marker lists into named constants paid off here.

## Reproducibility

```bash
cd /Users/zmy/intership/5/agenter/pm-agent
git log --oneline a968e87..ef44c7c    # 4 R3 fix commits
uv run pytest tests/ -q               # 146 passed
for f in reproductions/r3/bug_*.py; do uv run python "$f" >/dev/null 2>&1; echo "$(basename $f): $?"; done
# all 10 must print: exit=1
```
