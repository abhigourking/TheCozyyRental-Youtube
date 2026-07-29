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

## 9. Current setup: GitHub Actions is the sole scheduler

- **GitHub Actions (active)** — `schedule:` in
  `.github/workflows/daily-short.yml` runs every 4 hours (00:00, 04:00,
  08:00, 12:00, 16:00, 20:00 UTC). The repo is public, so this runs on
  unlimited free Actions minutes (no more billing-quota risk). Manual
  trigger also works anytime via **Actions tab → Run workflow**.
- **Local Mac launchd — intentionally not installed.** Confirmed unloaded
  (`launchctl list` and `~/Library/LaunchAgents/` both empty). Leave it
  off; running two schedulers at once would double-post. Section 4 above
  still documents how to install it if you ever want to switch back to
  local-only automation instead of GitHub Actions.
- **Cowork scheduled task — removed.** It couldn't reach the Groq/YouTube
  APIs from the sandbox (network allowlist blocks them), so it was
  monitoring-only at best; deleted now that GitHub Actions handles
  execution directly.

If GitHub Actions ever needs to be paused again (e.g. future billing
issue), comment out the `schedule:` block in
`.github/workflows/daily-short.yml`, commit, and push.

---

## Where things live

| File | Purpose |
|---|---|
| `.env` | Secrets/config for local runs (gitignored, never committed) |
| `generate_short.py` | The main pipeline |
| `topics.json` | Topic list + niche + language rotation state |
| `performance_log.json` | Posted video history (for category weighting) |
| `pipeline_runs.log` | Local run log (gitignored) |
| `.last_run_timestamp` | Used for skip detection (gitignored) |
