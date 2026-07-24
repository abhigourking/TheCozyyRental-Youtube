"""
End-to-end pipeline: pick a topic -> generate script (Groq) -> voiceover
(Edge TTS) -> visuals (real stock footage via Pexels, falling back to
Pollinations.ai AI images with a Ken Burns zoom if no stock match is found)
-> assemble vertical video with captions (ffmpeg) -> upload to YouTube as a
Short.

Env vars required:
    GROQ_API_KEY        - from https://console.groq.com/keys
    YT_CLIENT_ID        - from credentials/client_secret.json
    YT_CLIENT_SECRET    - from credentials/client_secret.json
    YT_REFRESH_TOKEN    - produced once by local_auth.py

Optional:
    PEXELS_API_KEY      - from https://www.pexels.com/api/ (free). Without
                           this, every shot falls back to AI images.
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

# Alternates every run (state persisted in topics.json). Each language needs
# its own Edge TTS neural voice and its own subtitle font (see write_srt) -
# Devanagari script needs a font that actually has those glyphs, Latin fonts
# render it as blank boxes.
VOICES = {
    "en": "en-US-GuyNeural",
    "hi": "hi-IN-MadhurNeural",
}
VIDEO_SIZE = (1080, 1920)

# Curated for the travel / food / technology niche - visually rich subjects
# that read well as fast photo/video slideshows. Deliberately excludes
# r/news, r/worldnews, r/politics, r/PublicFreakout etc. so we don't even
# fetch heavy news/tragedy/political content in the first place. This is the
# primary defense; the SENSITIVE_KEYWORDS filter below is the backup.
# Split by category (rather than one flat list) so performance data can bias
# which category gets picked next - see compute_category_weights().
CATEGORY_SUBREDDITS = {
    "travel": ["travel", "backpacking", "itookapicture", "hiking"],
    "food": ["food", "recipes", "EatCheapAndHealthy"],
    "tech": ["gadgets", "technology"],
}

# Evergreen, proven high-reach hashtags for YouTube Shorts discovery - added
# on top of Groq's 5 topic-specific hashtags rather than relying purely on
# ones invented per-topic. Guarantees every upload has real "viral" tags,
# not just niche-specific ones.
UNIVERSAL_HASHTAGS = ["shorts", "youtubeshorts", "viral", "trending", "shortsfeed"]


def build_hashtags(topic_hashtags):
    """Merges the topic-specific hashtags with the evergreen universal ones,
    de-duplicated case-insensitively and capped at 10 so it doesn't read as
    tag spam. Always returns at least 5 (usually 8-10)."""
    seen = set()
    merged = []
    for tag in list(topic_hashtags or []) + UNIVERSAL_HASHTAGS:
        clean = tag.strip().lstrip("#")
        if not clean:
            continue
        key = clean.lower()
        if key in seen:
            continue
        seen.add(key)
        merged.append(clean)
    return merged[:10]

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


def get_trending_topic(category):
    """Pulls today's trending, lightweight/fun topics (no rental/property
    focus) from Reddit's public JSON API (no key required) across the
    subreddits for the given category - deliberately excludes news/politics
    subs, and filters out anything sensitive/serious via is_light_content.
    Picks randomly from the top 5 candidates by upvotes (not always the same
    #1 post) so repeated runs in the same day don't all pick the same topic.
    Returns None if nothing suitable is found, in which case the caller
    should fall back to the static topics.json list.
    """
    headers = {"User-Agent": "shorts-auto-pipeline/1.0"}
    candidates = []
    for sub in CATEGORY_SUBREDDITS.get(category, []):
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


def pick_topic(category=None, force_static=False):
    if category is None:
        category = random.choice(list(CATEGORY_SUBREDDITS.keys()))
    data = json.loads(TOPICS_FILE.read_text())

    if not force_static and os.environ.get("USE_TRENDING", "true").lower() == "true":
        trending = get_trending_topic(category)
        if trending:
            return trending, data["niche"]

    # Static fallback: topics are tagged with a category (see topics.json).
    # Random rather than round-robin - the pool per category is small enough
    # that strict rotation isn't worth the extra state to track.
    topics = data["topics"]
    matching = [t for t in topics if t.get("category") == category]
    if not matching:
        matching = topics  # safety net if a category has no static entries
    topic = random.choice(matching)
    return topic["text"], data["niche"]


def pick_language():
    """Alternates en/hi every run, state persisted in topics.json so it
    survives across separate GitHub Actions runs (each run is a fresh
    checkout, only topics.json's committed state carries over)."""
    data = json.loads(TOPICS_FILE.read_text())
    lang = data.get("next_language", "en")
    data["next_language"] = "hi" if lang == "en" else "en"
    TOPICS_FILE.write_text(json.dumps(data, indent=2))
    return lang


def generate_script(topic, niche, language="en"):
    if language == "hi":
        language_instruction = """LANGUAGE: Write "title", "description", and every beat's "line" entirely
in Hindi using Devanagari script (not Hinglish/romanized). Keep the tone
natural and conversational, like a popular Hindi YouTube creator - not a
stiff textbook translation. Hashtags may stay in English (common practice
for reach). IMPORTANT EXCEPTION: "visual_prompts" must ALWAYS be written in
English regardless of narration language, since they are search queries
against an English-language stock footage database - Hindi search terms
will return no results."""
    else:
        language_instruction = 'LANGUAGE: Write everything in English.'

    prompt = f"""You write scripts for YouTube Shorts in the niche: {niche}.
Topic: {topic}

{language_instruction}

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
    - "visual_prompts": array of 2-3 short stock-footage SEARCH PHRASES, in
      English (2-5 words each, like you'd type into a stock video site -
      e.g. "street food market night", "airplane window clouds", "smartphone
      close up hands"), each a DIFFERENT quick shot/angle/moment illustrating
      this line (not near-duplicates of each other). Keep these concrete and
      common enough that real stock footage of them plausibly exists -
      avoid overly specific or abstract phrasing

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


async def synthesize_voice(text, out_path, language="en", rate="+18%"):
    # Slightly faster than default speaking rate - matches the punchier,
    # quick-cut editing style rather than a slow, deliberate voiceover.
    voice = VOICES.get(language, VOICES["en"])
    communicate = edge_tts.Communicate(text, voice, rate=rate)
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


def search_pexels_video(query):
    """Searches Pexels' free video API for real stock footage matching the
    query. Returns (video_file_url, source_duration_seconds) or None if no
    key is configured, nothing matched, or the request failed - callers
    should fall back to the AI image approach in that case."""
    api_key = os.environ.get("PEXELS_API_KEY")
    if not api_key:
        return None
    try:
        r = requests.get(
            "https://api.pexels.com/videos/search",
            headers={"Authorization": api_key},
            params={"query": query, "orientation": "portrait", "per_page": 3},
            timeout=20,
        )
        if r.status_code != 200:
            return None
        videos = r.json().get("videos", [])
        if not videos:
            return None
        video = random.choice(videos)
        files = [f for f in video.get("video_files", []) if f.get("height", 0) >= 720]
        if not files:
            files = video.get("video_files", [])
        if not files:
            return None
        files.sort(key=lambda f: f.get("height", 0))
        chosen = files[len(files) // 2]  # middling quality/size, not the largest
        return chosen["link"], video.get("duration", 10)
    except requests.exceptions.RequestException:
        return None


def download_file(url, out_path, timeout=60):
    r = requests.get(url, timeout=timeout, stream=True)
    r.raise_for_status()
    with open(out_path, "wb") as f:
        for chunk in r.iter_content(chunk_size=1 << 16):
            f.write(chunk)


def trim_and_scale_clip(src_path, start, duration, out_path):
    subprocess.run([
        "ffmpeg", "-y", "-ss", str(max(start, 0)), "-i", str(src_path), "-t", str(duration),
        "-vf", f"scale={VIDEO_SIZE[0]}:{VIDEO_SIZE[1]}:force_original_aspect_ratio=increase,"
               f"crop={VIDEO_SIZE[0]}:{VIDEO_SIZE[1]}",
        "-an", "-r", str(FPS), "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out_path)
    ], check=True, capture_output=True)


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


MAX_CUT_DURATION = 1.2  # cap on how long any single visual can hold the screen

# Absolute last-resort AI image prompts if the shot's own prompt fails even
# after download_image()'s internal retries - simple, generic, and much
# more likely to succeed than a specific/unusual prompt during a rough
# patch on the image API's end.
GENERIC_FALLBACK_PROMPTS = [
    "scenic nature landscape",
    "modern city skyline",
    "cozy lifestyle background",
    "colorful abstract background",
]


def make_solid_color_clip(duration, out_path, color="0x1a1a2e"):
    """Absolute last resort if literally nothing else works for a shot (no
    stock match, no AI image even with a generic fallback prompt, and no
    earlier clip in this video to reuse) - a plain background keeps the
    pipeline from crashing entirely over one shot."""
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi",
        "-i", f"color=c={color}:s={VIDEO_SIZE[0]}x{VIDEO_SIZE[1]}:d={duration}:r={FPS}",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out_path)
    ], check=True, capture_output=True)


def duplicate_clip_to_duration(src_path, target_duration, out_path):
    """Re-uses a previously successful clip from earlier in this same video
    to fill a shot slot whose own visual fetch failed entirely. This keeps
    total video length in sync with the (fixed-length) voiceover instead of
    leaving a gap - losing a fraction of a second of visual variety on one
    shot out of 20-30 is unnoticeable; a truncated voiceover or a crashed
    run is not."""
    probe = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(src_path)],
        capture_output=True, text=True, check=True,
    )
    src_dur = float(probe.stdout.strip())
    if src_dur >= target_duration:
        subprocess.run([
            "ffmpeg", "-y", "-i", str(src_path), "-t", str(target_duration),
            "-c", "copy", str(out_path)
        ], check=True, capture_output=True)
    else:
        loops = int(target_duration // src_dur) + 1
        subprocess.run([
            "ffmpeg", "-y", "-stream_loop", str(loops), "-i", str(src_path),
            "-t", str(target_duration), "-c", "copy", str(out_path)
        ], check=True, capture_output=True)


def prepare_shot_clips(prompt, shot_dur, work_dir, index, fallback_clip=None):
    """Returns a list of ready-made mp4 clips (already trimmed/scaled to the
    target vertical size) covering shot_dur total. Tries real stock footage
    from Pexels first (each sub-cut takes a different time-slice of the same
    downloaded clip, so repeats show fresh motion rather than a frozen
    frame); falls back to an AI-generated image with a Ken Burns zoom if no
    stock match is available or Pexels isn't configured.

    If the AI image fails even after its own internal retries (e.g. the
    image API is having a rough patch), this never raises - it cascades
    through a generic fallback prompt, then reusing the last successful
    clip from this video, then a plain color background as an absolute
    last resort. One flaky image request should never crash an entire
    video/upload slot when MAX_ATTEMPTS=1 makes that expensive to redo.
    """
    n_splits = max(1, round(shot_dur / MAX_CUT_DURATION))
    sub_dur = shot_dur / n_splits

    result = search_pexels_video(prompt)
    if result:
        video_url, src_duration = result
        src_path = work_dir / f"src_{index}.mp4"
        try:
            download_file(video_url, src_path)
            clips = []
            for j in range(n_splits):
                start = min(j * sub_dur, max(0, src_duration - sub_dur - 0.1))
                out_path = work_dir / f"clip_{index}_{j}.mp4"
                trim_and_scale_clip(src_path, start, sub_dur, out_path)
                clips.append(out_path)
            return clips, "stock"
        except Exception:
            pass  # fall through to the AI-image fallback below

    img_path = work_dir / f"img_{index}.jpg"
    try:
        download_image(prompt, img_path)
    except Exception as e:
        print(f"    AI image failed for {prompt!r}: {e}", flush=True)
        fallback_prompt = random.choice(GENERIC_FALLBACK_PROMPTS)
        print(f"    Retrying with generic fallback prompt: {fallback_prompt!r}", flush=True)
        try:
            download_image(fallback_prompt, img_path)
        except Exception as e2:
            print(f"    Generic fallback also failed: {e2}", flush=True)
            clips = []
            for j in range(n_splits):
                out_path = work_dir / f"clip_{index}_{j}.mp4"
                if fallback_clip is not None:
                    duplicate_clip_to_duration(fallback_clip, sub_dur, out_path)
                else:
                    make_solid_color_clip(sub_dur, out_path)
                clips.append(out_path)
            source = "reused" if fallback_clip is not None else "placeholder"
            print(f"    Shot {index} degraded to fallback source: {source}", flush=True)
            return clips, source

    clips = []
    for j in range(n_splits):
        out_path = work_dir / f"clip_{index}_{j}.mp4"
        make_ken_burns_clip(img_path, sub_dur, out_path, zoom_in=(j % 2 == 0))
        clips.append(out_path)
    return clips, "ai_image"


def build_video(clip_paths, voice_path, srt_path, out_path):
    """clip_paths are already-prepared mp4 clips (from prepare_shot_clips),
    at their final target duration/resolution - just concatenate + caption +
    mux audio."""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
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


def write_srt(beats, durations, out_path, language="en"):
    """Writes an .ass subtitle file (despite the name, kept for compatibility
    with callers) with the caption style baked into the file header - no
    CLI-side force_style escaping needed.

    Fontname must actually have glyphs for the script being rendered -
    Devanagari (Hindi) needs a dedicated font or it renders as blank boxes.
    The CI workflow installs "fonts-noto-core" via apt, which bundles both
    "Noto Sans" and "Noto Sans Devanagari"."""

    def fmt(t):
        h = int(t // 3600); m = int((t % 3600) // 60); s = t % 60
        cs = int((t - int(t)) * 100)
        return f"{h:d}:{m:02d}:{int(s):02d}.{cs:02d}"

    font = "Noto Sans Devanagari" if language == "hi" else "Noto Sans"

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font},64,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,3,3,0,2,60,60,140,1

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


def get_youtube_client():
    creds = Credentials(
        None,
        refresh_token=os.environ["YT_REFRESH_TOKEN"],
        client_id=os.environ["YT_CLIENT_ID"],
        client_secret=os.environ["YT_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
    )
    return build("youtube", "v3", credentials=creds)


def upload_to_youtube(video_path, title, description, tags):
    youtube = get_youtube_client()

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


PERFORMANCE_FILE = ROOT / "performance_log.json"
MIN_MATURITY_HOURS = 20   # give a video time to accumulate real views/likes
                          # before it counts toward steering future topics
MIN_SAMPLES_PER_CATEGORY = 3  # below this, treat the category as unproven
                               # and keep exploring it rather than trusting
                               # a tiny/noisy sample


def load_performance_log():
    if PERFORMANCE_FILE.exists():
        return json.loads(PERFORMANCE_FILE.read_text())
    return {"videos": []}


def record_performance_entry(video_id, category, language, topic):
    """Called right after a successful upload so we can look up its real
    view/like counts later and use them to bias future topic selection."""
    if not video_id:
        return
    data = load_performance_log()
    data["videos"].append({
        "video_id": video_id,
        "category": category,
        "language": language,
        "topic": topic,
        "posted_at": time.time(),
    })
    PERFORMANCE_FILE.write_text(json.dumps(data, indent=2))


def compute_category_weights():
    """Fetches current view/like counts (cheap - 1 quota unit per 50 videos,
    nothing like the 1600-unit cost of an upload) for past videos old enough
    to have accumulated real stats, and turns them into a weighted-random
    distribution favoring categories that have historically performed
    better. Categories with too few mature samples get a neutral weight
    instead of zero, so we keep exploring them rather than writing them off
    on noise. Fails open (equal weights) on any error - this is an
    optimization, never something that should block a run."""
    categories = list(CATEGORY_SUBREDDITS.keys())
    equal_weights = {c: 1.0 for c in categories}

    try:
        data = load_performance_log()
        now = time.time()
        mature = [
            v for v in data["videos"]
            if now - v.get("posted_at", 0) >= MIN_MATURITY_HOURS * 3600
        ]
        if not mature:
            return equal_weights

        youtube = get_youtube_client()
        stats_by_id = {}
        ids = [v["video_id"] for v in mature]
        for i in range(0, len(ids), 50):
            batch = ids[i:i + 50]
            resp = youtube.videos().list(part="statistics", id=",".join(batch)).execute()
            for item in resp.get("items", []):
                stats_by_id[item["id"]] = item.get("statistics", {})

        scores_by_category = {c: [] for c in categories}
        for v in mature:
            stats = stats_by_id.get(v["video_id"])
            if not stats:
                continue
            views = int(stats.get("viewCount", 0))
            likes = int(stats.get("likeCount", 0))
            # Likes weighted heavier than raw views - a like is a much
            # stronger engagement signal than a passive view/impression.
            score = views + likes * 10
            cat = v.get("category")
            if cat in scores_by_category:
                scores_by_category[cat].append(score)

        all_scores = [s for lst in scores_by_category.values() for s in lst]
        overall_avg = (sum(all_scores) / len(all_scores)) if all_scores else 1.0

        weights = {}
        for c in categories:
            lst = scores_by_category[c]
            if len(lst) >= MIN_SAMPLES_PER_CATEGORY:
                weights[c] = max(sum(lst) / len(lst), 1.0)
            else:
                weights[c] = overall_avg  # not enough data yet - keep exploring
        return weights
    except Exception as e:
        print(f"compute_category_weights failed, falling back to equal weights: {e}", flush=True)
        return equal_weights


def pick_category(weights):
    categories = list(weights.keys())
    return random.choices(categories, weights=[weights[c] for c in categories], k=1)[0]


class UnsafeTopicError(Exception):
    pass


def run_once(topic, niche, language="en", category=None):
    """Runs the full pipeline for a single topic: script -> safety check ->
    voice -> images -> video -> upload. Raises on any failure; caller decides
    whether to retry with a different topic."""
    work = Path(tempfile.mkdtemp())
    print("Topic:", topic, flush=True)
    print("Language:", language, flush=True)
    print("Category:", category, flush=True)

    print("Generating script (Groq)...", flush=True)
    script = generate_script(topic, niche, language=language)
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
    asyncio.run(synthesize_voice(full_text, voice_path, language=language))

    total_dur = get_audio_duration(voice_path)
    print(f"Voiceover ready: {total_dur:.1f}s", flush=True)

    # Proportional pacing: a beat with more words gets more screen time,
    # instead of every beat getting an equal slice regardless of length.
    word_counts = [max(len(b["line"].split()), 1) for b in beats]
    total_words = sum(word_counts)
    beat_durations = [total_dur * (wc / total_words) for wc in word_counts]

    srt_path = work / "captions.ass"
    write_srt(beats, beat_durations, srt_path, language=language)

    # Fast quick-cut editing: each beat gets 2-3 distinct visual shots
    # (downloaded images) instead of one static image for its whole duration.
    shots = []  # list of (prompt, shot_duration)
    for beat, beat_dur in zip(beats, beat_durations):
        prompts = beat.get("visual_prompts") or [beat.get("visual_prompt", beat["line"])]
        prompts = prompts[:3] or [beat["line"]]
        shot_dur = beat_dur / len(prompts)
        for p in prompts:
            shots.append((p, shot_dur))

    # Sequential, not parallel: Pollinations' free tier (used only as the
    # fallback when no stock footage matches) enforces a hard "max 1 queued
    # request per IP" limit - any concurrency trips it immediately. Pexels'
    # free tier is far more generous, but we keep this paced regardless.
    print(f"Fetching {len(shots)} visuals (stock footage, falling back to AI images)...", flush=True)
    all_clip_paths = []
    stock_count = ai_count = degraded_count = 0
    last_successful_clip = None
    for i, (prompt, shot_dur) in enumerate(shots):
        print(f"  shot {i+1}/{len(shots)}: {prompt[:60]!r}...", flush=True)
        clips, source = prepare_shot_clips(prompt, shot_dur, work, i, fallback_clip=last_successful_clip)
        all_clip_paths.extend(clips)
        if source == "stock":
            stock_count += 1
            last_successful_clip = clips[-1]
        elif source == "ai_image":
            ai_count += 1
            last_successful_clip = clips[-1]
        else:
            degraded_count += 1  # "reused" or "placeholder" - don't update
                                  # last_successful_clip, it's already a fallback
        time.sleep(0.4)
    print(f"All visuals ready ({stock_count} stock footage, {ai_count} AI image, "
          f"{degraded_count} degraded fallback).", flush=True)

    print(f"Assembling video: {len(all_clip_paths)} quick cuts, {total_dur:.1f}s total "
          f"(ffmpeg encoding - this takes a bit)...", flush=True)
    out_video = work / "short.mp4"
    build_video(all_clip_paths, voice_path, srt_path, out_video)
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
    hashtags = build_hashtags(script["hashtags"])
    video_id = upload_to_youtube(
        out_video,
        title=script["title"],
        description=script["description"] + "\n\n" + " ".join(f"#{h}" for h in hashtags),
        tags=hashtags,
    )
    record_performance_entry(video_id, category, language, topic)


def main():
    max_attempts = int(os.environ.get("MAX_ATTEMPTS", "3"))
    last_error = None

    # Picked once per invocation (not per attempt) and persisted in
    # topics.json, so it alternates en/hi across separate scheduled runs.
    language = pick_language()

    # Weighted by how travel/food/tech videos have actually performed so
    # far (views + likes on videos old enough to have real stats) - see
    # compute_category_weights(). Categories with too little data yet get a
    # neutral weight so we keep exploring instead of over-committing early.
    weights = compute_category_weights()
    category = pick_category(weights)
    print(f"Category weights: { {k: round(v, 1) for k, v in weights.items()} }", flush=True)
    print(f"Chosen category: {category}", flush=True)

    for attempt in range(1, max_attempts + 1):
        # First attempt uses trending (if enabled). Retries force the static
        # topics.json rotation instead - trending would likely just return
        # the same top candidate again and fail the same way.
        force_static = attempt > 1
        topic, niche = pick_topic(category, force_static=force_static)

        try:
            run_once(topic, niche, language=language, category=category)
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
