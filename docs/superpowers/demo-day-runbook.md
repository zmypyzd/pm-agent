# Demo Day Runbook (Task 11 — Day 14)

Hand-script for the 9-beat demo. Spec source: §7.2 / §7.3. ~7 min total.

## T-30 min — final prep

```bash
cd /Users/zmy/intership/5/agenter/pm-agent

# Verify overnight daemon still alive
ps -p $(cat /tmp/loop.pid) && echo "✅ daemon up" || echo "❌ daemon DEAD — go to Plan C"

# Take baseline screenshots BEFORE demo (in case live state breaks)
uv run pm-agent loop report > docs/superpowers/demo-evidence/pre-demo-report.txt
cp /tmp/loop-overnight.log docs/superpowers/demo-evidence/loop-log-pre-demo.txt

# macOS: screencapture for the 3 dashboard panels
screencapture -W docs/superpowers/demo-evidence/dashboard-cost-banner.png
screencapture -W docs/superpowers/demo-evidence/dashboard-live-cycle.png
screencapture -W docs/superpowers/demo-evidence/dashboard-24h-chart.png
```

Have these ready in tabs:
- Browser tab 1: `http://127.0.0.1:8000` (live dashboard)
- Browser tab 2: GitHub `/pulls` for the dry-run repo
- Browser tab 3: One opened High-severity PR (from overnight run)
- Browser tab 4: One opened auto-merged PR
- Terminal: pre-typed `uv run pm-agent loop trigger-now` (Beat 5) and fallback

## The 9 beats

### Beat 1 — Opening (30s)
> "This is its state after running since ~6pm last night."

Show: dashboard 24h chart (should be ~25 data points) + cumulative cost banner (~$15).

### Beat 2 — Live Cycle (45s)
> "Right now: scan found N findings; on #M, Coder-1 committed, Coder-2 writing test."

Point to live cycle panel. If idle, say so honestly and tee up Beat 5.

### Beat 3 — GitHub PR list (90s)
> "Auto-merged N Low-severity last night; these M High are awaiting you."

Switch to GitHub tab. Open a High PR — point to acceptance criteria + failed gate
output in body. Then open an auto-merged PR — show description + commits.

### Beat 4 — Architecture (90s)
> "Five modules, single SQLite DB, one daemon loop. Here are the design points
> that make it crash-safe."

Switch to IDE or pre-rendered diagram. Surface:
- reconciler on startup (state.db reconciler in `pm_agent/persistence.py`)
- triple gate (pytest + mypy + ruff in `pm_agent/loop.py`)
- 3-cycle skip + alert
- Coder serial chain (Coder-2 sees Coder-1's diff)

### Beat 5 — Live trigger (60s, optional)

```bash
uv run pm-agent loop trigger-now
```

Switch to dashboard live panel and narrate scan → Coder-1 → Coder-2 → integrate → route.

**Risk:** API latency may push to 3-5 min. If clock hits 2 min still on scan:

### Beat 5b — Fallback

```bash
uv run pm-agent loop trigger-now --inject scanner-empty
```

> "Scanner found 0 bugs → auto-falls back to tech-debt → found N improvements."

### Beat 6 — Failure recovery (60s)

Dashboard cycle history panel. Point to:
- A failed cycle: "Coder-2 timed out — finding marked failed, not shipped; next cycle retries."
- A skipped finding (3-cycle gate hit): "This bug failed 3 cycles in a row — system skipped and alerted."

### Beat 7 — Cost transparency (30s)

Dashboard cumulative cost banner.
> "14 hours, N cycles, ~$0.60/cycle average, M PRs shipped."

### Beat 8 — Known limits (45s, **say proactively**)

- LLM-on-LLM blindspot: complex concurrency bugs may stay invisible
- 39 repros are regression, not correctness oracle → that's why mypy + ruff layered in
- Auto-merge limited to Low/hygiene; important decisions remain yours
- Single-machine demo; no production supervision/rollback

### Beat 9 — Q&A

## Plan B / C (per spec §7.3)

| Failure | Switch to | Time to switch |
|---|---|---|
| Dashboard browser broken | TUI mode + `pm-agent loop trigger-now --inject planner-yaml` | 30s |
| Live trigger stuck on API | Extend Beat 3 (open more PRs from overnight) | immediate |
| Overnight dry run failed | Architecture walkthrough + Day 12 screencast | 5 min |
| Daemon won't start | Pre-recorded screencast of dry run #2 | immediate |

## Post-demo wrap

```bash
# Stop daemons cleanly
kill -TERM $(cat /tmp/loop.pid) $(cat /tmp/dashboard.pid)
sleep 5
uv run pm-agent loop report > docs/superpowers/demo-evidence/post-demo-report.txt

# Archive run artifacts
mv ~/.pm-agent/runs docs/superpowers/demo-evidence/runs-archive-day14 2>/dev/null || true
git add docs/superpowers/demo-evidence/
git commit -m "docs: archive demo-day evidence (screenshots + log + run artifacts)"
git push
```

## Honest demo posture

The mentor knows you built a Beta-grade tool in 14 days. Don't oversell.
Beat 8's known-limits list is the credibility moment — say them before the
mentor finds them.
