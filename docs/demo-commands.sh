#!/usr/bin/env bash
# Live recording cheat-sheet — copy-paste in beat order during the 5/22 demo.
# Each section is self-contained; you don't have to run earlier sections to
# run a later one (except RESET, which preps shared state).
#
# Pairs with docs/demo-narrative.md. Talk-time budgets are in the narrative.
#
# DO NOT pipe this whole file — run sections individually during recording.

set -u
TGT=/tmp/pm-agent-day7-target

# ─────────────────────────────────────────────────────────────────────────────
# RESET — run once before recording, and again between dry-runs.
# ─────────────────────────────────────────────────────────────────────────────
reset_target() {
    rm -rf "$TGT"
    mkdir -p "$TGT/tests"
    cat > "$TGT/server.py" <<'PY'
"""Tiny request handler used as a target repo for pm-agent demos."""


def handle_request(path: str) -> dict:
    if path == "/":
        return {"hello": "world"}
    return {"error": "not found", "path": path}
PY
    cat > "$TGT/tests/test_server.py" <<'PY'
from server import handle_request


def test_root():
    result = handle_request("/")
    assert result["hello"] == "world"


def test_unknown_path():
    result = handle_request("/nope")
    assert result["error"] == "not found"
    assert result["path"] == "/nope"
PY
    echo init > "$TGT/README.md"
    git -C "$TGT" init -q -b master
    git -C "$TGT" -c user.name=pm-agent -c user.email=pm@local add .
    git -C "$TGT" -c user.name=pm-agent -c user.email=pm@local commit -q -m "init"
    echo "[reset] $TGT ready at $(git -C "$TGT" rev-parse --short HEAD)"
}

prewarm_claude() {
    # Burns ~$0.01 but kills the first-call latency that ruins the opening beat.
    uv run python -m pm_agent.runner "say only ok" >/dev/null 2>&1
    echo "[prewarm] claude session warm"
}

# ─────────────────────────────────────────────────────────────────────────────
# BEAT 1 — Foundation: claude -p runner (45-60s)
# Commit: 431dc64 + ab73cfd
# Talk: 60-line stdlib runner; --setting-sources fix cut cost 68%.
# ─────────────────────────────────────────────────────────────────────────────
beat1() {
    uv run python -m pm_agent.runner "what is 2+2"
}

# ─────────────────────────────────────────────────────────────────────────────
# BEAT 2 — TUI 5-panel skeleton (45s)
# Commit: e3ec409 — SVG: docs/tui-day2-snapshot.svg
# Talk: mock data, Textual layout, q to quit.
# Show the SVG (open in image viewer) — don't actually launch mock TUI live
# unless you have a wide terminal already prepped.
# ─────────────────────────────────────────────────────────────────────────────
beat2_live() {
    # Run mock TUI; q to quit when audience has had ~15s to look.
    uv run python -m pm_agent.tui
}
beat2_svg() {
    open docs/tui-day2-snapshot.svg
}

# ─────────────────────────────────────────────────────────────────────────────
# BEAT 3 — Real claude stream into TUI (45s)
# Commit: cfc1b70 — SVG: docs/tui-day3-real-snapshot.svg
# Show the SVG. Live single-coder works but uses real $.
# ─────────────────────────────────────────────────────────────────────────────
beat3_live() {
    uv run python -m pm_agent.tui --single "say only the word four"
}
beat3_svg() {
    open docs/tui-day3-real-snapshot.svg
}

# ─────────────────────────────────────────────────────────────────────────────
# BEAT 4 — 2 parallel Coders via git worktree (90s)
# Commit: dc366fb — SVG: docs/tui-day5-multi-snapshot.svg
# Show SVG. Architecture talk over the still image.
# ─────────────────────────────────────────────────────────────────────────────
beat4_svg() {
    open docs/tui-day5-multi-snapshot.svg
}

# ─────────────────────────────────────────────────────────────────────────────
# BEAT 5 — Real Planner (60s)
# Commit: 79983ad — SVG: docs/tui-day6-planner-snapshot.svg
# ─────────────────────────────────────────────────────────────────────────────
beat5_svg() {
    open docs/tui-day6-planner-snapshot.svg
}

# ─────────────────────────────────────────────────────────────────────────────
# BEAT 6 — End-to-end (90s)
# Commit: 0a0ca51 — SVG: docs/tui-day7-e2e-snapshot.svg
# Show SVG, then cat the latest summary.md to prove the artifacts are real.
# ─────────────────────────────────────────────────────────────────────────────
beat6_svg() {
    open docs/tui-day7-e2e-snapshot.svg
}
beat6_artifact() {
    # Use the canonical good run (cbf7936-era /stats demo).
    cat ~/.pm-agent/runs/20260511-161417/summary.md | head -60
}

# ─────────────────────────────────────────────────────────────────────────────
# BEAT 7 — Integration merge + robustness 4-pack (120s)
# Commits: 0f98823, a79877d, a577eb7, c5934b2
# SVG: docs/tui-day8-integration-snapshot.svg
# ─────────────────────────────────────────────────────────────────────────────
beat7_svg() {
    open docs/tui-day8-integration-snapshot.svg
}

# ─────────────────────────────────────────────────────────────────────────────
# BEAT 8 — TUI polish (60s)
# Commit: c230a09
# SVG: docs/tui-day10-polish-snapshot.svg + docs/tui-day10-e2e-snapshot.svg
# ─────────────────────────────────────────────────────────────────────────────
beat8_svg() {
    open docs/tui-day10-polish-snapshot.svg docs/tui-day10-e2e-snapshot.svg
}

# ─────────────────────────────────────────────────────────────────────────────
# BEAT 9 — Failure recovery + 2nd positive demo (180s)
# Commits: dd35b5f, cbf7936
# SVGs: 3 fault modes + /stats success
# Choose ONE fault mode to run LIVE (planner-yaml is most visual + ~free).
# Save the other two for SVG show.
# ─────────────────────────────────────────────────────────────────────────────
beat9_live_planner_fault() {
    reset_target
    uv run python -m pm_agent.tui \
        --repo "$TGT" \
        --inject-fault planner-yaml \
        --coder-timeout 60 \
        --test-cmd "python3 -c 'import tests.test_server as t; [getattr(t,n)() for n in dir(t) if n.startswith(\"test_\")]; print(\"PASS\")'" \
        "Add a /version endpoint returning a dict"
}
beat9_svgs() {
    open docs/tui-day11-fault-planner-yaml-snapshot.svg
    open docs/tui-day11-fault-coder-timeout-snapshot.svg
    open docs/tui-day11-fault-api-error-snapshot.svg
    open docs/tui-day11-stats-snapshot.svg
}
beat9_artifact() {
    cat ~/.pm-agent/runs/20260511-161417/summary.md
}

# ─────────────────────────────────────────────────────────────────────────────
# CONTINGENCY — if live e2e is wanted but rate limit risk is high
# Run inject-fault planner-yaml: it's deterministic, ~$0.08, no real Coder calls.
# ─────────────────────────────────────────────────────────────────────────────
contingency_live() {
    reset_target
    uv run python -m pm_agent.tui \
        --repo "$TGT" \
        --inject-fault planner-yaml \
        --test-cmd "echo PASS" \
        "Add a /health endpoint"
}

# ─────────────────────────────────────────────────────────────────────────────
# FULL DRY RUN (~$0.40, ~2 min) — Day 14 use only, not during recording.
# This is what cbf7936 ran.
# ─────────────────────────────────────────────────────────────────────────────
full_e2e_dry_run() {
    reset_target
    uv run python -m pm_agent.tui \
        --repo "$TGT" \
        --coder-timeout 180 \
        --test-cmd "python3 -c 'import tests.test_server as t; [getattr(t,n)() for n in dir(t) if n.startswith(\"test_\")]; print(\"PASS\")'" \
        "Add a /health endpoint returning a dict with key status equal to ok, plus a test"
}

# ─────────────────────────────────────────────────────────────────────────────
# Entry point: source this file then call functions directly, OR pass a beat
# name as arg.
#   source docs/demo-commands.sh && beat1
#   bash docs/demo-commands.sh beat1
# ─────────────────────────────────────────────────────────────────────────────
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    fn=${1:-help}
    if [ "$fn" = "help" ] || [ "$fn" = "-h" ] || [ "$fn" = "--help" ]; then
        echo "Usage: bash $0 <function>"
        echo "Available:"
        grep -E '^[a-z_0-9]+\(\) \{' "$0" | sed 's/() {//' | sed 's/^/  /'
        exit 0
    fi
    "$fn"
fi
