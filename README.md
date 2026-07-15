# TheCozyyRental Shorts Auto-Pipeline

Generates a faceless YouTube Short daily (script -> voiceover -> images -> video -> upload), fully free-tier.

## One-time local setup

1. `pip install -r requirements.txt` (also install `ffmpeg` locally: `brew install ffmpeg`)
2. `python local_auth.py` — opens a browser, log in with the Google account that owns the YouTube channel, approve access. Copy the printed refresh token.
3. Open `credentials/client_secret.json` and note the `client_id` and `client_secret` values.

## GitHub repo secrets

Go to Settings -> Secrets and variables -> Actions -> New repository secret, and add:

- `GROQ_API_KEY` (from https://console.groq.com/keys)
- `YT_CLIENT_ID` (from client_secret.json)
- `YT_CLIENT_SECRET` (from client_secret.json)
- `YT_REFRESH_TOKEN` (printed by local_auth.py)

## Test locally before scheduling

```
export GROQ_API_KEY=...
export YT_CLIENT_ID=...
export YT_CLIENT_SECRET=...
export YT_REFRESH_TOKEN=...
python generate_short.py
```

Check `output/latest_short.mp4` and your YouTube Studio (uploads as **private** by default).

## Enable the schedule

Once a manual `workflow_dispatch` run in the Actions tab succeeds and the uploaded video looks right, flip `PRIVACY_STATUS` in `daily-short.yml` to `"public"` (or `"unlisted"`) and let the daily cron take over.

## Customize

Edit `topics.json` to change the niche/topic rotation. Edit the prompt in `generate_script()` inside `generate_short.py` to change tone/style.
