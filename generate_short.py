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
import subprocess
import tempfile
import shutil
import asyncio
from pathlib import Path

import requests
import edge_tts

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

ROOT = Path(__file__).parent
TOPICS_FILE = ROOT / "topics.json"
VOICE = "en-US-GuyNeural"
VIDEO_SIZE = (1080, 1920)

# Subreddits relevant to the channel niche - only pull "trending" candidates
# from here, not open internet trends, to keep topics on-niche and reduce risk.
TRENDING_SUBREDDITS = ["AirBnB", "travel", "vacationrentals", "digitalnomad"]

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

_BLOCKED_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(w) for w in BLOCKED_KEYWORDS) + r")\b",
    re.IGNORECASE,
)


def is_safe_topic(title):
    return _BLOCKED_PATTERN.search(title) is None


def get_trending_topic():
    """Try to pull a safe, on-niche trending topic from Reddit's public JSON
    API (no key required). Falls back to None if nothing suitable is found,
    in which case the caller should fall back to the static topics.json list.
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
                if not is_safe_topic(title):
                    continue
                candidates.append((d.get("ups", 0), title))
        except Exception:
            continue  # this subreddit failed, just skip it

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


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

Return STRICT JSON with keys:
- "title": catchy YouTube title, under 90 chars
- "description": 2-3 sentence description with a call to action
- "hashtags": array of 5 relevant hashtags (no # symbol)
- "beats": array of 4-6 objects, each with:
    - "line": one sentence of narration (conversational, punchy, no filler)
    - "visual_prompt": a short text-to-image prompt describing what should be shown

Keep total narration under 130 words. First line must be a strong hook.
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


async def synthesize_voice(text, out_path):
    communicate = edge_tts.Communicate(text, VOICE)
    await communicate.save(str(out_path))


def download_image(prompt, out_path, width=1080, height=1920):
    url = f"https://image.pollinations.ai/prompt/{requests.utils.quote(prompt)}"
    params = {"width": width, "height": height, "nologo": "true"}
    r = requests.get(url, params=params, timeout=60)
    r.raise_for_status()
    out_path.write_bytes(r.content)


def get_audio_duration(path):
    out = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True
    )
    return float(out.stdout.strip())


def build_video(beats, image_paths, voice_path, srt_path, out_path):
    total_dur = get_audio_duration(voice_path)
    per_image = total_dur / len(image_paths)

    with tempfile.TemporaryDirectory() as td:
        concat_file = Path(td) / "concat.txt"
        lines = []
        for img in image_paths:
            lines.append(f"file '{img}'")
            lines.append(f"duration {per_image}")
        lines.append(f"file '{image_paths[-1]}'")
        concat_file.write_text("\n".join(lines))

        silent_video = Path(td) / "silent.mp4"
        subprocess.run([
            "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_file),
            "-vf", f"scale={VIDEO_SIZE[0]}:{VIDEO_SIZE[1]}:force_original_aspect_ratio=increase,"
                   f"crop={VIDEO_SIZE[0]}:{VIDEO_SIZE[1]}",
            "-r", "30", "-pix_fmt", "yuv420p", str(silent_video)
        ], check=True)

        # Style is baked into the .ass file itself (see write_ass) instead of
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
    print("Topic:", topic)

    script = generate_script(topic, niche)

    full_check_text = script["title"] + " " + script["description"] + " " + \
        " ".join(b["line"] for b in script["beats"])
    if not is_safe_topic(full_check_text):
        raise UnsafeTopicError(f"Generated script for topic {topic!r} failed the safety check")

    beats = script["beats"]
    full_text = " ".join(b["line"] for b in beats)

    voice_path = work / "voice.mp3"
    asyncio.run(synthesize_voice(full_text, voice_path))

    image_paths = []
    for i, beat in enumerate(beats):
        img_path = work / f"img_{i}.jpg"
        download_image(beat["visual_prompt"], img_path)
        image_paths.append(img_path)

    total_dur = get_audio_duration(voice_path)
    per_beat = total_dur / len(beats)
    durations = [per_beat] * len(beats)
    srt_path = work / "captions.ass"
    write_srt(beats, durations, srt_path)

    out_video = work / "short.mp4"
    build_video(beats, image_paths, voice_path, srt_path, out_video)

    final_path = ROOT / "output"
    final_path.mkdir(exist_ok=True)
    shutil.copy(out_video, final_path / "latest_short.mp4")

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
