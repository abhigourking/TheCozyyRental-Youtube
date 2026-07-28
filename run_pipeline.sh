#!/bin/bash
# Wrapper launchd calls every 4 hours. Handles:
#  - activating the project's venv (so the right interpreter + installed
#    deps are used - a bare `python3` here would hit system Python instead)
#  - detecting skipped runs and auto-firing catch-up videos, spaced 5 min
#    apart, capped to today's remaining 6/day quota (Mac was asleep/off
#    through one or more scheduled slots)
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
DAILY_TARGET=6           # videos/day policy - auto catch-up never posts past this
MAX_AUTO_CATCHUP=5       # hard safety cap per invocation regardless of how long the Mac was off

if [ -f ".venv/bin/activate" ]; then
    source .venv/bin/activate
fi

now_epoch=$(date +%s)
now_human=$(date "+%Y-%m-%d %H:%M:%S %Z")

# --- Skip detection ---
missed=0
if [ -f "$LAST_RUN_FILE" ]; then
    last_epoch=$(cat "$LAST_RUN_FILE")
    gap_hours=$(( (now_epoch - last_epoch) / 3600 ))
    if [ "$gap_hours" -gt "$SKIP_THRESHOLD_HOURS" ]; then
        missed=$(( gap_hours / EXPECTED_INTERVAL_HOURS - 1 ))
        [ "$missed" -lt 1 ] && missed=1
        echo "[$now_human] SKIP DETECTED: ~${gap_hours}h since last run - ~${missed} scheduled run(s) likely missed (Mac probably asleep/off)." >> "$LOG_FILE"
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

# --- Auto catch-up (only if a skip was detected above) ---
if [ "$missed" -gt 0 ]; then
    today_count=$(python3 - <<'PYEOF'
import json, datetime
try:
    with open("performance_log.json") as f:
        data = json.load(f)
except Exception:
    data = {"videos": []}
today = datetime.date.today()
count = 0
for v in data.get("videos", []):
    try:
        if datetime.datetime.fromtimestamp(v["posted_at"]).date() == today:
            count += 1
    except Exception:
        pass
print(count)
PYEOF
)
    remaining=$(( DAILY_TARGET - today_count ))
    [ "$remaining" -lt 0 ] && remaining=0

    catchup_count=$missed
    [ "$catchup_count" -gt "$remaining" ] && catchup_count=$remaining
    [ "$catchup_count" -gt "$MAX_AUTO_CATCHUP" ] && catchup_count=$MAX_AUTO_CATCHUP

    if [ "$catchup_count" -gt 0 ]; then
        echo "[$now_human] Auto-starting $catchup_count catch-up run(s), 5 min apart (today: $today_count/$DAILY_TARGET before catch-up)." >> "$LOG_FILE"
        osascript -e "display notification \"Auto-running $catchup_count catch-up video(s), ~5 min apart, to get back on today's $DAILY_TARGET/day pace.\" with title \"Shorts Pipeline\"" 2>/dev/null
        ./catch_up.sh "$catchup_count" >> "$LOG_FILE" 2>&1
    else
        echo "[$now_human] Skip detected but today's $DAILY_TARGET/day quota is already met - no auto catch-up." >> "$LOG_FILE"
    fi
fi
