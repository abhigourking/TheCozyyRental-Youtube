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

## 8. Git basics — branch + PR required for `master`

As of July 2026 the repo is **public** and `master` is protected by a
ruleset: **no direct pushes are allowed, including from the repo owner's
own token/CLI** — every change must go through a pull request that you
approve and merge yourself. (The GitHub Actions bot's automatic
`topics.json` / `performance_log.json` state commits are exempted via the
ruleset's bypass list, since they're just rotation state, not code.)

```bash
cd "/Users/viral/Desktop/KingsInnovations/My Apps/TheCozyyRental-Youtube"
git checkout -b my-change-branch
git add -A
git commit -m "your message here"
git push -u origin my-change-branch
```

Then open a PR from that branch into `master` on GitHub (the push output
prints a direct "Create a pull request" link — or use the **Compare &
pull request** button that appears on the repo page). Review the diff and
click **Merge** yourself; nothing lands on `master` until you do.

If you ever see `fatal: Unable to create '.git/index.lock': File exists`:
```bash
rm -f .git/index.lock .git/HEAD.lock
```

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
issue), comment out the `schedule:` block in `.github/workflows/daily-short.yml`
via the branch+PR process in section 8 — `workflow_dispatch` keeps manual
runs available either way.

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
