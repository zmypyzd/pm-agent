# Dry Run #N — observations

> Copy this file to `YYYY-MM-DD-dry-run-N.md` (e.g. `2026-05-12-dry-run-1.md`)
> and fill in the blanks. Numbers come from `uv run pm-agent loop report`.

- **Date:** YYYY-MM-DD
- **Duration target:** 6h | 24h
- **Cycle interval:** 1800s (30 min) — set via `--interval-s`
- **Branch / commit:** main @ <SHA>
- **Operator:** <name>

## Headline

> One sentence the operator could text a mentor at midnight.

## Raw report

```
<paste output of `uv run pm-agent loop report` here verbatim>
```

## Issues observed

For each issue: short title, severity (Critical / High / Medium / Low), one-line
description, and where to find evidence (log line, dashboard panel, PR URL).

- **[severity]** Title — what happened. Evidence: /tmp/loop.log:NNN | PR #N | dashboard panel X.
- ...

If zero issues observed, write "None — clean run." and move on.

## Action items (for the next run)

Numbered list, each linkable to an issue above. Empty list means ship to the
next phase.

1. Fix: ...
2. Investigate: ...
3. Reduce: ...

## Cost vs budget

- Spent: $X.XX
- Budgeted for this phase: $Y.YY (see spec §7.5)
- On track for total ≤ $100 cap? Y/N

## Decision

- [ ] Ready for next phase (Day 11 fixes | Day 12 dry run #2 | Day 13 24h | Day 14 demo)
- [ ] Block on action items above
- [ ] Architectural change needed — escalate to plan revision
