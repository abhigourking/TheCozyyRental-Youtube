#!/bin/bash
# Manually make up for skipped run(s) - e.g. after run_pipeline.sh notified
# you that N scheduled runs were missed while your Mac was asleep/off.
#
# Usage:
#   ./catch_up.sh        -> runs 1 catch-up video now
#   ./catch_up.sh 3       -> runs 3 catch-up videos, spaced 2 min apart
#
# Kept deliberately manual (not automatic) so a long sleep/vacation doesn't
# silently dump a pile of back-to-back videos and burn through the YouTube
# daily upload quota (1600 units/upload, 10,000/day default) without you
# choosing to do so.

REPO_DIR="/Users/viral/Desktop/KingsInnovations/My Apps/TheCozyyRental-Youtube"
cd "$REPO_DIR" || exit 1

COUNT="${1:-1}"
echo "Running $COUNT catch-up video(s)..."

for i in $(seq 1 "$COUNT"); do
    echo "--- Catch-up run $i/$COUNT ---"
    ./run_pipeline.sh
    if [ "$i" -lt "$COUNT" ]; then
        echo "Waiting 2 minutes before the next catch-up run..."
        sleep 120
    fi
done

echo "Catch-up complete. Check pipeline_runs.log or run ./check_status.sh for details."
