"""
Called by the GitHub Actions "Commit updated state" step when a git push is
rejected AND a plain `git rebase origin/master` hits a real conflict -
almost always two runs both appending an entry to performance_log.json's
"videos" array around the same time. git's line-based merge can't
reconcile two inserts at the same spot (the closing bracket), so this
resolves it in Python instead.

Expects /tmp/ours_performance_log.json to hold this run's own
pre-reset performance_log.json (saved by the workflow before it ran
`git reset --hard origin/master`). Unions both sides' video lists by
video_id so neither run's entry is silently dropped, then overwrites the
working tree's performance_log.json (now at origin's version) with the
merged result, ready to be git-added and committed.
"""
import json

with open("/tmp/ours_performance_log.json") as f:
    ours = json.load(f)
with open("performance_log.json") as f:
    theirs = json.load(f)

by_id = {v["video_id"]: v for v in theirs.get("videos", [])}
for v in ours.get("videos", []):
    by_id.setdefault(v["video_id"], v)
merged = sorted(by_id.values(), key=lambda v: v.get("posted_at", 0))

with open("performance_log.json", "w") as f:
    json.dump({"videos": merged}, f, indent=2)

print(f"Merged performance_log.json: {len(merged)} total video entries.")
