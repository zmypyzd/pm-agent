#!/usr/bin/env bash
# Run every bug reproduction script in priority order and tally results.
#
# Exit codes per script:
#   0 = bug REPRODUCED (the failure pattern was observed)
#   1 = bug NOT REPRODUCED (likely fixed, or could not trigger)
#
# This wrapper does NOT care which it is — it just prints what each script
# said. The aggregate at the bottom tells you how many bugs are still present.
#
# Usage:
#   bash reproductions/run_all.sh           # run everything
#   bash reproductions/run_all.sh critical  # only one tier
#   bash reproductions/run_all.sh BUG-001   # one specific bug

set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$ROOT/.." && pwd)"
cd "$PROJECT_ROOT"

if command -v uv >/dev/null 2>&1; then
    PY="uv run python"
else
    PY="python3"
fi

tier_filter=""
bug_filter=""
case "${1:-}" in
    critical|high|medium|low) tier_filter="$1" ;;
    BUG-*)                    bug_filter="${1#BUG-}" ;;
    "")                       ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
esac

# Priority order matches BUGS.md "推荐修复顺序" (updated end of Round 2).
# Critical first, then High by audit severity, then Medium, then Low.
ORDER=(
    critical/bug_001_stderr_deadlock.py
    high/bug_049_sync_runner_stderr_deadlock.py    # same root cause as 001
    high/bug_010_cancel_doesnt_kill.py
    critical/bug_002_test_cmd_orphans.py
    high/bug_039_markup_injection.py
    high/bug_025_git_author_env.py
    high/bug_008_default_repo_race.py
    high/bug_007_detached_head.py
    high/bug_004_011_027_parse_tasks_type_checks.py
    high/bug_047_duplicate_task_id.py
    high/bug_026_filename_prompt_injection.py
    high/bug_006_coder_no_allowed_paths.py
    high/bug_005_glob_disjoint.py
    high/bug_003_run_id_collision.py
    high/bug_009_n_gt_2_tasks.py
    medium/bug_012_yaml_bomb.py
    medium/bug_013_extract_yaml_newline.py
    medium/bug_014_diff_stats.py
    medium/bug_016_test_timeout_unconfigurable.py
    medium/bug_018_progress_not_updated.py
    medium/bug_019_cleanup_silent.py
    medium/bug_020_day8_cleanup_overlap.py
    medium/bug_024_demo_path_mismatch.py
    medium/bug_029_is_error_not_checked.py
    medium/bug_031_default_branch.py
    medium/bug_032_arg_max.py
    medium/bug_035_no_tests.py
    medium/bug_037_handoff_stale_commit.py
    medium/bug_053_api_error_markup.py
    low/bug_015_ev_null_comment.py
    low/bug_017_max_retries_hardcoded.py
    low/bug_021_idle_range.py
    low/bug_022_reviewer_dead.py
    low/bug_023_main_orphan.py
    low/bug_028_empty_path.py
    low/bug_033_log_throttle.py
    low/bug_034_pyproject_scripts.py
    low/bug_038_teamagent_half_tracked.py
    low/bug_045_diff_stats_dashdash_content.py
)

repro_count=0
fixed_count=0
error_count=0
results=()

for rel in "${ORDER[@]}"; do
    if [ -n "$tier_filter" ] && [[ "$rel" != "$tier_filter"/* ]]; then continue; fi
    if [ -n "$bug_filter" ] && [[ "$rel" != *"bug_$bug_filter"* ]]; then continue; fi

    abs="$ROOT/$rel"
    if [ ! -f "$abs" ]; then
        echo "[MISSING] $rel"
        error_count=$((error_count+1))
        continue
    fi

    out=$($PY "$abs" 2>&1)
    code=$?
    line=$(echo "$out" | tail -n 1)

    if [ $code -eq 0 ]; then
        repro_count=$((repro_count+1))
        printf "  \033[33m%-13s\033[0m %s\n" "REPRODUCED" "$line"
    elif [ $code -eq 1 ]; then
        fixed_count=$((fixed_count+1))
        printf "  \033[32m%-13s\033[0m %s\n" "NOT-REPRO" "$line"
    else
        error_count=$((error_count+1))
        printf "  \033[31m%-13s\033[0m %s (exit %d)\n" "ERROR" "$line" "$code"
        echo "$out" | sed 's/^/      /'
    fi
done

echo
echo "──────────────────────────────────────────────────────"
echo "  Reproduced (bug still present): $repro_count"
echo "  Not reproduced (likely fixed):  $fixed_count"
echo "  Script error / missing:         $error_count"
echo "──────────────────────────────────────────────────────"
