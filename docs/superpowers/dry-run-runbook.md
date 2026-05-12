# Dry-run Runbook (Task 10 & 11)

One source of truth for launching, observing, and stopping a real-LLM dry run.
Pairs with `pm-agent loop preflight` + `pm-agent loop report`.

## Pre-launch (do this with eyes open)

```bash
cd /Users/zmy/intership/5/agenter/pm-agent

# 1. Be on main with a clean tree.
git checkout main && git pull
git status                       # must be clean

# 2. Nuke any prior state.
rm -rf ~/.pm-agent/state.db ~/.pm-agent/state.db-wal ~/.pm-agent/state.db-shm

# 3. 30-second sanity check.
uv run pm-agent loop preflight   # must end "✅ READY — all checks passed"
```

If preflight fails: read its line items, fix the root cause, re-run. Do **not**
launch with a FAIL — you'll burn LLM money on a misconfigured environment.

## Launch

```bash
# Foreground daemon, log to /tmp/loop.log
uv run pm-agent loop run --repo . --interval-s 1800 > /tmp/loop.log 2>&1 &
echo $! > /tmp/loop.pid

# Dashboard in a second terminal
uv run pm-agent dashboard serve --port 8000
# Open http://127.0.0.1:8000 in browser
```

`--interval-s 1800` = 30 min between cycle ticks. Six-hour run ≈ 12 scans;
twenty-four-hour run ≈ 48 scans.

## Hourly health check (during a 6h or 24h run)

```bash
uv run pm-agent loop report
```

Look for:

- `running` cycles count = 1 (the in-flight cycle). > 1 means a zombie from
  a prior crash — investigate before continuing.
- `errored + aborted` < 20% of `total`. Higher → environment instability,
  pause and read /tmp/loop.log.
- Cumulative cost on track (~$0.50–$1.00/cycle for the seed repo).

If anything looks pathological:

```bash
tail -100 /tmp/loop.log
```

## Stop

```bash
kill -TERM $(cat /tmp/loop.pid)
sleep 10                                 # graceful shutdown window
uv run pm-agent loop report              # final numbers
tail -50 /tmp/loop.log                   # post-mortem on last cycle
```

The cancellation-safe `_ensure_finished` closure in `loop.py` guarantees no
zombie `running` rows on SIGTERM. If you see one in the final report, that's
a regression — file a bug.

## Write the observation note

Copy `docs/superpowers/observations/_template.md` to today's dated file:

```bash
cp docs/superpowers/observations/_template.md \
   docs/superpowers/observations/$(date +%Y-%m-%d)-dry-run-N.md
```

Fill in the numbers from `pm-agent loop report` (you can copy-paste the table
output directly into the "Raw report" section). Then commit:

```bash
git add docs/superpowers/observations/
git commit -m "obs: dry-run #N — N cycles, \$N.NN, N PRs"
```

## Common failure modes (and what they mean)

| Symptom | Probable cause |
|---|---|
| All cycles `errored`, log shows "claude: command not found" | $PATH missing claude CLI under daemon's shell — start daemon from interactive shell |
| First cycle `ok`, subsequent all `aborted` | gh auth token expired mid-run — `gh auth status` to confirm |
| `running` cycle stuck > 1h | Coder subprocess hung — kill PID listed in log; reconciler will mark aborted on restart |
| Dashboard 500s when "Live Cycle" panel renders | state.db corruption — check WAL files exist + are non-empty; rare |
| No PRs opened despite findings | scanner produced findings but Coder failed all gates — check log for "gates_failed" |

See spec §7.3 for demo-day fallbacks if a fatal failure hits within the
24h window before demo.
