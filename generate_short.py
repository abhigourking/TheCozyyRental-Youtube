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
import sys
import json
import re
import random
import time
import subprocess
import tempfile
import shutil
import asyncio
import threading
import traceback
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

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

# Native-language narration for country-specific videos, for authenticity.
# Every other country video is narrated in that country's main language
# instead of English (see pick_use_native()); captions stay in English
# either way, so the video is still watchable by an English-speaking
# audience and the subtitle font never needs non-Latin glyphs.
#
# "voice" values are Microsoft Edge TTS neural voices. If any voice name
# here is wrong/retired, synthesis raises and run_once() falls back to the
# English voice + English lines automatically - a bad entry costs one
# English video, never a failed run. (These couldn't be verified against
# the live Edge TTS voice list from this sandbox, since Microsoft's TTS
# endpoint isn't reachable here - the fallback path is the safety net.)
COUNTRY_LANGUAGES = {
    "Japan":       {"name": "Japanese",              "voice": "ja-JP-KeitaNeural"},
    "Italy":       {"name": "Italian",               "voice": "it-IT-DiegoNeural"},
    "France":      {"name": "French",                "voice": "fr-FR-HenriNeural"},
    "Thailand":    {"name": "Thai",                  "voice": "th-TH-NiwatNeural"},
    "Mexico":      {"name": "Mexican Spanish",       "voice": "es-MX-JorgeNeural"},
    "India":       {"name": "Hindi",                 "voice": "hi-IN-MadhurNeural"},
    "Vietnam":     {"name": "Vietnamese",            "voice": "vi-VN-NamMinhNeural"},
    "Greece":      {"name": "Greek",                 "voice": "el-GR-NestorasNeural"},
    "Turkey":      {"name": "Turkish",               "voice": "tr-TR-AhmetNeural"},
    "Spain":       {"name": "European Spanish",      "voice": "es-ES-AlvaroNeural"},
    "Indonesia":   {"name": "Indonesian",            "voice": "id-ID-ArdiNeural"},
    "Peru":        {"name": "Peruvian Spanish",      "voice": "es-PE-AlexNeural"},
    "Morocco":     {"name": "Moroccan Arabic",       "voice": "ar-MA-JamalNeural"},
    "South Korea": {"name": "Korean",                "voice": "ko-KR-InJoonNeural"},
    "Brazil":      {"name": "Brazilian Portuguese",  "voice": "pt-BR-AntonioNeural"},
    "Portugal":    {"name": "European Portuguese",   "voice": "pt-PT-DuarteNeural"},
    "Egypt":       {"name": "Egyptian Arabic",       "voice": "ar-EG-ShakirNeural"},
    "Philippines": {"name": "Filipino",              "voice": "fil-PH-AngeloNeural"},
    "Argentina":   {"name": "Argentinian Spanish",   "voice": "es-AR-TomasNeural"},
    "Iceland":     {"name": "Icelandic",             "voice": "is-IS-GunnarNeural"},
}

# Curated for the travel / food niche only - visually rich subjects that
# read well as fast photo/video slideshows. Deliberately excludes r/news,
# r/worldnews, r/politics, r/PublicFreakout etc. so we don't even fetch
# heavy news/tragedy/political content in the first place. This is the
# primary defense; the SENSITIVE_KEYWORDS filter below is the backup.
# Split by category (rather than one flat list) so performance data can bias
# which category gets picked next - see compute_category_weights(). Which
# category gets picked each run is random (weighted by past performance, see
# pick_category()) - not a fixed sequence of phases.
#
# tech/ai/animals were removed (travel + food only, per request) - both
# remaining categories are country-rotated (see COUNTRY_TOPIC_TEMPLATES
# below), so every video now goes through the country rotation.
CATEGORY_SUBREDDITS = {
    "travel": ["travel", "backpacking", "itookapicture", "hiking"],
    "food": ["food", "recipes", "EatCheapAndHealthy"],
}

# Rotated through (round-robin, state persisted in topics.json) to keep
# travel/food content fresh and globally varied instead of a fixed, generic
# topic pool - a broad, safe, widely-recognizable spread across continents.
COUNTRIES = [
    "Japan", "Italy", "France", "Thailand", "Mexico", "India", "Vietnam",
    "Greece", "Turkey", "Spain", "Indonesia", "Peru", "Morocco",
    "South Korea", "Brazil", "Portugal", "Egypt", "Philippines",
    "Argentina", "Iceland",
]

# {country} gets filled in with the current pick_country() result. Several
# per category so the same country doesn't produce the same phrasing twice
# in a row as it cycles back around. travel/food share the same rotating
# pick_country() pointer (round-robin across all 20 countries), rather than
# each category tracking its own separate position.
COUNTRY_TOPIC_TEMPLATES = {
    "travel": [
        "A viral travel spot in {country} that's all over social media right now",
        "A hidden gem destination in {country} most tourists don't know about",
        "The most breathtaking place to visit in {country}",
        "A bucket-list experience you can only have in {country}",
        "A surprising fact about a famous tourist attraction in {country}",
    ],
    "food": [
        "The most viral street food trend in {country} right now",
        "A must-try traditional dish from {country} and the story behind it",
        "A popular food market or food street worth visiting in {country}",
        "A comfort food from {country} that's become an internet obsession",
        "A unique regional specialty from {country} most outsiders have never heard of",
    ],
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
    "gambling", "casino", "betting", "racist", "racism",
    "nazi", "hate crime", "child abuse", "minor", "underage",
]
# ("bet" on its own was removed - "bet you didn't know", "you bet" etc. are
# extremely common casual phrasing in this niche's generated scripts and
# have nothing to do with gambling. "gambling"/"casino"/"betting" already
# cover the actual gambling-content case.)

# Common benign idioms/compound phrases that would otherwise false-positive
# against BLOCKED_KEYWORDS above and kill an entirely safe run - e.g. "this
# dish is a flavor bomb" tripped "bomb" and failed a completely fine Spanish
# food video. Stripped out of the text before the blocklist regex runs, so a
# genuinely concerning use of the same underlying word is still caught (a
# standalone "bomb" with no "flavor"/"photo"/"bath" in front of it still
# blocks normally).
SAFE_IDIOMS = [
    "flavor bomb", "flavour bomb", "flavor bombs", "flavour bombs",
    "photo bomb", "photobomb", "photobombed", "photobombing", "bath bomb",
    "f-bomb", "f bomb",
    "photo shoot", "photoshoot", "video shoot", "camera shoot", "night shoot",
    "shooting star", "shooting stars",
    "glue gun", "staple gun", "spray gun", "water gun", "nail gun", "top gun",
    "secret weapon",
    "naked eye",
    "kill two birds",
    "steal the show", "steal your heart", "steal the spotlight",
]
_SAFE_IDIOM_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(p) for p in SAFE_IDIOMS) + r")\b",
    re.IGNORECASE,
)


def _strip_safe_idioms(text):
    return _SAFE_IDIOM_PATTERN.sub(" ", text)

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
    return _BLOCKED_PATTERN.search(_strip_safe_idioms(title)) is None


def find_blocked_match(text):
    """Returns the matched keyword + surrounding snippet for diagnostics,
    or None if nothing matched. Used to debug false positives without
    needing to print/expose the entire generated script. Operates on the
    same idiom-stripped text is_safe_topic checks, so the reported match is
    always a genuine one, never a "flavor bomb"-style false positive."""
    stripped = _strip_safe_idioms(text)
    m = _BLOCKED_PATTERN.search(stripped)
    if not m:
        return None
    start = max(0, m.start() - 25)
    end = min(len(stripped), m.end() + 25)
    return m.group(0), stripped[start:end]


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


def pick_country():
    """Round-robin through COUNTRIES, state persisted in topics.json (like
    pick_language()) so it advances one-per-run across separate scheduled
    invocations rather than resetting each time.

    Once every country has been picked at least once (a full 20-country
    cycle), permanently switches to performance-weighted selection via
    compute_country_weights() - "once we're done with all the countries,
    start shortlisting the ones getting the most views." The
    "country_cycle_complete" flag in topics.json makes that switch
    one-directional: it never reverts to round-robin, it just keeps
    re-weighting as more videos mature."""
    data = json.loads(TOPICS_FILE.read_text())

    if data.get("country_cycle_complete", False):
        weights = compute_country_weights()
        country = random.choices(
            COUNTRIES, weights=[weights[c] for c in COUNTRIES], k=1
        )[0]
        # next_country_index no longer drives selection once weighted, but
        # keep advancing it anyway - harmless, and useful if you ever want
        # to fall back to round-robin by clearing the flag.
        idx = data.get("next_country_index", 0) % len(COUNTRIES)
        data["next_country_index"] = (idx + 1) % len(COUNTRIES)
        TOPICS_FILE.write_text(json.dumps(data, indent=2))
        return country

    idx = data.get("next_country_index", 0) % len(COUNTRIES)
    country = COUNTRIES[idx]
    next_idx = (idx + 1) % len(COUNTRIES)
    data["next_country_index"] = next_idx
    if next_idx == 0:
        # We just picked the last country in the rotation - every country
        # has now been posted at least once. Flip on weighted selection.
        data["country_cycle_complete"] = True
        print("Country rotation: full cycle complete - switching to "
              "performance-weighted country selection from here on.", flush=True)
    TOPICS_FILE.write_text(json.dumps(data, indent=2))
    return country


def pick_use_native():
    """Alternates native-language / English narration every run, state
    persisted in topics.json so it survives across separate GitHub Actions
    runs (each run is a fresh checkout - only committed state carries over).
    Returns True when this run should narrate in the country's own language:
    "one English then another video that country's national language"."""
    data = json.loads(TOPICS_FILE.read_text())
    use_native = bool(data.get("next_use_native", False))
    data["next_use_native"] = not use_native
    TOPICS_FILE.write_text(json.dumps(data, indent=2))
    return use_native


def pick_topic(category=None, force_static=False):
    """Returns (topic, niche, country). With only travel/food configured in
    CATEGORY_SUBREDDITS, every category is country-rotated, so country is
    always set here in practice - it's what main() uses to decide which
    native-language voice a country video can be narrated in. The
    non-country branches below (Reddit trending / static fallback) are kept
    as dead-but-working code in case a non-country category is ever added
    back."""
    if category is None:
        category = random.choice(list(CATEGORY_SUBREDDITS.keys()))
    data = json.loads(TOPICS_FILE.read_text())

    # Travel/food always rotate through a specific country instead of using
    # the generic static list or Reddit trending - keeps content fresh and
    # globally varied every single run ("gather more videos about each
    # country's viral travel/food topics, then rotate").
    if category in COUNTRY_TOPIC_TEMPLATES:
        country = pick_country()
        template = random.choice(COUNTRY_TOPIC_TEMPLATES[category])
        topic = template.format(country=country)
        print(f"Country: {country}", flush=True)
        return topic, data["niche"], country

    if not force_static and os.environ.get("USE_TRENDING", "true").lower() == "true":
        trending = get_trending_topic(category)
        if trending:
            return trending, data["niche"], None

    # Static fallback: topics are tagged with a category (see topics.json).
    # Random rather than round-robin - the pool per category is small enough
    # that strict rotation isn't worth the extra state to track.
    topics = data["topics"]
    matching = [t for t in topics if t.get("category") == category]
    if not matching:
        matching = topics  # safety net if a category has no static entries
    topic = random.choice(matching)
    return topic["text"], data["niche"], None


def pick_language():
    """Alternates en/hi every run, state persisted in topics.json so it
    survives across separate GitHub Actions runs (each run is a fresh
    checkout, only topics.json's committed state carries over)."""
    data = json.loads(TOPICS_FILE.read_text())
    lang = data.get("next_language", "en")
    data["next_language"] = "hi" if lang == "en" else "en"
    TOPICS_FILE.write_text(json.dumps(data, indent=2))
    return lang


# Running total for THIS process (one video/run of generate_short.py),
# reset each fresh invocation - not persisted between runs by itself.
# record_performance_entry() writes the final total into
# performance_log.json so it becomes a durable per-video record you can
# sum across a day to see actual usage against Groq's 100k/day cap,
# instead of only ever seeing it in transient Actions logs.
_GROQ_RUN_USAGE = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "calls": 0}


def record_groq_usage(usage):
    _GROQ_RUN_USAGE["prompt_tokens"] += usage.get("prompt_tokens", 0) or 0
    _GROQ_RUN_USAGE["completion_tokens"] += usage.get("completion_tokens", 0) or 0
    _GROQ_RUN_USAGE["total_tokens"] += usage.get("total_tokens", 0) or 0
    _GROQ_RUN_USAGE["calls"] += 1


def get_groq_run_usage():
    return dict(_GROQ_RUN_USAGE)


def _groq_api_keys():
    """Configured Groq API keys, in fallback order: primary, then optional
    secondary (GROQ_API_KEY_2). Groq enforces rate limits per organization,
    not per key - a second key on the SAME Groq account shares the exact
    same exhausted quota, so this only helps if GROQ_API_KEY_2 belongs to a
    genuinely separate account/org (confirmed to be the case here)."""
    keys = []
    primary = os.environ.get("GROQ_API_KEY")
    if primary:
        keys.append(("primary", primary))
    secondary = os.environ.get("GROQ_API_KEY_2")
    if secondary:
        keys.append(("secondary", secondary))
    return keys


def _is_daily_quota_error(resp):
    """True only for Groq's per-day (TPD/RPD) quota exhaustion - the case
    where switching to a different account's key actually helps. A
    per-minute rate limit (RPM/TPM) is transient and clears in seconds on
    its own, so it should just propagate/retry normally rather than burning
    the fallback key on something that wasn't the problem."""
    if resp.status_code != 429:
        return False
    try:
        detail = resp.json().get("error", {}).get("message", "")
    except ValueError:
        detail = resp.text
    detail = detail.lower()
    return "per day" in detail or "tpd" in detail or "rpd" in detail


def generate_script(topic, niche, language="en", native_language_name=None):
    """native_language_name: e.g. "Japanese" - when set, each beat's "line"
    is narration in that language while "line_en" carries the English
    caption text for the same beat. Title/description/hashtags stay English
    for discoverability with a global audience. When None, narration is
    English and "line_en" simply mirrors "line"."""
    if native_language_name:
        language_instruction = f"""LANGUAGE - IMPORTANT, READ CAREFULLY: this video is narrated in
{native_language_name} for authenticity, but captioned in English so an
English-speaking audience can still follow it. So for EVERY beat you must
return BOTH:
  - "line": the narration, written naturally in {native_language_name} (in
    that language's own native script where applicable, NOT romanized).
    Write like a popular local {native_language_name}-speaking YouTube
    creator would actually talk - natural and conversational, never a stiff
    word-for-word textbook translation.
  - "line_en": a faithful English translation of that same line, used as
    the on-screen caption.
"title", "description", and "hashtags" must ALL be in English regardless -
they drive discovery with a global audience.
IMPORTANT EXCEPTION: "visual_prompts" must ALWAYS be in English too, since
they are search queries against an English-language stock footage database -
non-English search terms return no results."""
    elif language == "hi":
        language_instruction = """LANGUAGE: Write "title", "description", and every beat's "line" entirely
in Hindi using Devanagari script (not Hinglish/romanized). Keep the tone
natural and conversational, like a popular Hindi YouTube creator - not a
stiff textbook translation. Hashtags may stay in English (common practice
for reach). Also include "line_en" per beat: a faithful English translation
of that line. IMPORTANT EXCEPTION: "visual_prompts" must ALWAYS be written
in English regardless of narration language, since they are search queries
against an English-language stock footage database - Hindi search terms
will return no results."""
    else:
        language_instruction = ('LANGUAGE: Write everything in English. Set each beat\'s '
                                '"line_en" to the same text as its "line".')

    prompt = f"""You write scripts for YouTube Shorts in the niche: {niche}.
Topic: {topic}

{language_instruction}

This needs to be a punchy Short that runs 15-25 seconds when read aloud at a
brisk pace - NOT shorter. Every word has to earn its place, but do not
undershoot the length either: a 7-10 second video is far too short and will
be rejected. Structure it tight:
1. A scroll-stopping hook in the first line (a bold claim, a question, or
   "nobody tells you this" style opener)
2. 3-5 concrete, specific tips/facts/beats - each genuinely useful, zero
   filler, zero build-up/throat-clearing
3. A quick closing line with a call to action (follow for more, comment your
   experience, etc.)

This should feel like a fast-paced, addictive, quick-cut viral Short (think
TikTok/Reels editing style) - visuals should change every 1.5-2.5 seconds,
NOT one slow static image per sentence. To achieve that, give each beat
MULTIPLE quick visual shots instead of just one.

Return STRICT JSON with keys:
- "title": catchy YouTube title, under 90 chars (ALWAYS in English)
- "description": 2-3 sentence description with a call to action (ALWAYS in English)
- "hashtags": array of 5 relevant hashtags, no # symbol (ALWAYS in English)
- "beats": array of 5-7 objects (following the structure above). EVERY beat
  object MUST have all three of these keys, always, with no exceptions:
    - "line": one sentence of narration in the language specified above
      (conversational, punchy, no filler, roughly 12-16 words each - not
      shorter, these must add up to a 15-25 second read)
    - "line_en": the English version of that same line. If the language
      specified above is already English, "line_en" must be IDENTICAL to
      "line" - never omit this key even then.
    - "visual_prompts": array of 2-3 short stock-footage SEARCH PHRASES, in
      English (2-5 words each, like you'd type into a stock video site -
      e.g. "street food market night", "airplane window clouds", "smartphone
      close up hands"), each a DIFFERENT quick shot/angle/moment illustrating
      this line (not near-duplicates of each other). Keep these concrete and
      common enough that real stock footage of them plausibly exists -
      avoid overly specific or abstract phrasing. NEVER suggest statues,
      sculptures, classical/nude art, or beach/bathing/spa scenes as a
      visual prompt, even if the topic is about a place famous for them -
      pick a different concrete visual instead (architecture, food,
      landscape, everyday street life, etc.)

HARD REQUIREMENT ON LENGTH: total narration across all beats combined must be
70-95 words. Count them before answering. Fewer than 70 words produces a
video that is too short to be usable, so err toward the upper end rather
than the lower. First line must be a strong hook.
Content must be strictly brand-safe and family-friendly: no adult content,
violence, illegal activity, hate speech, drugs, gambling, or anything that
could be flagged as unsafe for advertisers or YouTube's community guidelines.
Output ONLY the JSON, no markdown fences. Do not omit "line_en" from any beat."""

    keys = _groq_api_keys()
    if not keys:
        raise RuntimeError("No Groq API key configured (GROQ_API_KEY missing).")

    resp = None
    for i, (label, key) in enumerate(keys):
        resp = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={
                # llama-3.3-70b-versatile was deprecated by Groq, shutdown
                # 2026-08-16 - openai/gpt-oss-120b is their recommended
                # replacement (the other option, qwen/qwen3.6-27b, is
                # smaller/less capable). It's a reasoning model, but unlike
                # some others its chain-of-thought goes into a separate
                # message.reasoning field by default - message.content
                # stays just the final answer, so the existing
                # content-parsing/JSON-fence-stripping below didn't need to
                # change. reasoning_effort=low + include_reasoning=False
                # skip the (unneeded, for a short structured script) deep
                # reasoning pass entirely - faster and cheaper per call.
                "model": "openai/gpt-oss-120b",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.9,
                "response_format": {"type": "json_object"},
                "reasoning_effort": "low",
                "include_reasoning": False,
            },
            timeout=60,
        )
        if resp.ok:
            break
        # Print the response body before raising - a bare "400 Client Error"
        # with no context (as happened once) is useless for diagnosing what
        # Groq actually objected to (bad request shape, rate limit, content
        # policy, etc.).
        print(f"Groq API error {resp.status_code} ({label} key): {resp.text[:500]}", flush=True)
        is_last_key = (i == len(keys) - 1)
        if _is_daily_quota_error(resp) and not is_last_key:
            print(f"Groq {label} key hit its daily token quota - "
                  f"switching to the next configured key.", flush=True)
            continue
        # Either this was the last available key, or it's a non-quota error
        # (bad request, transient per-minute limit, auth failure) that
        # switching accounts wouldn't fix anyway - raise normally so the
        # existing retry/backoff paths (script_try loop, workflow-level
        # 60s-on-failure backoff) handle it same as before.
        resp.raise_for_status()

    data = resp.json()
    text = data["choices"][0]["message"]["content"].strip()
    text = re.sub(r"^```json|```$", "", text, flags=re.MULTILINE).strip()
    script = json.loads(text)

    # Track token usage so it's visible per-call in the log and summable
    # across a whole run (a run can call Groq more than once - one per
    # script_try retry) - this is what makes "how many tokens did today's
    # runs actually use" answerable instead of just guessing against the
    # 100k/day cap.
    usage = data.get("usage", {})
    record_groq_usage(usage)
    print(f"Groq usage this call: {usage.get('total_tokens', '?')} tokens "
          f"(prompt={usage.get('prompt_tokens', '?')}, "
          f"completion={usage.get('completion_tokens', '?')}) - "
          f"run total so far: {_GROQ_RUN_USAGE['total_tokens']} tokens "
          f"across {_GROQ_RUN_USAGE['calls']} Groq call(s)", flush=True)

    # Defensive normalization: the schema above asks for both "line" and
    # "line_en" on every beat, but a model can still drift and omit one -
    # that previously surfaced as a bare, confusing "KeyError: 'line'" deep
    # in run_once() and killed the whole run. Backfill from whichever field
    # is present; only raise (a clear, specific error the retry loop can
    # act on) if a beat has neither.
    for i, beat in enumerate(script.get("beats", [])):
        has_line = bool(beat.get("line"))
        has_line_en = bool(beat.get("line_en"))
        if not has_line and not has_line_en:
            raise ValueError(f"Groq returned beat {i} with neither 'line' nor 'line_en': {beat!r}")
        if not has_line:
            beat["line"] = beat["line_en"]
        if not has_line_en:
            beat["line_en"] = beat["line"]

    return script


async def synthesize_voice(text, out_path, language="en", rate="+18%", voice_override=None):
    # Slightly faster than default speaking rate - matches the punchier,
    # quick-cut editing style rather than a slow, deliberate voiceover.
    # voice_override lets a caller pass a specific Edge TTS voice (used for
    # native-language narration, see COUNTRY_LANGUAGES) instead of looking
    # one up by language code.
    voice = voice_override or VOICES.get(language, VOICES["en"])
    communicate = edge_tts.Communicate(text, voice, rate=rate)
    await communicate.save(str(out_path))


# Pollinations' free tier enforces a hard "max 1 queued request per IP"
# limit - any concurrency trips it immediately. Now that shot-fetching runs
# multiple shots in a thread pool (see run_once()), this lock is what keeps
# that promise even though several threads may all decide they need an AI
# image at the same time - only one Pollinations request is ever in flight
# at once, the rest simply queue on the lock. Stock-footage shots (Pexels)
# and ffmpeg encoding never touch this lock, so they're unaffected and get
# the full benefit of running in parallel.
POLLINATIONS_LOCK = threading.Lock()


# Explicit nudity classes only - deliberately NOT blocking on
# FEET_EXPOSED/BELLY_EXPOSED/ARMPITS_EXPOSED/MALE_BREAST_EXPOSED, which
# would false-positive on completely normal travel/food/beach content
# (bare feet, a visible midriff, a shirtless man at the beach are not
# nudity under YouTube's actual policy). These five are the ones that
# matter for "sex and nudity policy" strikes.
NUDITY_DETECTION_CLASSES = {
    "FEMALE_GENITALIA_EXPOSED",
    "MALE_GENITALIA_EXPOSED",
    "FEMALE_BREAST_EXPOSED",
    "BUTTOCKS_EXPOSED",
    "ANUS_EXPOSED",
}
# Lowered from 0.5 to 0.3 after the third strike (an innocent food-topic
# prompt still produced nudity) - 0.5 was letting borderline-confidence
# detections through unflagged. Erring toward more false positives (a safe
# image gets discarded and retried) is the right tradeoff vs. another
# Community Guidelines strike.
NUDITY_DETECTION_THRESHOLD = 0.3

# Optional path to NudeNet's larger, more accurate "640m" detection model
# (YOLOv8m-based, 640x640 input - notably better recall than the ~7MB
# "320n" model bundled by default with the pip package). Not shipped in the
# package itself, so the workflow downloads it once and caches it across
# runs, then points here via env var. Falls back to the bundled default
# model if unset or the file isn't actually present, so this never becomes
# a hard dependency / can't turn into a new single point of failure.
_NUDENET_MODEL_PATH = os.environ.get("NUDENET_MODEL_PATH", "").strip()

_nude_detector = None
_nude_detector_lock = threading.Lock()


def _get_nude_detector():
    """Lazy-loaded singleton - the model only needs to load once per
    process, and onnxruntime's InferenceSession.run() is safe to call
    concurrently from multiple threads, so only the one-time construction
    needs the lock, not each detection call."""
    global _nude_detector
    if _nude_detector is None:
        with _nude_detector_lock:
            if _nude_detector is None:
                from nudenet import NudeDetector
                if _NUDENET_MODEL_PATH and os.path.isfile(_NUDENET_MODEL_PATH):
                    # A corrupt/truncated download (interrupted transfer, or
                    # a bad file that got cached from an earlier failed
                    # run) would otherwise raise here, and since this whole
                    # call is inside _is_flagged_nsfw()'s fail-closed
                    # try/except, that used to mean EVERY single image for
                    # the rest of the run got silently flagged - a 320n
                    # ONNX load bug looks identical to "this run's images
                    # were all nudity" unless you're staring at the log.
                    # Falling back to the known-good bundled model here
                    # keeps a bad custom model from taking the whole
                    # detector down instead of just losing the accuracy
                    # upgrade for this run.
                    try:
                        print(f"    Loading NudeNet 640m model from {_NUDENET_MODEL_PATH}", flush=True)
                        _nude_detector = NudeDetector(model_path=_NUDENET_MODEL_PATH, inference_resolution=640)
                    except Exception as e:
                        print(f"    Failed to load 640m model ({type(e).__name__}: {e}) - "
                              f"falling back to the bundled default (320n) model instead.", flush=True)
                        _nude_detector = NudeDetector()
                else:
                    print("    NUDENET_MODEL_PATH not set/found - using bundled default (320n) model.", flush=True)
                    _nude_detector = NudeDetector()
    return _nude_detector


# Running tally for this test week (2026-08-11 through the strike
# cool-down) - lets us log, per video, how many candidate images the
# NudeNet check actually looked at vs how many it flagged, so we can
# verify the detection logic is doing real work rather than just trusting
# it silently. flag_details records WHY each one was flagged - either a
# real detection (class + confidence, matched right back to the prompt
# that produced it) or a technical error (fails closed, but distinguishing
# the two matters: a wall of "error" entries means a bug, not nudity - see
# the 640m-model-load fallback above, added after exactly that happened).
# Reset per-process (each GitHub Actions run is a fresh process), written
# out via record_nsfw_test_entry() at the end of the run.
_NSFW_RUN_STATS = {"checked": 0, "flagged": 0, "flag_details": []}


FLAGGED_PROMPTS_FILE = ROOT / "flagged_prompts.json"
_flagged_prompts_lock = threading.Lock()


def _load_flagged_prompts():
    try:
        return json.loads(FLAGGED_PROMPTS_FILE.read_text()) if FLAGGED_PROMPTS_FILE.exists() else {}
    except Exception:
        return {}


def record_flagged_prompt(prompt, cls, confidence, topic, category, country):
    """Learned blocklist: every time NudeNet gets a REAL detection (not a
    technical error) on an AI-generated image, remember the exact prompt
    that produced it. _get_safe_ai_image_prompt() checks new prompts
    against this before ever sending them to the generator, so a prompt
    that has produced nudity once doesn't get to waste another generation
    cycle (image download + retries + NudeNet check, all just to get
    discarded again) reproducing it. Keyed by the normalized prompt text so
    repeats (same country+template combo, common given the rotation) merge
    into one growing-confidence entry instead of duplicating."""
    key = prompt.strip().lower()
    with _flagged_prompts_lock:
        try:
            data = _load_flagged_prompts()
            entry = data.get(key, {"prompt": prompt, "times_flagged": 0, "classes": [], "examples": []})
            entry["times_flagged"] += 1
            if cls not in entry["classes"]:
                entry["classes"].append(cls)
            entry["examples"] = (entry["examples"] + [{
                "topic": topic, "category": category, "country": country,
                "confidence": round(confidence, 3), "flagged_at": time.time(),
            }])[-5:]  # keep it bounded - most recent 5 examples is plenty
            data[key] = entry
            FLAGGED_PROMPTS_FILE.write_text(json.dumps(data, indent=2))
        except Exception as e:
            print(f"  (non-fatal) failed to update flagged_prompts.json: {e}", flush=True)


def _is_flagged_nsfw(path, prompt=None):
    """Real, offline, pixel-level nudity check on the actual generated
    image - not the prompt used to request it. This exists because prompt-
    based prevention (safe=true, a "no nudity" suffix, even a hard keyword
    block on risky-sounding prompts) proved insufficient across repeated
    YouTube strikes: an AI image generator can produce nudity completely
    unprompted, for an entirely innocent-sounding request (a "Must-Try Thai
    Dish" food video got flagged - nothing about that topic should trigger
    any keyword filter, yet the generator still produced a nude figure).
    Only inspecting the actual pixels closes that gap.

    Uses NudeNet (pip install nudenet) - runs fully offline via ONNX
    Runtime, no network call at inference time, so unlike the previous
    attempt at this (Pollinations' vision endpoint, which turned out to
    require payment - HTTP 402 on every call) this cannot break due to a
    service going paid, changing its rate limits, or going down.

    FAILS CLOSED like every safety-relevant check in this file: if
    detection itself errors (corrupt file, model issue, anything), this
    returns True (treat as flagged) rather than silently passing. That
    failure mode is recorded distinctly from a real detection (see
    flag_details below) precisely so a technical failure doesn't get
    mistaken for - or learned as - an actual nudity pattern."""
    _NSFW_RUN_STATS["checked"] += 1
    try:
        detector = _get_nude_detector()
        detections = detector.detect(str(path))
        for d in detections:
            if d.get("class") in NUDITY_DETECTION_CLASSES and d.get("score", 0) >= NUDITY_DETECTION_THRESHOLD:
                print(f"    NSFW check flagged this image: {d['class']} "
                      f"(confidence {d['score']:.2f}) - prompt: {prompt!r}", flush=True)
                _NSFW_RUN_STATS["flagged"] += 1
                _NSFW_RUN_STATS["flag_details"].append({
                    "reason": "detected", "class": d["class"],
                    "confidence": round(d["score"], 3), "prompt": prompt,
                })
                return True
        return False
    except Exception as e:
        print(f"    NSFW check errored ({type(e).__name__}: {e}) - treating as flagged out of caution "
              f"(this is a technical failure, not necessarily real nudity - check the reason in "
              f"nsfw_test_log.json).", flush=True)
        _NSFW_RUN_STATS["flagged"] += 1
        _NSFW_RUN_STATS["flag_details"].append({
            "reason": "error", "error": f"{type(e).__name__}: {e}", "prompt": prompt,
        })
        return True


def _is_decodable_image(path):
    """Validates the file is actually a real, decodable image - not just
    that the HTTP request returned 200. Pollinations can return a 200 with
    a corrupt/truncated/error-page body (more likely now that every shot
    queues through one lock back-to-back); without this check that bad file
    gets treated as a successful download and later crashes ffmpeg's Ken
    Burns step with an opaque, hard-to-diagnose error (this is exactly what
    happened - reproduced locally: a non-image body fed to `ffmpeg -loop 1`
    fails with "No JPEG data found in image" / nonzero exit). Uses ffprobe
    (already a hard dependency) rather than adding a new one like Pillow.

    Important: ffprobe exits 0 even on a corrupt file - it just reports
    "0,0" for width/height while logging the real error to stderr. So this
    can't just check the exit code / non-empty stdout; it has to parse the
    dimensions and confirm they're actually positive."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            return False
        parts = result.stdout.strip().split(",")
        if len(parts) != 2:
            return False
        width, height = int(parts[0]), int(parts[1])
        return width > 0 and height > 0
    except Exception:
        return False


# Appended to every AI-image prompt sent to Pollinations, on top of
# safe=true - handling safety "on the API end" rather than a separate
# post-generation check call (a second check via text.pollinations.ai's
# vision model turned out to require payment - HTTP 402 on every single
# call - which isn't a usage cap or rate limit, that model just isn't on
# the free tier at all. Fail-closed design meant that alone broke every
# image in every video, so it's removed - safe=true plus this prompt
# directive are the actual defense now, both part of the one free request).
NSFW_SAFETY_SUFFIX = (
    ", safe for work, family-friendly, fully clothed, no nudity, "
    "no sexual content, wholesome, appropriate for all audiences"
)


def download_image(prompt, out_path, width=1440, height=2560, max_retries=4):
    # Requesting above final 1080x1920 output resolution gives the zoompan
    # (Ken Burns) effect in build_video room to zoom in without softening.
    # Two safety measures, both part of this one free request instead of a
    # separate paid check call: safe=true is Pollinations' own NSFW filter
    # (rejects/errors on flagged content before it's even returned), and
    # NSFW_SAFETY_SUFFIX steers the generation itself away from unsafe
    # output in the first place.
    safe_prompt = prompt + NSFW_SAFETY_SUFFIX
    url = f"https://image.pollinations.ai/prompt/{requests.utils.quote(safe_prompt)}"

    with POLLINATIONS_LOCK:
        last_detail = None
        for attempt in range(max_retries):
            try:
                # A fresh random seed every attempt, including the first -
                # without one, Pollinations serves the identical cached
                # image for identical prompt+params (confirmed via their
                # own docs: "same seed + parameters return the same cached
                # result"). This was a real, confirmed bug: every "retry"
                # after an NSFW flag was hitting that exact same cached
                # image again, so all 4 retries were wasted (guaranteed to
                # fail identically) instead of actually attempting a fresh
                # generation - and worse, since shot prompts are often
                # short reused phrases (e.g. "Porto city streets"), any
                # OTHER video that happened to generate the same prompt
                # text, at any point, would also get served that exact
                # same flagged image forever. Randomizing the seed makes
                # every single request (first try and retries alike) a
                # genuinely independent generation.
                params = {"width": width, "height": height, "nologo": "true",
                          "enhance": "true", "safe": "true",
                          "seed": random.randint(1, 2_000_000_000)}
                r = requests.get(url, params=params, timeout=90)
                if r.status_code == 429:
                    last_detail = f"HTTP 429: {r.text[:200]}"
                    wait = (2 ** attempt) * 3 + random.uniform(0, 2)
                    time.sleep(wait)
                    continue
                if r.status_code >= 400:
                    # This is also what safe=true triggers when Pollinations'
                    # own filter flags the prompt/output - a rejection here
                    # is exactly what we want it to do, not a bug to route
                    # around.
                    last_detail = f"HTTP {r.status_code}: {r.text[:200]}"
                    time.sleep((2 ** attempt) + random.uniform(0, 1))
                    continue
                r.raise_for_status()
                out_path.write_bytes(r.content)
                if not _is_decodable_image(out_path):
                    last_detail = (f"HTTP 200 but {len(r.content)} bytes weren't a "
                                    f"decodable image (corrupt/error body)")
                    time.sleep((2 ** attempt) + random.uniform(0, 1))
                    continue
                if _is_flagged_nsfw(out_path, prompt=prompt):
                    last_detail = "image failed the local NudeNet nudity check"
                    time.sleep((2 ** attempt) + random.uniform(0, 1))
                    continue
                time.sleep(0.3)  # brief courtesy pause before releasing the
                                  # lock, so a queued thread's request doesn't
                                  # fire the instant this one lands
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

# Repeated YouTube nudity strikes traced back to this specific gap: classical
# Greek/Roman statues and sculptures are famously, overwhelmingly nude - when
# an AI image generator is asked for "ancient Greek sculpture" or "classical
# statue", it's very likely to produce actual nude imagery, because that's
# genuinely what the real subject looks like. Pollinations' safe=true filter
# is explicitly tuned to let "artistic" content through (their own docs),
# which is exactly the bucket classical statuary falls into - safe=true and
# a prompt-level "no nudity" suffix both proved unreliable against this
# specific category (that's what caused the repeat strikes). This is
# travel/food content, so statues/sculptures/bathing/beach scenes come up
# often and legitimately - rather than block the whole shot, any prompt
# matching one of these concepts gets swapped for a generic, unrelated-but-
# safe fallback prompt before it's ever sent to the AI generator. Real stock
# footage (Pexels) is NOT affected - that's curated commercial photography,
# not a diffusion model, and was never the source of either incident.
AI_IMAGE_RISK_KEYWORDS = [
    "statue", "statues", "sculpture", "sculptures", "sculptural",
    "carving", "carvings", "figurine", "bronze figure", "marble figure",
    "classical art", "classical figure", "nude art", "life drawing",
    "beach", "bikini", "swimwear", "swimsuit", "topless", "shirtless",
    "bare-chested", "bare chested", "spa", "bath house", "bathhouse",
    "onsen", "hot spring", "hammam", "sauna", "skinny dip",
    "locker room", "changing room", "massage",
]


def _get_safe_ai_image_prompt(prompt):
    """Returns prompt unchanged unless it matches AI_IMAGE_RISK_KEYWORDS or
    is a known-bad prompt from flagged_prompts.json (see
    record_flagged_prompt), in which case it returns a generic,
    topic-unrelated fallback instead - trading a bit of visual relevance on
    that one shot for guaranteeing the AI generator is never even asked for
    a nudity-adjacent concept in the first place, rather than trusting a
    downstream filter to catch it after generation. Only used for the
    AI-image path; Pexels stock search still gets the real, original
    prompt."""
    lower = prompt.lower()
    for kw in AI_IMAGE_RISK_KEYWORDS:
        if kw in lower:
            safe = random.choice(GENERIC_FALLBACK_PROMPTS)
            print(f"    AI image prompt {prompt!r} matched risk keyword "
                  f"{kw!r} - substituting safe generic prompt {safe!r} "
                  f"instead of sending the original to the generator.", flush=True)
            return safe

    flagged = _load_flagged_prompts()
    hit = flagged.get(lower.strip())
    if hit:
        safe = random.choice(GENERIC_FALLBACK_PROMPTS)
        print(f"    AI image prompt {prompt!r} previously produced real nudity "
              f"({hit['times_flagged']}x, classes: {hit['classes']}) - substituting "
              f"safe generic prompt {safe!r} instead of trying it again.", flush=True)
        return safe

    return prompt


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


def _degraded_clips(n_splits, sub_dur, work_dir, index, get_fallback_clip):
    """Last-resort fallback shared by every failure path in
    prepare_shot_clips(): reuse the most recently successful clip from this
    video if one exists, otherwise a plain solid-color background. Never
    raises - this is the floor everything else falls back to, so one flaky
    shot never crashes the whole run/upload."""
    fallback_clip = get_fallback_clip() if get_fallback_clip else None
    clips = []
    for j in range(n_splits):
        out_path = work_dir / f"clip_{index}_{j}.mp4"
        if fallback_clip is not None:
            duplicate_clip_to_duration(fallback_clip, sub_dur, out_path)
        else:
            make_solid_color_clip(sub_dur, out_path)
        clips.append(out_path)
    source = "reused" if fallback_clip is not None else "placeholder"
    return clips, source


def prepare_shot_clips(prompt, shot_dur, work_dir, index, get_fallback_clip=None):
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

    get_fallback_clip is a zero-arg callable (not a static path) returning
    whatever the most recently completed successful clip is, read fresh at
    the moment of use - shots now run concurrently in a thread pool, so
    there's no single well-defined "previous shot" anymore, just "whatever
    finished most recently across all in-flight shots."
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
    ai_prompt = _get_safe_ai_image_prompt(prompt)
    try:
        download_image(ai_prompt, img_path)
    except Exception as e:
        print(f"    AI image failed for {prompt!r}: {e}", flush=True)
        fallback_prompt = random.choice(GENERIC_FALLBACK_PROMPTS)
        print(f"    Retrying with generic fallback prompt: {fallback_prompt!r}", flush=True)
        try:
            download_image(fallback_prompt, img_path)
        except Exception as e2:
            print(f"    Generic fallback also failed: {e2}", flush=True)
            clips, source = _degraded_clips(n_splits, sub_dur, work_dir, index, get_fallback_clip)
            print(f"    Shot {index} degraded to fallback source: {source}", flush=True)
            return clips, source

    # download_image() now validates it got a real, decodable image before
    # returning (see _is_decodable_image) - but ffmpeg's zoompan/Ken Burns
    # step is still its own possible failure point (e.g. a genuinely valid
    # but unusual image tripping up the filter), and previously had NO
    # fallback at all: one bad frame here used to crash the entire run
    # (reproduced and confirmed - "No JPEG data found in image" / nonzero
    # ffmpeg exit, with MAX_ATTEMPTS=1 meaning no second chance). Now it
    # degrades the same way every other failure in this function does.
    try:
        clips = []
        for j in range(n_splits):
            out_path = work_dir / f"clip_{index}_{j}.mp4"
            make_ken_burns_clip(img_path, sub_dur, out_path, zoom_in=(j % 2 == 0))
            clips.append(out_path)
        return clips, "ai_image"
    except Exception as e:
        print(f"    Ken Burns clip generation failed for shot {index}: {e}", flush=True)
        clips, source = _degraded_clips(n_splits, sub_dur, work_dir, index, get_fallback_clip)
        print(f"    Shot {index} degraded to fallback source: {source}", flush=True)
        return clips, source


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


def write_srt(beats, durations, out_path, language="en", caption_key="line"):
    """Writes an .ass subtitle file (despite the name, kept for compatibility
    with callers) with the caption style baked into the file header - no
    CLI-side force_style escaping needed.

    caption_key selects which field of each beat to render as the caption.
    For native-language narration this is "line_en" (English captions over
    native audio), which also keeps the font simple: English captions never
    need non-Latin glyphs no matter what language the voiceover is in.

    Fontname must actually have glyphs for the script being rendered -
    Devanagari (Hindi) needs a dedicated font or it renders as blank boxes.
    The CI workflow installs "fonts-noto-core" via apt, which bundles both
    "Noto Sans" and "Noto Sans Devanagari"."""

    def fmt(t):
        h = int(t // 3600); m = int((t % 3600) // 60); s = t % 60
        cs = int((t - int(t)) * 100)
        return f"{h:d}:{m:02d}:{int(s):02d}.{cs:02d}"

    # Captions are English whenever caption_key is "line_en", so the Latin
    # font is correct then regardless of the narration language.
    if caption_key == "line_en" or language == "en":
        font = "Noto Sans"
    elif language == "hi":
        font = "Noto Sans Devanagari"
    else:
        font = "Noto Sans"

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
        raw = beat.get(caption_key) or beat.get("line", "")
        text = raw.replace("\n", " ").replace(",", "\\,")
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
MIN_SAMPLES_PER_COUNTRY = 3   # same idea, for compute_country_weights()


def load_performance_log():
    if PERFORMANCE_FILE.exists():
        return json.loads(PERFORMANCE_FILE.read_text())
    return {"videos": []}


def record_performance_entry(video_id, category, language, topic, country=None):
    """Called right after a successful upload so we can look up its real
    view/like counts later and use them to bias future topic selection.
    country is a structured field (not just parsed back out of the topic
    text) so compute_country_weights() can group by it directly - it's
    None for tech/ai topics, which aren't country-rotated. Older log
    entries from before this field existed simply have no "country" key,
    which compute_country_weights() already treats as "skip".

    Also records this run's total Groq token usage (summed across every
    Groq call the run made, including script-length regeneration retries)
    so performance_log.json becomes a durable, queryable record of actual
    usage - "how many tokens did today's videos use" is just summing
    groq_tokens across today's entries, instead of scrolling Actions logs."""
    if not video_id:
        return
    data = load_performance_log()
    data["videos"].append({
        "video_id": video_id,
        "category": category,
        "language": language,
        "topic": topic,
        "country": country,
        "groq_tokens": get_groq_run_usage()["total_tokens"],
        "groq_calls": get_groq_run_usage()["calls"],
        "posted_at": time.time(),
    })
    PERFORMANCE_FILE.write_text(json.dumps(data, indent=2))


ERROR_LOG_FILE = ROOT / "error_log.json"


def _github_run_url():
    """None outside GitHub Actions (e.g. running locally) - the run/job env
    vars are only set there."""
    run_id = os.environ.get("GITHUB_RUN_ID")
    if not run_id:
        return None
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    return f"{server}/{repo}/actions/runs/{run_id}"


def record_error_entry(topic, category, country, attempt, max_attempts, error):
    """Persists every failed attempt (not just the final one that kills the
    run) to error_log.json, including the GitHub Actions run number/link so
    a failure seen here can be traced straight back to its job's full logs -
    the console output itself isn't kept anywhere once a run's logs age out.
    Never raises - a logging failure should never be what takes down a run."""
    try:
        data = json.loads(ERROR_LOG_FILE.read_text()) if ERROR_LOG_FILE.exists() else {"errors": []}
        data["errors"].append({
            "run_number": os.environ.get("GITHUB_RUN_NUMBER"),
            "run_id": os.environ.get("GITHUB_RUN_ID"),
            "run_url": _github_run_url(),
            "topic": topic,
            "category": category,
            "country": country,
            "attempt": attempt,
            "max_attempts": max_attempts,
            "error_type": type(error).__name__,
            "error": str(error),
            "traceback": traceback.format_exc()[-4000:],
            "occurred_at": time.time(),
        })
        # Keep this from growing without bound - most recent 200 failures is
        # plenty to spot patterns without the file becoming unwieldy.
        data["errors"] = data["errors"][-200:]
        ERROR_LOG_FILE.write_text(json.dumps(data, indent=2))
    except Exception as log_err:
        print(f"  (non-fatal) failed to write error_log.json: {log_err}", flush=True)


NSFW_TEST_LOG_FILE = ROOT / "nsfw_test_log.json"


def record_nsfw_test_entry(topic, category, country=None):
    """Appends one entry per run to nsfw_test_log.json - separate from
    performance_log.json since this tracks detection-logic behavior, not
    video performance, and needs to keep working during the SKIP_UPLOAD
    strike cool-down when there's no video_id to key off of. Purely a
    verification/observability log; nothing in the pipeline reads this
    back, so a write failure here should never break a run.

    Also feeds every REAL detection (not a technical error - see
    _is_flagged_nsfw) into flagged_prompts.json via record_flagged_prompt(),
    so a prompt that has produced actual nudity once is remembered and
    avoided next time (_get_safe_ai_image_prompt() checks it) instead of
    wasting another full generation+check cycle reproducing the same
    result."""
    try:
        data = json.loads(NSFW_TEST_LOG_FILE.read_text()) if NSFW_TEST_LOG_FILE.exists() else {"runs": []}
        # Bounded per-run detail log - a pathological run (e.g. a bug
        # flagging every single image) shouldn't blow this file up
        # unboundedly; the checked/flagged counts above still capture the
        # full picture regardless.
        details = _NSFW_RUN_STATS["flag_details"][:25]
        data["runs"].append({
            "run_number": os.environ.get("GITHUB_RUN_NUMBER"),
            "run_url": _github_run_url(),
            "topic": topic,
            "category": category,
            "country": country,
            "images_checked": _NSFW_RUN_STATS["checked"],
            "images_flagged": _NSFW_RUN_STATS["flagged"],
            "flag_details": details,
            "ran_at": time.time(),
        })
        data["runs"] = data["runs"][-500:]
        NSFW_TEST_LOG_FILE.write_text(json.dumps(data, indent=2))
    except Exception as e:
        print(f"  (non-fatal) failed to write nsfw_test_log.json: {e}", flush=True)

    for d in _NSFW_RUN_STATS["flag_details"]:
        if d.get("reason") == "detected" and d.get("prompt"):
            record_flagged_prompt(d["prompt"], d["class"], d["confidence"], topic, category, country)


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


def compute_country_weights():
    """Same idea as compute_category_weights(), but grouped by country
    instead of category - only called by pick_country() once the initial
    20-country round-robin cycle has fully completed at least once, so
    every country already has a fair first shot before weighting kicks in.
    Countries with too few mature samples (including any older log entries
    from before the "country" field existed, which just won't match any
    country here) get a neutral weight so they stay in the mix rather than
    getting starved by early noise. Fails open (equal weights) on any
    error - this is an optimization, never something that should block a
    run."""
    equal_weights = {c: 1.0 for c in COUNTRIES}

    try:
        data = load_performance_log()
        now = time.time()
        mature = [
            v for v in data["videos"]
            if v.get("country") in COUNTRIES
            and now - v.get("posted_at", 0) >= MIN_MATURITY_HOURS * 3600
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

        scores_by_country = {c: [] for c in COUNTRIES}
        for v in mature:
            stats = stats_by_id.get(v["video_id"])
            if not stats:
                continue
            views = int(stats.get("viewCount", 0))
            likes = int(stats.get("likeCount", 0))
            score = views + likes * 10
            scores_by_country[v["country"]].append(score)

        all_scores = [s for lst in scores_by_country.values() for s in lst]
        overall_avg = (sum(all_scores) / len(all_scores)) if all_scores else 1.0

        weights = {}
        for c in COUNTRIES:
            lst = scores_by_country[c]
            if len(lst) >= MIN_SAMPLES_PER_COUNTRY:
                weights[c] = max(sum(lst) / len(lst), 1.0)
            else:
                weights[c] = overall_avg  # not enough data yet - keep exploring
        return weights
    except Exception as e:
        print(f"compute_country_weights failed, falling back to equal weights: {e}", flush=True)
        return equal_weights


class UnsafeTopicError(Exception):
    pass


MIN_VIDEO_SECONDS = 14.0   # anything under this reads as a broken/stub video
                           # (a 7s Short slipped out once when the model
                           # badly undershot the word target) - regenerate
                           # the script rather than publishing it
MAX_SCRIPT_TRIES = 3


def run_once(topic, niche, language="en", category=None, native=None, country=None):
    """Runs the full pipeline for a single topic: script -> safety check ->
    voice -> images -> video -> upload. Raises on any failure; caller decides
    whether to retry with a different topic.

    native: optional {"name": ..., "voice": ...} from COUNTRY_LANGUAGES. When
    set, narration is synthesized in that language and captions render the
    English translation. If native synthesis fails for any reason (bad/retired
    voice name, TTS hiccup), this falls back to the English voice reading the
    English lines - never a failed run just because a native voice misbehaved.
    """
    # Reset so a retried attempt (see main()'s retry loop) reports this
    # attempt's own NudeNet stats, not an accumulation across every attempt
    # tried so far this process.
    _NSFW_RUN_STATS["checked"] = 0
    _NSFW_RUN_STATS["flagged"] = 0
    _NSFW_RUN_STATS["flag_details"] = []

    work = Path(tempfile.mkdtemp())
    print("Topic:", topic, flush=True)
    print("Language:", language, flush=True)
    print("Category:", category, flush=True)
    if native:
        print(f"Narration language: {native['name']} ({native['voice']}), English captions", flush=True)

    voice_path = work / "voice.mp3"
    script = None
    beats = None
    total_dur = None
    caption_key = "line_en" if native else "line"
    narrating_native = bool(native)
    MIN_VOICE_FILE_BYTES = 1024  # edge_tts can "succeed" (no exception) but

    # write a near-empty/corrupt file for a bad voice name - catch that
    # explicitly instead of letting it surface later as a cryptic ffprobe
    # crash that skips the English fallback entirely (that's exactly what
    # happened with the ja-JP/ar-EG/th-TH voices: no exception at synthesis,
    # just a bad file, and ffprobe blew up outside the old try/except).

    # Regenerate if the model produces a script too short to be a usable
    # Short - measured on the actual synthesized audio, not a word-count
    # guess, since speaking rate varies a lot by language. Also regenerate
    # (rather than crash the whole run) if Groq returns a malformed beat.
    for script_try in range(1, MAX_SCRIPT_TRIES + 1):
        print(f"Generating script (Groq), try {script_try}/{MAX_SCRIPT_TRIES}...", flush=True)
        try:
            script = generate_script(
                topic, niche, language=language,
                native_language_name=native["name"] if native else None,
            )
            print(f"Script ready: {len(script['beats'])} beats, title: {script['title']!r}", flush=True)

            # Safety check runs on the English text (title/description/
            # line_en) - BLOCKED_KEYWORDS are English, so checking
            # native-script narration would silently pass everything.
            full_check_text = script["title"] + " " + script["description"] + " " + \
                " ".join((b.get("line_en") or b.get("line", "")) for b in script["beats"])
            if not is_safe_topic(full_check_text):
                match = find_blocked_match(full_check_text)
                print(f"Safety check matched: {match[0]!r} in context: ...{match[1]}...", flush=True)
                raise UnsafeTopicError(f"Generated script for topic {topic!r} failed the safety check")

            beats = script["beats"]
            narration_text = " ".join((b.get("line") or b.get("line_en", "")) for b in beats)
            english_text = " ".join((b.get("line_en") or b.get("line", "")) for b in beats)
        except (ValueError, KeyError) as e:
            if script_try == MAX_SCRIPT_TRIES:
                raise
            print(f"Malformed script from Groq ({e!r}) - regenerating...", flush=True)
            continue

        print("Synthesizing voiceover (Edge TTS)...", flush=True)
        narrating_native = bool(native)
        caption_key = "line_en" if native else "line"
        if native:
            try:
                asyncio.run(synthesize_voice(
                    narration_text, voice_path, voice_override=native["voice"]
                ))
                actual_size = voice_path.stat().st_size if voice_path.exists() else 0
                if actual_size < MIN_VOICE_FILE_BYTES:
                    raise RuntimeError(
                        f"native voice produced a {actual_size}-byte file "
                        f"(no exception raised, but clearly not real audio)"
                    )
                total_dur = get_audio_duration(voice_path)
            except Exception as e:
                print(f"Native voice {native['voice']!r} failed ({e}) - "
                      f"falling back to English narration.", flush=True)
                narrating_native = False
                caption_key = "line"
                asyncio.run(synthesize_voice(english_text, voice_path, language="en"))
                total_dur = get_audio_duration(voice_path)
        else:
            asyncio.run(synthesize_voice(narration_text, voice_path, language=language))
            total_dur = get_audio_duration(voice_path)

        print(f"Voiceover ready: {total_dur:.1f}s", flush=True)

        if total_dur >= MIN_VIDEO_SECONDS or script_try == MAX_SCRIPT_TRIES:
            if total_dur < MIN_VIDEO_SECONDS:
                print(f"WARNING: still only {total_dur:.1f}s after "
                      f"{MAX_SCRIPT_TRIES} tries - publishing anyway.", flush=True)
            break
        print(f"Too short ({total_dur:.1f}s < {MIN_VIDEO_SECONDS}s) - "
              f"regenerating a longer script...", flush=True)

    # Proportional pacing: a beat with more words gets more screen time,
    # instead of every beat getting an equal slice regardless of length.
    # Weight by whichever text was actually spoken.
    spoken_key = "line" if narrating_native or not native else "line_en"
    word_counts = [max(len((b.get(spoken_key) or b.get("line", "")).split()), 1) for b in beats]
    total_words = sum(word_counts)
    beat_durations = [total_dur * (wc / total_words) for wc in word_counts]

    srt_path = work / "captions.ass"
    write_srt(beats, beat_durations, srt_path, language=language, caption_key=caption_key)

    # Fast quick-cut editing: each beat gets 2-3 distinct visual shots
    # (downloaded images) instead of one static image for its whole duration.
    shots = []  # list of (prompt, shot_duration)
    for beat, beat_dur in zip(beats, beat_durations):
        default_prompt = beat.get("line_en") or beat.get("line", "")
        prompts = beat.get("visual_prompts") or [beat.get("visual_prompt", default_prompt)]
        prompts = prompts[:3] or [default_prompt]
        shot_dur = beat_dur / len(prompts)
        for p in prompts:
            shots.append((p, shot_dur))

    # Runs shots concurrently instead of one at a time - this was the single
    # biggest chunk of wall-clock time in a run (Pexels searches, stock
    # downloads, and ffmpeg trims for ~15-20 shots, each waited on in full
    # before the next even started). Pexels' free tier is generous enough to
    # handle several requests at once; ffmpeg trims are separate subprocesses
    # so they parallelize fine too. The one thing that genuinely can't run
    # concurrently is Pollinations (AI-image fallback) - its free tier caps
    # at 1 in-flight request per IP - so that part alone is still serialized,
    # via POLLINATIONS_LOCK inside download_image(). Net effect: shots that
    # hit stock footage (the common case when PEXELS_API_KEY is set) get the
    # full parallel speedup; shots that fall back to AI images queue safely
    # behind each other without wasting the pool's other worker slots.
    SHOT_FETCH_WORKERS = 5
    print(f"Fetching {len(shots)} visuals (stock footage, falling back to AI images; "
          f"up to {SHOT_FETCH_WORKERS} shots in parallel)...", flush=True)

    fallback_lock = threading.Lock()
    fallback_state = {"clip": None}

    def get_fallback_clip():
        with fallback_lock:
            return fallback_state["clip"]

    def set_fallback_clip(clip):
        with fallback_lock:
            fallback_state["clip"] = clip

    def fetch_one(i, prompt, shot_dur):
        clips, source = prepare_shot_clips(
            prompt, shot_dur, work, i, get_fallback_clip=get_fallback_clip
        )
        return i, prompt, clips, source

    results = [None] * len(shots)
    stock_count = ai_count = degraded_count = 0
    done = 0
    with ThreadPoolExecutor(max_workers=SHOT_FETCH_WORKERS) as executor:
        futures = [
            executor.submit(fetch_one, i, prompt, shot_dur)
            for i, (prompt, shot_dur) in enumerate(shots)
        ]
        for future in as_completed(futures):
            i, prompt, clips, source = future.result()
            results[i] = clips
            done += 1
            if source in ("stock", "ai_image"):
                set_fallback_clip(clips[-1])
                stock_count += (source == "stock")
                ai_count += (source == "ai_image")
            else:
                degraded_count += 1  # "reused" or "placeholder"
            print(f"  [{done}/{len(shots)}] shot {i+1} ({source}): {prompt[:60]!r}", flush=True)

    all_clip_paths = []
    for clips in results:
        all_clip_paths.extend(clips)
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

    print(f"NudeNet check this run: {_NSFW_RUN_STATS['checked']} images checked, "
          f"{_NSFW_RUN_STATS['flagged']} flagged.", flush=True)
    record_nsfw_test_entry(topic, category, country=country)

    hashtags = build_hashtags(script["hashtags"])
    # Log what was actually narrated (not what was requested) so the record
    # stays accurate when a native voice fell back to English.
    logged_language = native["name"] if (native and narrating_native) else language

    # Extra safety gate on top of the per-shot retry logic in
    # download_image()/prepare_shot_clips(), which already guarantees a
    # flagged image is never the one that actually ends up in the
    # assembled video (it gets retried away or swapped for a safe
    # fallback/stock/reused clip first). This is stricter still: if the
    # detector flagged ANYTHING at all while generating this video's
    # shots, don't publish or queue it for later publishing, even though
    # the visuals actually used are already clean. Deliberately
    # conservative given the strike history - a flag means the model tried
    # to produce unsafe content for this topic at least once, and that's
    # not a bar worth publishing right up against. Costs nothing: a fresh
    # topic gets tried automatically next cycle (~15 min later).
    if _NSFW_RUN_STATS["flagged"] > 0:
        print(f"  {_NSFW_RUN_STATS['flagged']} image(s) were flagged during generation for "
              f"this topic - skipping upload/queue entirely out of caution, even though the "
              f"assembled video only used visuals that passed the check. Will try a "
              f"different topic next cycle.", flush=True)
        return

    # Set SKIP_UPLOAD=true to render and save locally without touching
    # YouTube at all - useful while you're still dialing in quality/pacing
    # and don't want every test run to actually publish anything. Also used
    # to ride out the strike cool-down window (see daily-short.yml) - keeps
    # the full pipeline, including the NudeNet check, exercising and logging
    # every run without risking another upload attempt while the channel is
    # restricted.
    if os.environ.get("SKIP_UPLOAD", "false").lower() == "true":
        # Write the metadata this video would have been uploaded with
        # alongside it, so a later catch-up run (see --upload-queued) has
        # everything it needs to actually publish it once uploads resume -
        # the workflow packages latest_short.mp4 + this file together into
        # a GitHub Actions artifact so nothing generated during the pause
        # gets thrown away.
        meta = {
            "title": script["title"],
            "description": script["description"] + "\n\n" + " ".join(f"#{h}" for h in hashtags),
            "tags": hashtags,
            "topic": topic,
            "category": category,
            "country": country,
            "language": logged_language,
        }
        (final_path / "latest_short_meta.json").write_text(json.dumps(meta, indent=2))
        print(f"SKIP_UPLOAD is set - not uploading. Saved to {final_path / 'latest_short.mp4'} "
              f"(metadata alongside it for later catch-up upload).", flush=True)
        return

    print("Uploading to YouTube...", flush=True)
    video_id = upload_to_youtube(
        out_video,
        title=script["title"],
        description=script["description"] + "\n\n" + " ".join(f"#{h}" for h in hashtags),
        tags=hashtags,
    )
    record_performance_entry(video_id, category, logged_language, topic, country=country)


def _recheck_queued_video(video_path):
    """Re-run the CURRENT nudity detector over a queued video's actual
    frames before it gets published, instead of just trusting that it was
    clean at generation time.

    Why this matters specifically for the queue: a video can sit as a
    queued artifact for days before the catch-up path picks it up, and
    the detector running today isn't guaranteed to be the same one that
    checked it originally - e.g. this backlog was generated while the
    pipeline was briefly using NudeNet's 640m model, which was then
    reverted back to the bundled 320n model after a week of
    nsfw_test_log.json data. Re-checking with whatever detector is live
    right now (see _get_nude_detector) closes that gap instead of
    blindly publishing something an earlier/different model happened to
    wave through.

    Fails closed: any error (ffmpeg, detector, missing frames) counts as
    NOT safe, same philosophy as _is_flagged_nsfw() for freshly generated
    images.
    """
    try:
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            frames_pattern = str(td / "frame_%04d.jpg")
            subprocess.run(
                ["ffmpeg", "-y", "-i", str(video_path), "-vf", "fps=1/2", frames_pattern],
                check=True, capture_output=True, text=True, timeout=120,
            )
            frames = sorted(td.glob("frame_*.jpg"))
            if not frames:
                print("    Re-check: no frames extracted - treating as unsafe (fail closed).", flush=True)
                return False

            detector = _get_nude_detector()
            for frame in frames:
                detections = detector.detect(str(frame))
                for d in detections:
                    if (d.get("class") in NUDITY_DETECTION_CLASSES
                            and d.get("score", 0) >= NUDITY_DETECTION_THRESHOLD):
                        print(f"    Re-check FLAGGED: {d['class']} ({d['score']:.3f}) in {frame.name} - "
                              f"discarding this queued video, will NOT upload.", flush=True)
                        return False
            print(f"    Re-check passed: {len(frames)} frame(s) clean.", flush=True)
            return True
    except Exception as e:
        print(f"    Re-check failed ({type(e).__name__}: {e}) - treating as unsafe (fail closed), "
              f"will NOT upload.", flush=True)
        return False


def upload_queued_video(video_path, meta_path):
    """Catch-up path used by the workflow once SKIP_UPLOAD flips back off:
    uploads a video that was generated (and NudeNet-checked) during the
    strike cool-down but never published, using the metadata saved
    alongside it at generation time. Mirrors the tail end of run_once()'s
    normal upload path so it shows up in performance_log.json exactly like
    a live-generated video would.

    Re-checks the video against the CURRENT detector first (see
    _recheck_queued_video) rather than trusting whatever detector was
    live when it was generated - if that fails, this deliberately does
    NOT raise: it logs and returns normally so the workflow step still
    exits successfully, the consumed artifact still gets cleaned up (it's
    unsafe either way, no reason to keep it around or retry it), and the
    chain moves on to the next queued video/normal generation instead of
    getting stuck retrying a video that will never pass."""
    meta = json.loads(Path(meta_path).read_text())
    print(f"Re-checking queued video before upload: {meta['title']!r} (topic: {meta['topic']!r})", flush=True)
    if not _recheck_queued_video(Path(video_path)):
        print("Queued video did NOT pass re-check - skipping upload for this one.", flush=True)
        return

    print(f"Uploading queued video: {meta['title']!r} (topic: {meta['topic']!r})", flush=True)
    video_id = upload_to_youtube(
        Path(video_path),
        title=meta["title"],
        description=meta["description"],
        tags=meta["tags"],
    )
    record_performance_entry(video_id, meta["category"], meta["language"], meta["topic"], country=meta.get("country"))
    print(f"Queued video uploaded: {video_id}", flush=True)


def main():
    max_attempts = int(os.environ.get("MAX_ATTEMPTS", "3"))
    last_error = None

    # English only for now (was alternating en/hi - pick_language()/the
    # Hindi voice+prompt+font support is still intact in the code below,
    # just not invoked, in case this gets turned back on later).
    language = "en"

    # Weighted by how travel/food/tech videos have actually performed so
    # far (views + likes on videos old enough to have real stats) - see
    # compute_category_weights(). Categories with too little data yet get a
    # neutral weight so we keep exploring instead of over-committing early.
    weights = compute_category_weights()
    category = pick_category(weights)
    print(f"Category weights: { {k: round(v, 1) for k, v in weights.items()} }", flush=True)
    print(f"Chosen category: {category}", flush=True)

    # Alternate English / country-native narration on country videos, so the
    # feed reads as "one English, then one in that country's own language".
    # Only advances the toggle once per run (not per retry attempt).
    use_native = pick_use_native()

    for attempt in range(1, max_attempts + 1):
        # First attempt uses trending (if enabled). Retries force the static
        # topics.json rotation instead - trending would likely just return
        # the same top candidate again and fail the same way.
        force_static = attempt > 1
        topic, niche, country = pick_topic(category, force_static=force_static)

        # Native narration only applies to country-rotated categories - a
        # generic tech/AI topic has no country, so it stays English.
        native = COUNTRY_LANGUAGES.get(country) if (use_native and country) else None
        if use_native and not native:
            print("Native-narration slot, but this topic has no country - using English.", flush=True)

        try:
            run_once(topic, niche, language=language, category=category, native=native, country=country)
            usage = get_groq_run_usage()
            print(f"Success on attempt {attempt}/{max_attempts}. "
                  f"Groq usage this run: {usage['total_tokens']} tokens "
                  f"across {usage['calls']} call(s) - "
                  f"see performance_log.json for the running per-video record.")
            return
        except Exception as e:
            last_error = e
            print(f"Attempt {attempt}/{max_attempts} failed for topic {topic!r}: {e}")
            record_error_entry(topic, category, country, attempt, max_attempts, e)
            if attempt < max_attempts:
                print("Retrying with a different topic...")

    raise SystemExit(
        f"All {max_attempts} attempts failed. Last error: {last_error}"
    )


if __name__ == "__main__":
    # `python generate_short.py --upload-queued <video.mp4> <meta.json>` -
    # catch-up path the workflow uses to drain the backlog of videos
    # generated (and NudeNet-checked) during the SKIP_UPLOAD strike
    # cool-down, one per run, once uploads resume. Separate from the normal
    # `python generate_short.py` entry point below so a catch-up run never
    # also tries to generate a brand new video in the same job.
    if len(sys.argv) > 1 and sys.argv[1] == "--upload-queued":
        upload_queued_video(sys.argv[2], sys.argv[3])
    else:
        main()
