#!/bin/bash
# Wrapper launchd calls every 4 hours. Handles:
#  - detecting and logging/notifying about skipped runs (Mac was asleep/off
#    through a scheduled slot - launchd usually catches up on wake, but not
#    reliably across a full shutdown, so this is a backstop)
#  - loading secrets from .env
#  - running the pipeline
#  - committing/pushing the updated state back to GitHub
#
# Safe to run manually too (e.g. via catch_up.sh) - it just does one cycle.

REPO_DIR="/Users/viral/Desktop/KingsInnovations/My Apps/TheCozyyRental-Youtube"
cd "$REPO_DIR" || exit 1

LOG_FILE="$REPO_DIR/pipeline_runs.log"
LAST_RUN_FILE="$REPO_DIR/.last_run_timestamp"
EXPECTED_INTERVAL_HOURS=4
SKIP_THRESHOLD_HOURS=5   # buffer above the exact 4h interval before we call it a skip

now_epoch=$(date +%s)
now_human=$(date "+%Y-%m-%d %H:%M:%S %Z")

# --- Skip detection ---
if [ -f "$LAST_RUN_FILE" ]; then
    last_epoch=$(cat "$LAST_RUN_FILE")
    gap_hours=$(( (now_epoch - last_epoch) / 3600 ))
    if [ "$gap_hours" -gt "$SKIP_THRESHOLD_HOURS" ]; then
        missed=$(( gap_hours / EXPECTED_INTERVAL_HOURS - 1 ))
        [ "$missed" -lt 1 ] && missed=1
        echo "[$now_human] SKIP DETECTED: ~${gap_hours}h since last run - ~${missed} scheduled run(s) likely missed (Mac probably asleep/off)." >> "$LOG_FILE"
        osascript -e "display notification \"~${missed} scheduled run(s) were missed while your Mac was asleep/off. Run ./catch_up.sh ${missed} to make them up.\" with title \"Shorts Pipeline\" sound name \"Basso\"" 2>/dev/null
    fi
fi
echo "$now_epoch" > "$LAST_RUN_FILE"

echo "[$now_human] Starting run..." >> "$LOG_FILE"

set -a
source .env
set +a

if python3 generate_short.py >> "$LOG_FILE" 2>&1; then
    echo "[$now_human] Run succeeded." >> "$LOG_FILE"
else
    echo "[$now_human] Run FAILED - see log above for details." >> "$LOG_FILE"
    osascript -e "display notification \"Pipeline run failed - check pipeline_runs.log\" with title \"Shorts Pipeline\" sound name \"Basso\"" 2>/dev/null
fi

# Commit + push the updated state (topics.json, performance_log.json).
git add topics.json performance_log.json >> "$LOG_FILE" 2>&1
if git commit -m "chore: advance state [local launchd run]" >> "$LOG_FILE" 2>&1; then
    if ! git push origin master >> "$LOG_FILE" 2>&1; then
        echo "[$now_human] Push failed (maybe a concurrent update) - will resolve on next run." >> "$LOG_FILE"
    fi
else
    echo "[$now_human] Nothing to commit." >> "$LOG_FILE"
fi

echo "[$now_human] Done." >> "$LOG_FILE"
echo "---" >> "$LOG_FILE"
