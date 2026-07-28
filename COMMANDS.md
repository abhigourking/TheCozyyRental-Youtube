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

---

## 9. Current setup: hybrid scheduling

As of July 2026, three schedulers exist, with only one meant to actually
run the pipeline at a time:

- **Local Mac launchd (primary)** - full network access, no cost. Section 4
  below. `run_pipeline.sh` now activates `.venv` automatically and, on
  detecting a skipped run (Mac was asleep/off), auto-fires catch-up runs
  5 minutes apart via `catch_up.sh`, capped to today's remaining 6/day quota
  (never over-posts even after a long sleep/vacation).
- **GitHub Actions (paused)** - `schedule:` is commented out in
  `.github/workflows/daily-short.yml` because this account is out of free
  Actions minutes for the billing cycle. `workflow_dispatch` (manual "Run
  workflow" button in the Actions tab) still works anytime. To make this
  free and permanent again, register a self-hosted runner on the Mac and
  switch `runs-on: ubuntu-latest` to `runs-on: self-hosted` - self-hosted
  runners don't consume the minutes quota.
- **Cowork scheduled task (monitoring only)** - runs every 4 hours but does
  NOT attempt the pipeline itself. Cowork's sandbox can only reach a small
  allowlist of domains and gets blocked (403) calling Groq/YouTube APIs, so
  it just checks `performance_log.json`/`pipeline_runs.log` and reports
  whether today's 6-video pace is on track, flagging if the Mac appears to
  be behind or offline. It also only fires while the Claude desktop app is
  open (a closed app means the check runs on next launch instead).

To switch primary scheduler back to GitHub Actions (e.g. once minutes
reset, or after setting up a self-hosted runner):

1. Edit `.github/workflows/daily-short.yml` — uncomment the `schedule:`
   block near the top
2. `git add .github/workflows/daily-short.yml && git commit -m "Re-enable GH Actions schedule" && git push`
3. Stop the local automation so only one scheduler is active:
   `launchctl unload ~/Library/LaunchAgents/com.cozyyrental.shortspipeline.plist`

You can also trigger GitHub Actions manually anytime (even while paused)
from the repo's **Actions** tab → select the workflow → **Run workflow**.

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
