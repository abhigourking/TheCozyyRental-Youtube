"""
Called by the GitHub Actions "Commit updated state" step alongside
merge_performance_log.py, when a git push is rejected AND a plain
`git rebase origin/master` hits a real conflict.

Mirrors merge_performance_log.py's approach for the OTHER bot-managed,
append-style state files: nsfw_test_log.json, error_log.json, and
flagged_prompts.json. Without this, `git reset --hard origin/master`
(used to cleanly resolve the conflict) silently discarded this run's own
entries in every one of these files - which is exactly what happened
during the NudeNet test week: roughly 4 days of nsfw_test_log.json
entries were lost to repeated push conflicts with no error or warning,
because only performance_log.json had this protection. This closes that
gap for the rest of the bot-managed logs.

Expects backups at /tmp/ours_<filename> for whichever of these files
existed in this run's working tree before the reset (see the workflow
step - it now backs up all of them, not just performance_log.json).
Only touches a file if its backup exists, so this is a no-op for repos/
runs that never wrote a given log.

topics.json is deliberately NOT handled here - it's live rotation state
(index/flags), not an append log, and origin's post-conflict version is
already a perfectly valid state to continue from (worst case: this run's
own rotation step gets superseded by whichever run's push won the race,
which just reorders an upcoming pick, not a correctness issue).
"""
import json
import os

MAX_ENTRIES = {"nsfw_test_log.json": 500, "error_log.json": 200}


def merge_list_log(filename, list_key, dedupe_key, sort_key):
    backup = f"/tmp/ours_{filename}"
    if not os.path.exists(backup):
        return
    with open(backup) as f:
        ours = json.load(f)

    theirs = {}
    if os.path.exists(filename):
        with open(filename) as f:
            theirs = json.load(f)
    theirs_list = theirs.get(list_key, [])

    seen = {dedupe_key(e) for e in theirs_list}
    added = 0
    for e in ours.get(list_key, []):
        k = dedupe_key(e)
        if k not in seen:
            theirs_list.append(e)
            seen.add(k)
            added += 1

    theirs_list.sort(key=sort_key)
    cap = MAX_ENTRIES.get(filename)
    if cap:
        theirs_list = theirs_list[-cap:]

    with open(filename, "w") as f:
        json.dump({list_key: theirs_list}, f, indent=2)
    print(f"Merged {filename}: +{added} entries from this run, {len(theirs_list)} total.")


def merge_flagged_prompts():
    backup = "/tmp/ours_flagged_prompts.json"
    if not os.path.exists(backup):
        return
    with open(backup) as f:
        ours = json.load(f)

    theirs = {}
    if os.path.exists("flagged_prompts.json"):
        with open("flagged_prompts.json") as f:
            theirs = json.load(f)

    added = 0
    for key, entry in ours.items():
        if key not in theirs:
            theirs[key] = entry
            added += 1
        else:
            existing = theirs[key]
            existing["times_flagged"] = existing.get("times_flagged", 0) + entry.get("times_flagged", 0)
            for c in entry.get("classes", []):
                if c not in existing.get("classes", []):
                    existing.setdefault("classes", []).append(c)
            existing["examples"] = (existing.get("examples", []) + entry.get("examples", []))[-5:]

    with open("flagged_prompts.json", "w") as f:
        json.dump(theirs, f, indent=2)
    print(f"Merged flagged_prompts.json: {added} new prompt(s) from this run, {len(theirs)} total.")


merge_list_log(
    "nsfw_test_log.json", "runs",
    dedupe_key=lambda e: (e.get("run_number"), e.get("ran_at")),
    sort_key=lambda e: e.get("ran_at", 0),
)
merge_list_log(
    "error_log.json", "errors",
    dedupe_key=lambda e: (e.get("run_number"), e.get("occurred_at")),
    sort_key=lambda e: e.get("occurred_at", 0),
)
merge_flagged_prompts()
