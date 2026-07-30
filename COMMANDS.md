# Terminal Commands Reference

All commands assume you start in Terminal and haven't `cd`'d anywhere yet.
Every command below starts from (or includes) navigating into the project folder.

---

## 1. One-time environment setup

Only needed once, or if you're on a fresh Mac / the `.venv` folder is missing.

```bash
cd "/Users/viral/Desktop/KingsInnovations/My Apps/TheCozyyRental-Youtube"
brew install uv
uv venv .venv --python 3.12
source .venv/bin/activate
uv pip install -r requirements.txt
```

---

## 2. Get / refresh the YouTube OAuth token

Run this whenever you see `invalid_grant: Token has been expired or revoked`
in a run's output. Opens a browser for a one-time Google login.

```bash
cd "/Users/viral/Desktop/KingsInnovations/My Apps/TheCozyyRental-Youtube"
source .venv/bin/activate
python local_auth.py
```

It prints a new `refresh_token` at the end — copy it and update `.env`
(the `YT_REFRESH_TOKEN=` line) and/or the `YT_REFRESH_TOKEN` GitHub secret.

---

## 3. Run the pipeline manually (one video, right now)

Uses whatever is saved in `.env` (Groq key, YouTube credentials, etc.) —
nothing to type in each time.

```bash
cd "/Users/viral/Desktop/KingsInnovations/My Apps/TheCozyyRental-Youtube"
source .venv/bin/activate
set -a; source .env; set +a
python generate_short.py
```

To render and save locally **without** uploading to YouTube (safe test run):

```bash
cd "/Users/viral/Desktop/KingsInnovations/My Apps/TheCozyyRental-Youtube"
source .venv/bin/activate
set -a; source .env; set +a
SKIP_UPLOAD=true python generate_short.py
```

---

## 4. Local automation (launchd) — install once

Runs automatically every 4 hours (00:00, 04:00, 08:00, 12:00, 16:00, 20:00
local time), for as long as your Mac is on.

```bash
cd "/Users/viral/Desktop/KingsInnovations/My Apps/TheCozyyRental-Youtube"
chmod +x run_pipeline.sh catch_up.sh check_status.sh
cp com.cozyyrental.shortspipeline.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.cozyyrental.shortspipeline.plist
```

**Stop it:**
```bash
launchctl unload ~/Library/LaunchAgents/com.cozyyrental.shortspipeline.plist
```

**Restart it** (e.g. after editing the plist):
```bash
launchctl unload ~/Library/LaunchAgents/com.cozyyrental.shortspipeline.plist
launchctl load ~/Library/LaunchAgents/com.cozyyrental.shortspipeline.plist
```

---

## 5. Check status

```bash
cd "/Users/viral/Desktop/KingsInnovations/My Apps/TheCozyyRental-Youtube"
./check_status.sh
```

Shows: when the pipeline last ran, whether the launchd job is loaded, and
the last 25 lines of the run log.

---

## 6. Catch up on skipped runs

If `check_status.sh` (or a macOS notification) says runs were skipped
(Mac was asleep/off):

```bash
cd "/Users/viral/Desktop/KingsInnovations/My Apps/TheCozyyRental-Youtube"
./catch_up.sh          # 1 makeup video
./catch_up.sh 3         # 3 makeup videos, spaced 2 min apart
```

---

## 7. Force a single run right now (outside the schedule)

```bash
cd "/Users/viral/Desktop/KingsInnovations/My Apps/TheCozyyRental-Youtube"
./run_pipeline.sh
```

---

## 8. Git basics (only needed if something didn't auto-push)

As of July 2026 the repo is **public**. A branch ruleset on `master`
blocks force pushes and branch deletion, but normal pushes work fine —
there's no PR requirement (removed after hitting a self-approval
deadlock as the sole maintainer). Since nobody else has been added as a
collaborator, only you (and anything using your credentials) can push —
that's what actually keeps "everyone else" out, not a PR gate.

```bash
cd "/Users/viral/Desktop/KingsInnovations/My Apps/TheCozyyRental-Youtube"
git status
git add -A
git commit -m "your message here"
git push
```

If push is rejected (remote has newer commits):
```bash
git pull --no-rebase
git push
```

If you ever see `fatal: Unable to create '.git/index.lock': File exists`:
```bash
rm -f .git/index.lock .git/HEAD.lock
```

If you ever want to add a collaborator later, revisit branch protection
first (Settings → Rules → Rulesets) — re-enabling "Require a pull request
before merging" makes sense once more than one person has write access.

---

## 9. Current setup: self-chaining GitHub Actions

- **GitHub Actions (active), self-chained.** Each run triggers the next one
  itself: the final workflow step waits **15 minutes** after the run
  finishes, then calls GitHub's `workflow_dispatch` API to start the next
  run. Manual trigger also works anytime via **Actions tab → Run workflow**
  (that's also how you restart the chain if it ever stalls).
- **Why self-chaining instead of plain cron:** GitHub's native `schedule:`
  trigger is explicitly "best effort" with no SLA — under load it *silently
  drops* ticks rather than queuing them. Observed real gaps of 2-3+ hours on
  an hourly cron, so cron can't be trusted as the primary driver at this
  frequency. A low-frequency `schedule:` (`23 */3 * * *`) is kept only as a
  backstop in case the chain breaks entirely.
- **Requires the `DISPATCH_TOKEN` secret** — a fine-grained token with
  **Actions: Read and write** on this repo. GitHub deliberately blocks the
  default `GITHUB_TOKEN` from triggering further workflow runs (to prevent
  infinite loops), so the chain needs its own token. If this secret is
  missing/expired the chain quietly stops and only the 3-hourly backstop
  fires — check the workflow log for the "Failed to trigger next run"
  warning.
- **Cancelling works properly:** the chaining step is gated on
  `always() && !cancelled()`, so a manual cancel stops the chain. (With bare
  `always()` it used to keep sleeping and dispatch the next run anyway,
  ignoring the cancel.) To hard-stop everything: **Actions → the workflow →
  "..." → Disable workflow**.
- **Local Mac launchd — intentionally not installed.** Confirmed unloaded
  (`launchctl list` and `~/Library/LaunchAgents/` both empty). Leave it
  off; running two schedulers at once would double-post. Section 4 above
  still documents how to install it if you ever want to switch back to
  local-only automation instead of GitHub Actions.
- **Cowork scheduled task — removed.** It couldn't reach the Groq/YouTube
  APIs from the sandbox (network allowlist blocks them), so it was
  monitoring-only at best; deleted now that GitHub Actions handles
  execution directly.

To pause everything: disable the workflow (above), or comment out the
`schedule:` block and remove the chaining step.

---

## 10. Content model (what gets made, and how it varies)

**Categories** — `travel`, `food`, `tech`, `ai`, `animals`. One is picked
per run at random, *weighted by past performance* (views + likes×10 on
videos at least 20h old, see `compute_category_weights()`). Categories with
fewer than 3 mature samples get a neutral weight so they keep getting
explored rather than written off on noise.

**Country rotation** — `travel` / `food` / `animals` topics are built from
per-category templates with a `{country}` slot, cycling round-robin through
20 countries (Japan → Italy → … → Iceland). The pointer lives in
`topics.json` as `next_country_index` and **wraps back to the first country
automatically** after the last one, so it cycles forever. `tech` / `ai` have
no country and use Reddit-trending topics (with a static fallback pool).

**Narration language** — alternates every run via `next_use_native` in
`topics.json`: one video in English, the next narrated in that country's own
language for authenticity (`COUNTRY_LANGUAGES` maps each country to a
Microsoft Edge TTS neural voice). **Captions are always English** regardless,
so the audience can follow either way — Groq returns both `line` (native
narration) and `line_en` (English caption) per beat. Title, description and
hashtags also stay English for discovery. Native narration only applies to
country videos; a `tech`/`ai` topic in a native slot just stays English.

**If a native voice fails** (wrong/retired voice name, TTS hiccup), the run
automatically falls back to the English voice reading the English lines — a
bad voice entry costs one English video, never a failed run. What actually
got narrated is what's recorded in `performance_log.json`.

**Length** — target 15-25 seconds (70-95 words, 5-7 beats). There's a hard
floor in code: if the synthesized voiceover comes out under
`MIN_VIDEO_SECONDS` (14s), the script is regenerated up to 3 times rather
than publishing a stub. This exists because a 7-second video once shipped
when the model badly undershot the word target.

**Hashtags** — Groq's 5 topic-specific tags merged with 5 evergreen ones
(`#shorts`, `#viral`, …), de-duplicated and capped at 10.

---

## Where things live

| File | Purpose |
|---|---|
| `.env` | Secrets/config for local runs (gitignored, never committed) |
| `generate_short.py` | The main pipeline |
| `topics.json` | Static topic pool + niche + rotation state (`next_country_index`, `next_use_native`) |
| `performance_log.json` | Posted video history (for category weighting) |
| `merge_performance_log.py` | Resolves `performance_log.json` merge conflicts in CI (see below) |
| `pipeline_runs.log` | Local run log (gitignored) |
| `.last_run_timestamp` | Used for skip detection (gitignored) |

### A note on `merge_performance_log.py`

Every run appends its video to `performance_log.json` and pushes. If two runs
(or a run and a code push) land close together, the push is rejected and a
plain `git rebase` can hit a real conflict — two JSON array appends at the
same spot, which git's line-based merge can't reconcile. The workflow's retry
loop then calls this script, which unions both sides' video lists by
`video_id` so no entry is lost. If you ever see a failed run whose only error
is in the "Commit updated state" step, **the video already uploaded fine** —
only the bookkeeping needed retrying, and the next run self-heals since it
checks out fresh state anyway.
