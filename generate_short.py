"""
End-to-end pipeline: pick a topic -> generate script (Groq) -> voiceover
(Edge TTS) -> images (Pollinations.ai) -> assemble vertical video with
captions (ffmpeg) -> upload to YouTube as a Short.

Env vars required:
    GROQ_API_KEY        - from https://console.groq.com/keys
    YT_CLIENT_ID        - from credentials/client_secret.json
    YT_CLIENT_SECRET    - from credentials/client_secret.json
    YT_REFRESH_TOKEN    - produced once by local_auth.py

Optional:
    PRIVACY_STATUS      - "private" (default, safe for testing), "unlisted", or "public"
"""

import os
import json
import re
import random
import time
import subprocess
import tempfile
import shutil
import asyncio
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import requests
import edge_tts

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

ROOT = Path(__file__).parent
TOPICS_FILE = ROOT / "topics.json"
VOICE = "en-US-GuyNeural"
VIDEO_SIZE = (1080, 1920)

# Curated for the DIY / city tours / nature niche - deliberately excludes
# r/news, r/worldnews, r/politics, r/PublicFreakout etc. so we don't even
# fetch heavy news/tragedy/political content in the first place. This is the
# primary defense; the SENSITIVE_KEYWORDS filter below is the backup.
TRENDING_SUBREDDITS = [
    "DIY", "crafts", "somethingimade", "nature", "wildlifephotography",
    "travel", "backpacking", "hiking", "itookapicture", "NationalPark",
]

# Basic safety net: skip any candidate whose title matches these. This is a
# blunt keyword filter, not a substitute for judgement - combined with
# Reddit's own NSFW flag and a curated subreddit list, and a safe static
# fallback list if nothing clean is found. Matched as whole words only
# (via \b boundaries) to avoid false positives like "skills" containing
# "kill" or "therapist" containing "rape" (the "Scunthorpe problem").
BLOCKED_KEYWORDS = [
    "porn", "sex", "nsfw", "nude", "naked", "onlyfans", "escort",
    "drug", "drugs", "cocaine", "heroin", "meth", "weed", "marijuana",
    "kill", "kills", "killed", "killing", "murder", "suicide",
    "self harm", "self-harm", "rape", "raped",
    "assault", "shooting", "shoot", "gun", "guns", "weapon", "weapons",
    "bomb", "terrorist", "terrorism",
    "scam", "fraud", "steal", "stolen", "illegal", "trafficking",
    "gambling", "casino", "bet", "betting", "racist", "racism",
    "nazi", "hate crime", "child abuse", "minor", "underage",
]

# Second, broader filter specifically for trending-topic selection: skips
# serious news / tragedy / politics even when it wouldn't otherwise trip the
# hard safety blocklist above. Keeps the channel on "fun trending" content
# rather than doom-scroll news, per the chosen content policy.
SENSITIVE_KEYWORDS = [
    "war", "invasion", "conflict", "military", "airstrike", "missile",
    "casualties", "death toll", "dies", "died", "dead", "mourns", "funeral",
    "earthquake", "tsunami", "hurricane", "wildfire", "flood", "disaster",
    "outbreak", "pandemic", "epidemic", "crash", "explosion", "wildfire",
    "protest", "riot", "strike", "layoffs", "recession", "indictment",
    "lawsuit", "arrested", "scandal", "controversy", "election", "president",
    "congress", "senate", "government shutdown", "impeach", "hostage",
]

_BLOCKED_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(w) for w in BLOCKED_KEYWORDS) + r")\b",
    re.IGNORECASE,
)
_SENSITIVE_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(w) for w in SENSITIVE_KEYWORDS) + r")\b",
    re.IGNORECASE,
)


def is_safe_topic(title):
    return _BLOCKED_PATTERN.search(title) is None


def find_blocked_match(text):
    """Returns the matched keyword + surrounding snippet for diagnostics,
    or None if nothing matched. Used to debug false positives without
    needing to print/expose the entire generated script."""
    m = _BLOCKED_PATTERN.search(text)
    if not m:
        return None
    start = max(0, m.start() - 25)
    end = min(len(text), m.end() + 25)
    return m.group(0), text[start:end]


def is_light_content(title):
    """Extra filter used only for trending-topic selection: True if the
    title avoids both the hard safety blocklist AND heavy news/politics."""
    return is_safe_topic(title) and _SENSITIVE_PATTERN.search(title) is None


def get_trending_topic():
    """Pulls today's trending, lightweight/fun topics (no rental/property
    focus) from Reddit's public JSON API (no key required) across a curated
    set of "fun trending" subreddits - deliberately excludes news/politics
    subs, and filters out anything sensitive/serious via is_light_content.
    Picks randomly from the top 5 candidates by upvotes (not always the same
    #1 post) so repeated runs in the same day don't all pick the same topic.
    Returns None if nothing suitable is found, in which case the caller
    should fall back to the static topics.json list.
    """
    headers = {"User-Agent": "shorts-auto-pipeline/1.0"}
    candidates = []
    for sub in TRENDING_SUBREDDITS:
        try:
            r = requests.get(
                f"https://www.reddit.com/r/{sub}/hot.json",
                params={"limit": 15},
                headers=headers,
                timeout=15,
            )
            r.raise_for_status()
            for post in r.json()["data"]["children"]:
                d = post["data"]
                title = d.get("title", "").strip()
                if not title or len(title) < 15:
                    continue
                if d.get("over_18"):
                    continue
                if d.get("stickied"):
                    continue
                if not is_light_content(title):
                    continue
                candidates.append((d.get("ups", 0), title))
        except Exception:
            continue  # this subreddit failed, just skip it

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[0], reverse=True)
    top5 = candidates[:5]
    return random.choice(top5)[1]


def pick_topic(force_static=False):
    data = json.loads(TOPICS_FILE.read_text())

    if not force_static and os.environ.get("USE_TRENDING", "true").lower() == "true":
        trending = get_trending_topic()
        if trending:
            return trending, data["niche"]

    topics = data["topics"]
    idx = data.get("next_index", 0) % len(topics)
    topic = topics[idx]
    data["next_index"] = (idx + 1) % len(topics)
    TOPICS_FILE.write_text(json.dumps(data, indent=2))
    return topic, data["niche"]


def generate_script(topic, niche):
    prompt = f"""You write scripts for YouTube Shorts in the niche: {niche}.
Topic: {topic}

This needs to be a full-length Short, roughly 55-70 seconds when read aloud
at a normal pace - NOT a quick 8-10 second clip. Structure it like a proper
piece of content people will actually watch to the end:
1. A scroll-stopping hook in the first line (a bold claim, a question, or
   "nobody tells you this" style opener)
2. Build-up / context (1-2 lines)
3. The main value: 4-7 concrete, specific tips/facts/steps - each its own
   beat, each genuinely useful, not generic filler
4. A strong closing line with a call to action (follow for more, comment
   your experience, etc.)

This should feel like a fast-paced, addictive, quick-cut viral Short (think
TikTok/Reels editing style) - visuals should change every 1.5-2.5 seconds,
NOT one slow static image per sentence. To achieve that, give each beat
MULTIPLE quick visual shots instead of just one.

Return STRICT JSON with keys:
- "title": catchy YouTube title, under 90 chars
- "description": 2-3 sentence description with a call to action
- "hashtags": array of 5 relevant hashtags (no # symbol)
- "beats": array of 9-14 objects (following the structure above), each with:
    - "line": one sentence of narration (conversational, punchy, no filler,
      roughly 12-20 words each)
    - "visual_prompts": array of 2-3 short, specific text-to-image prompts,
      each a DIFFERENT quick shot/angle/moment illustrating this line (not
      near-duplicates of each other) - interesting, high-quality,
      photorealistic, concrete scene/subject/composition, no vague prompts

Target 160-220 words of total narration across all beats combined - this is
important, do not undershoot it. First line must be a strong hook.
Content must be strictly brand-safe and family-friendly: no adult content,
violence, illegal activity, hate speech, drugs, gambling, or anything that
could be flagged as unsafe for advertisers or YouTube's community guidelines.
Output ONLY the JSON, no markdown fences."""

    resp = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {os.environ['GROQ_API_KEY']}"},
        json={
            "model": "llama-3.3-70b-versatile",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.9,
            "response_format": {"type": "json_object"},
        },
        timeout=60,
    )
    resp.raise_for_status()
    text = resp.json()["choices"][0]["message"]["content"].strip()
    text = re.sub(r"^```json|```$", "", text, flags=re.MULTILINE).strip()
    return json.loads(text)


async def synthesize_voice(text, out_path, rate="+18%"):
    # Slightly faster than default speaking rate - matches the punchier,
    # quick-cut editing style rather than a slow, deliberate voiceover.
    communicate = edge_tts.Communicate(text, VOICE, rate=rate)
    await communicate.save(str(out_path))


def download_image(prompt, out_path, width=1440, height=2560, max_retries=4):
    # Requesting above final 1080x1920 output resolution gives the zoompan
    # (Ken Burns) effect in build_video room to zoom in without softening.
    url = f"https://image.pollinations.ai/prompt/{requests.utils.quote(prompt)}"
    params = {"width": width, "height": height, "nologo": "true", "enhance": "true"}

    last_detail = None
    for attempt in range(max_retries):
        try:
            r = requests.get(url, params=params, timeout=90)
            if r.status_code == 429:
                last_detail = f"HTTP 429: {r.text[:200]}"
                wait = (2 ** attempt) * 3 + random.uniform(0, 2)
                time.sleep(wait)
                continue
            if r.status_code >= 400:
                last_detail = f"HTTP {r.status_code}: {r.text[:200]}"
                time.sleep((2 ** attempt) + random.uniform(0, 1))
                continue
            r.raise_for_status()
            out_path.write_bytes(r.content)
            return
        except requests.exceptions.RequestException as e:
            last_detail = f"{type(e).__name__}: {e}"
            time.sleep((2 ** attempt) + random.uniform(0, 1))

    raise RuntimeError(
        f"download_image failed after {max_retries} retries for prompt {prompt!r}. "
        f"Last error: {last_detail}"
    )


def get_audio_duration(path):
    out = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True
    )
    return float(out.stdout.strip())


FPS = 30


def make_ken_burns_clip(image_path, duration, out_path, zoom_in):
    """Turns a static image into a short video clip with a punchy zoom
    (Ken Burns effect) instead of a hard static cut. Zoom speed is tuned for
    short (~1.5-2.5s) quick-cut clips - fast enough to read as motion/energy
    within a couple seconds rather than a barely-perceptible drift. Alternates
    zoom-in/zoom-out across clips for variety.
    """
    frames = max(int(duration * FPS), 1)
    # Zoom rate scales with duration so a short clip still visibly moves
    # (zooms to ~1.15-1.25x over its lifetime) instead of looking static.
    zoom_rate = min(0.25 / max(frames, 1), 0.006)
    if zoom_in:
        zoom_expr = f"min(zoom+{zoom_rate},1.3)"
    else:
        zoom_expr = f"if(eq(on,1),1.3,max(zoom-{zoom_rate},1.0))"

    vf = (
        f"scale=3000:-1,"
        f"zoompan=z='{zoom_expr}':d={frames}:s={VIDEO_SIZE[0]}x{VIDEO_SIZE[1]}:fps={FPS},"
        f"format=yuv420p"
    )
    subprocess.run([
        "ffmpeg", "-y", "-loop", "1", "-i", str(image_path),
        "-t", str(duration), "-vf", vf,
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out_path)
    ], check=True, capture_output=True)


def build_video(image_paths, durations, voice_path, srt_path, out_path):
    """image_paths/durations are the flattened, per-visual-cut lists (already
    expanded from beats -> individual quick shots by the caller)."""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        clip_paths = []
        for i, (img, dur) in enumerate(zip(image_paths, durations)):
            clip_path = td / f"clip_{i}.mp4"
            make_ken_burns_clip(img, dur, clip_path, zoom_in=(i % 2 == 0))
            clip_paths.append(clip_path)

        concat_file = td / "concat.txt"
        concat_file.write_text(
            "\n".join(f"file '{c}'" for c in clip_paths)
        )

        silent_video = td / "silent.mp4"
        subprocess.run([
            "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_file),
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", str(FPS), str(silent_video)
        ], check=True, capture_output=True)

        # Style is baked into the .ass file itself (see write_srt) instead of
        # passed via force_style on the command line - that CLI syntax turned
        # out to be fragile/inconsistent across ffmpeg versions (comma/colon
        # escaping breaks on some builds). A plain "subtitles=path" avoids
        # that entirely.
        srt_escaped = str(srt_path).replace("\\", "\\\\").replace(":", "\\:")
        vf = f"subtitles={srt_escaped}"

        subprocess.run([
            "ffmpeg", "-y", "-i", str(silent_video), "-i", str(voice_path),
            "-vf", vf,
            "-c:v", "libx264", "-c:a", "aac", "-shortest", str(out_path)
        ], check=True)


def write_srt(beats, durations, out_path):
    """Writes an .ass subtitle file (despite the name, kept for compatibility
    with callers) with the caption style baked into the file header - no
    CLI-side force_style escaping needed."""

    def fmt(t):
        h = int(t // 3600); m = int((t % 3600) // 60); s = t % 60
        cs = int((t - int(t)) * 100)
        return f"{h:d}:{m:02d}:{int(s):02d}.{cs:02d}"

    header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,64,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,3,3,0,2,60,60,140,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    lines = [header]
    t = 0.0
    for beat, dur in zip(beats, durations):
        start, end = t, t + dur
        text = beat["line"].replace("\n", " ").replace(",", "\\,")
        lines.append(f"Dialogue: 0,{fmt(start)},{fmt(end)},Default,,0,0,0,,{text}")
        t = end

    out_path.write_text("\n".join(lines))


def upload_to_youtube(video_path, title, description, tags):
    creds = Credentials(
        None,
        refresh_token=os.environ["YT_REFRESH_TOKEN"],
        client_id=os.environ["YT_CLIENT_ID"],
        client_secret=os.environ["YT_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
    )
    youtube = build("youtube", "v3", credentials=creds)

    body = {
        "snippet": {
            "title": title[:95] + " #Shorts",
            "description": description,
            "tags": tags,
            "categoryId": "22",
        },
        "status": {
            "privacyStatus": os.environ.get("PRIVACY_STATUS", "private"),
            "selfDeclaredMadeForKids": False,
        },
    }
    media = MediaFileUpload(str(video_path), chunksize=-1, resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    while response is None:
        status, response = request.next_chunk()
    print("Uploaded:", response.get("id"))
    return response.get("id")


class UnsafeTopicError(Exception):
    pass


def run_once(topic, niche):
    """Runs the full pipeline for a single topic: script -> safety check ->
    voice -> images -> video -> upload. Raises on any failure; caller decides
    whether to retry with a different topic."""
    work = Path(tempfile.mkdtemp())
    print("Topic:", topic, flush=True)

    print("Generating script (Groq)...", flush=True)
    script = generate_script(topic, niche)
    print(f"Script ready: {len(script['beats'])} beats, title: {script['title']!r}", flush=True)

    full_check_text = script["title"] + " " + script["description"] + " " + \
        " ".join(b["line"] for b in script["beats"])
    if not is_safe_topic(full_check_text):
        match = find_blocked_match(full_check_text)
        print(f"Safety check matched: {match[0]!r} in context: ...{match[1]}...", flush=True)
        raise UnsafeTopicError(f"Generated script for topic {topic!r} failed the safety check")

    beats = script["beats"]
    full_text = " ".join(b["line"] for b in beats)

    print("Synthesizing voiceover (Edge TTS)...", flush=True)
    voice_path = work / "voice.mp3"
    asyncio.run(synthesize_voice(full_text, voice_path))

    total_dur = get_audio_duration(voice_path)
    print(f"Voiceover ready: {total_dur:.1f}s", flush=True)

    # Proportional pacing: a beat with more words gets more screen time,
    # instead of every beat getting an equal slice regardless of length.
    word_counts = [max(len(b["line"].split()), 1) for b in beats]
    total_words = sum(word_counts)
    beat_durations = [total_dur * (wc / total_words) for wc in word_counts]

    srt_path = work / "captions.ass"
    write_srt(beats, beat_durations, srt_path)

    # Fast quick-cut editing: each beat gets 2-3 distinct visual shots
    # (downloaded images) instead of one static image for its whole duration.
    shots = []  # list of (prompt, shot_duration)
    for beat, beat_dur in zip(beats, beat_durations):
        prompts = beat.get("visual_prompts") or [beat.get("visual_prompt", beat["line"])]
        prompts = prompts[:3] or [beat["line"]]
        shot_dur = beat_dur / len(prompts)
        for p in prompts:
            shots.append((p, shot_dur))

    # Sequential, not parallel: Pollinations' free tier enforces a hard
    # "max 1 queued request per IP" limit (confirmed via its actual 429
    # response body) - any concurrency at all trips it immediately. A small
    # pacing delay between requests avoids hammering it back-to-back.
    print(f"Fetching {len(shots)} images (sequential, ~1s apart - this is the slow part)...", flush=True)
    shot_image_paths = []
    for i, (prompt, _) in enumerate(shots):
        img_path = work / f"img_{i}.jpg"
        print(f"  image {i+1}/{len(shots)}: {prompt[:60]!r}...", flush=True)
        download_image(prompt, img_path)
        shot_image_paths.append(img_path)
        time.sleep(0.6)
    print("All images fetched.", flush=True)

    # Cocomelon-style pacing: cap how long any single shot can hold the
    # screen. If a shot is longer than MAX_CUT_DURATION, split it into
    # several quick sub-cuts reusing the same image (each still gets its own
    # zoom-in/out pass, so it reads as a real cut, not a freeze) rather than
    # fetching even more images.
    MAX_CUT_DURATION = 0.8
    image_paths, durations = [], []
    for img_path, shot_dur in zip(shot_image_paths, [d for _, d in shots]):
        n_splits = max(1, round(shot_dur / MAX_CUT_DURATION))
        sub_dur = shot_dur / n_splits
        for _ in range(n_splits):
            image_paths.append(img_path)
            durations.append(sub_dur)

    print(f"Assembling video: {len(image_paths)} quick cuts, {sum(durations):.1f}s total "
          f"(ffmpeg encoding - this takes a bit)...", flush=True)
    out_video = work / "short.mp4"
    build_video(image_paths, durations, voice_path, srt_path, out_video)
    print("Video assembled.", flush=True)

    final_path = ROOT / "output"
    final_path.mkdir(exist_ok=True)
    shutil.copy(out_video, final_path / "latest_short.mp4")

    # Set SKIP_UPLOAD=true to render and save locally without touching
    # YouTube at all - useful while you're still dialing in quality/pacing
    # and don't want every test run to actually publish anything.
    if os.environ.get("SKIP_UPLOAD", "false").lower() == "true":
        print(f"SKIP_UPLOAD is set - not uploading. Saved to {final_path / 'latest_short.mp4'}", flush=True)
        return

    print("Uploading to YouTube...", flush=True)
    upload_to_youtube(
        out_video,
        title=script["title"],
        description=script["description"] + "\n\n" + " ".join(f"#{h}" for h in script["hashtags"]),
        tags=script["hashtags"],
    )


def main():
    max_attempts = int(os.environ.get("MAX_ATTEMPTS", "3"))
    last_error = None

    for attempt in range(1, max_attempts + 1):
        # First attempt uses trending (if enabled). Retries force the static
        # topics.json rotation instead - trending would likely just return
        # the same top candidate again and fail the same way.
        force_static = attempt > 1
        topic, niche = pick_topic(force_static=force_static)

        try:
            run_once(topic, niche)
            print(f"Success on attempt {attempt}/{max_attempts}.")
            return
        except Exception as e:
            last_error = e
            print(f"Attempt {attempt}/{max_attempts} failed for topic {topic!r}: {e}")
            if attempt < max_attempts:
                print("Retrying with a different topic...")

    raise SystemExit(
        f"All {max_attempts} attempts failed. Last error: {last_error}"
    )


if __name__ == "__main__":
    main()
