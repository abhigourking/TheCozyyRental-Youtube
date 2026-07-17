"""
Step 1 of the hybrid manual workflow.

Picks a topic (trending or from topics.json), writes a script via Groq, and
prints a numbered shot list ready to paste into a free AI video web tool
(Sora, Kling, Hailuo, Pika, etc - whichever you have access to). It also
generates the voiceover and captions ahead of time (fully automatable, no
manual step needed for those) and saves everything to a job folder.

Usage:
    python prepare_video_job.py

Then, for each shot printed:
    1. Paste the prompt into your chosen AI video tool's web UI
    2. Generate a ~5-10 second clip
    3. Download it and save it into the printed clips folder as
       clip_1.mp4, clip_2.mp4, clip_3.mp4, etc (matching the shot numbers)

Once all clips are saved, run:
    python finish_and_upload.py <job_id>
"""

import os
import re
import json
import asyncio
import uuid
from pathlib import Path

import requests

from generate_short import (
    pick_topic, synthesize_voice, get_audio_duration, write_srt,
    is_safe_topic, find_blocked_match,
)

ROOT = Path(__file__).parent
QUEUE_DIR = ROOT / "queue"


def generate_manual_video_script(topic, niche):
    """Like generate_script() in generate_short.py, but tuned for the manual
    workflow: fewer, longer beats (5-8, ~8-10s of narration each) matching
    typical free-tier AI video clip length caps, and richer cinematic
    "video_prompt" descriptions (camera movement, action) instead of short
    stock-search keywords."""
    prompt = f"""You write scripts for YouTube Shorts in the niche: {niche}.
Topic: {topic}

This needs to be a full Short, roughly 45-60 seconds when read aloud.
Structure: a scroll-stopping hook, brief context, 3-5 concrete/useful main
points, then a call-to-action closer.

Return STRICT JSON with keys:
- "title": catchy YouTube title, under 90 chars
- "description": 2-3 sentence description with a call to action
- "hashtags": array of 5 relevant hashtags (no # symbol)
- "beats": array of 5-8 objects, each with:
    - "line": 2-3 sentences of narration for this beat (conversational,
      punchy, roughly 20-30 words - this is a bigger chunk than usual since
      each beat maps to ONE longer AI-generated video clip, not a quick cut)
    - "video_prompt": a rich, cinematic text-to-VIDEO prompt (not a static
      image prompt) describing camera movement and action for this beat,
      e.g. "Slow dolly-in on two kids painting rocks at a sunny backyard
      table, warm afternoon light, shallow depth of field" - concrete,
      visual, includes motion/camera direction

Target 140-200 words of total narration. First line must be a strong hook.
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


def main():
    QUEUE_DIR.mkdir(exist_ok=True)

    topic, niche = pick_topic()
    print(f"Topic: {topic}\n")

    script = generate_manual_video_script(topic, niche)

    full_check_text = script["title"] + " " + script["description"] + " " + \
        " ".join(b["line"] for b in script["beats"])
    if not is_safe_topic(full_check_text):
        match = find_blocked_match(full_check_text)
        raise SystemExit(f"Safety check failed - matched {match[0]!r}. Re-run to try a different topic.")

    beats = script["beats"]
    full_text = " ".join(b["line"] for b in beats)

    job_id = uuid.uuid4().hex[:8]
    job_dir = QUEUE_DIR / job_id
    clips_dir = job_dir / "clips"
    clips_dir.mkdir(parents=True)

    print("Generating voiceover...")
    voice_path = job_dir / "voice.mp3"
    asyncio.run(synthesize_voice(full_text, voice_path))
    total_dur = get_audio_duration(voice_path)

    word_counts = [max(len(b["line"].split()), 1) for b in beats]
    total_words = sum(word_counts)
    beat_durations = [total_dur * (wc / total_words) for wc in word_counts]

    srt_path = job_dir / "captions.ass"
    write_srt(beats, beat_durations, srt_path)

    meta = {
        "job_id": job_id,
        "topic": topic,
        "title": script["title"],
        "description": script["description"],
        "hashtags": script["hashtags"],
        "beat_durations": beat_durations,
        "num_beats": len(beats),
    }
    (job_dir / "meta.json").write_text(json.dumps(meta, indent=2))

    print(f"\n{'=' * 70}")
    print(f"JOB ID: {job_id}")
    print(f"Title: {script['title']}")
    print(f"Total narration length: {total_dur:.1f}s across {len(beats)} shots")
    print(f"{'=' * 70}\n")
    print("Paste each prompt below into your AI video tool, generate a clip")
    print(f"(should be roughly {total_dur/len(beats):.0f}s each, but any length is fine -")
    print("it'll be trimmed/looped to fit automatically), and save it as:")
    print(f"  {clips_dir}/clip_<N>.mp4\n")

    for i, (beat, dur) in enumerate(zip(beats, beat_durations), start=1):
        print(f"--- Shot {i}/{len(beats)} (~{dur:.1f}s) -> save as clip_{i}.mp4 ---")
        print(f"Narration: {beat['line']}")
        print(f"Video prompt: {beat['video_prompt']}\n")

    print(f"Once all {len(beats)} clips are saved, run:")
    print(f"  python finish_and_upload.py {job_id}")


if __name__ == "__main__":
    main()
