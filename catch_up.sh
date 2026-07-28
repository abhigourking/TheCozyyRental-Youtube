#!/bin/bash
# Make up for skipped run(s) - e.g. after a scheduled slot was missed while
# the Mac was asleep/off. Called two ways:
#   1. Automatically by run_pipeline.sh, capped to today's remaining 6/day
#      quota (see DAILY_TARGET/MAX_AUTO_CATCHUP there) - this is the normal
#      path now that catch-up is auto-triggered on skip detection.
#   2. Manually, any time, if you want extra runs outside that logic:
#        ./catch_up.sh        -> runs 1 catch-up video now
#        ./catch_up.sh 3      -> runs 3 catch-up videos, spaced 5 min apart
#
# The daily-quota cap in run_pipeline.sh is what keeps a long sleep/vacation
# from silently dumping a pile of back-to-back videos and burning through
# the YouTube daily upload quota (1600 units/upload, 10,000/day default) -
# manual invocations here don't have that cap, so pass a sane count.

REPO_DIR="/Users/viral/Desktop/KingsInnovations/My Apps/TheCozyyRental-Youtube"
cd "$REPO_DIR" || exit 1

COUNT="${1:-1}"
echo "Running $COUNT catch-up video(s)..."

for i in $(seq 1 "$COUNT"); do
    echo "--- Catch-up run $i/$COUNT ---"
    ./run_pipeline.sh
    if [ "$i" -lt "$COUNT" ]; then
        echo "Waiting 5 minutes before the next catch-up run..."
        sleep 300
    fi
done

echo "Catch-up complete. Check pipeline_runs.log or run ./check_status.sh for details."
