#!/bin/bash
# Quick visibility into the local launchd-driven pipeline: when it last ran,
# and the tail of its log.

REPO_DIR="/Users/viral/Desktop/KingsInnovations/My Apps/TheCozyyRental-Youtube"
cd "$REPO_DIR" || exit 1

if [ -f .last_run_timestamp ]; then
    last_epoch=$(cat .last_run_timestamp)
    now_epoch=$(date +%s)
    gap_hours=$(( (now_epoch - last_epoch) / 3600 ))
    echo "Last run: $(date -r "$last_epoch") (${gap_hours}h ago)"
    if [ "$gap_hours" -gt 5 ]; then
        echo "⚠️  That's longer than the 4h schedule - a run may have been skipped. Consider ./catch_up.sh"
    fi
else
    echo "No runs recorded yet - launchd job may not have fired, or hasn't been installed."
fi

echo ""
echo "launchd job status:"
launchctl list | grep cozyyrental || echo "  Not currently loaded - see install instructions."

echo ""
echo "Last 25 lines of pipeline_runs.log:"
tail -25 pipeline_runs.log 2>/dev/null || echo "(no log file yet)"
