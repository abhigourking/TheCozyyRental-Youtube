"""
End-to-end pipeline: pick a topic -> generate script (Gemini) -> voiceover
(Edge TTS) -> images (Pollinations.ai) -> assemble vertical video with
captions (ffmpeg) -> upload to YouTube as a Short.

Env vars required:
    GEMINI_API_KEY      - from https://aistudio.google.com/apikey
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
import google.generativeai as genai
import edge_tts

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

ROOT = Path(__file__).parent
TOPICS_FILE = ROOT / "topics.json"
VOICE = "en-US-GuyNeural"
VIDEO_SIZE = (1080, 1920)


def pick_topic():
    data = json.loads(TOPICS_FILE.read_text())
    topics = data["topics"]
    idx = data.get("next_index", 0) % len(topics)
    topic = topics[idx]
    data["next_index"] = (idx + 1) % len(topics)
    TOPICS_FILE.write_text(json.dumps(data, indent=2))
    return topic, data["niche"]


def generate_script(topic, niche):
    genai.configure(api_key=os.environ["GEMINI_API_KEY"])
    model = genai.GenerativeModel("gemini-2.0-flash")

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
Output ONLY the JSON, no markdown fences."""

    resp = model.generate_content(prompt)
    text = resp.text.strip()
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

        subprocess.run([
            "ffmpeg", "-y", "-i", str(silent_video), "-i", str(voice_path),
            "-vf", f"subtitles={srt_path}:force_style='FontSize=16,PrimaryColour=&HFFFFFF&,"
                   f"OutlineColour=&H000000&,BorderStyle=3,Alignment=2,MarginV=120'",
            "-c:v", "libx264", "-c:a", "aac", "-shortest", str(out_path)
        ], check=True)


def write_srt(beats, durations, out_path):
    def fmt(t):
        h = int(t // 3600); m = int((t % 3600) // 60); s = t % 60
        return f"{h:02d}:{m:02d}:{s:06.3f}".replace(".", ",")

    lines = []
    t = 0.0
    for i, (beat, dur) in enumerate(zip(beats, durations), start=1):
        start, end = t, t + dur
        lines.append(str(i))
        lines.append(f"{fmt(start)} --> {fmt(end)}")
        lines.append(beat["line"])
        lines.append("")
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


def main():
    work = Path(tempfile.mkdtemp())
    topic, niche = pick_topic()
    print("Topic:", topic)

    script = generate_script(topic, niche)
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
    srt_path = work / "captions.srt"
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


if __name__ == "__main__":
    main()
