"""
Step 2 of the hybrid manual workflow. Run after you've saved clip_1.mp4,
clip_2.mp4, etc into queue/<job_id>/clips/ (see prepare_video_job.py).

Trims/loops each clip to its target beat duration, concatenates them, burns
in the captions, muxes the pre-made voiceover, and uploads to YouTube (or
just saves locally if SKIP_UPLOAD=true).

Usage:
    python finish_and_upload.py <job_id>
"""

import os
import sys
import json
import subprocess
import tempfile
from pathlib import Path

from generate_short import VIDEO_SIZE, FPS, upload_to_youtube

ROOT = Path(__file__).parent
QUEUE_DIR = ROOT / "queue"


def fit_clip_to_duration(src_path, target_dur, out_path):
    """Trims if the clip is longer than needed, or loops it if shorter, then
    scales/crops to the target vertical resolution."""
    probe = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(src_path)],
        capture_output=True, text=True, check=True,
    )
    src_dur = float(probe.stdout.strip())

    vf = (f"scale={VIDEO_SIZE[0]}:{VIDEO_SIZE[1]}:force_original_aspect_ratio=increase,"
          f"crop={VIDEO_SIZE[0]}:{VIDEO_SIZE[1]}")

    if src_dur >= target_dur:
        subprocess.run([
            "ffmpeg", "-y", "-i", str(src_path), "-t", str(target_dur),
            "-vf", vf, "-an", "-r", str(FPS),
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out_path)
        ], check=True, capture_output=True)
    else:
        # Loop the clip until it covers the target duration
        loops = int(target_dur // src_dur) + 1
        subprocess.run([
            "ffmpeg", "-y", "-stream_loop", str(loops), "-i", str(src_path),
            "-t", str(target_dur), "-vf", vf, "-an", "-r", str(FPS),
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out_path)
        ], check=True, capture_output=True)


def main():
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python finish_and_upload.py <job_id>")

    job_id = sys.argv[1]
    job_dir = QUEUE_DIR / job_id
    if not job_dir.exists():
        raise SystemExit(f"No job found at {job_dir}")

    meta = json.loads((job_dir / "meta.json").read_text())
    clips_dir = job_dir / "clips"
    beat_durations = meta["beat_durations"]
    num_beats = meta["num_beats"]

    missing = [i for i in range(1, num_beats + 1) if not (clips_dir / f"clip_{i}.mp4").exists()]
    if missing:
        raise SystemExit(
            f"Missing clips: {missing}. Expected clip_1.mp4 .. clip_{num_beats}.mp4 in {clips_dir}"
        )

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        fitted_paths = []
        for i, dur in enumerate(beat_durations, start=1):
            src = clips_dir / f"clip_{i}.mp4"
            out = td / f"fitted_{i}.mp4"
            print(f"Fitting clip {i}/{num_beats} to {dur:.1f}s...")
            fit_clip_to_duration(src, dur, out)
            fitted_paths.append(out)

        concat_file = td / "concat.txt"
        concat_file.write_text("\n".join(f"file '{p}'" for p in fitted_paths))

        silent_video = td / "silent.mp4"
        subprocess.run([
            "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_file),
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", str(FPS), str(silent_video)
        ], check=True, capture_output=True)

        srt_path = job_dir / "captions.ass"
        srt_escaped = str(srt_path).replace("\\", "\\\\").replace(":", "\\:")

        out_video = job_dir / "final.mp4"
        print("Muxing audio + burning captions...")
        subprocess.run([
            "ffmpeg", "-y", "-i", str(silent_video), "-i", str(job_dir / "voice.mp3"),
            "-vf", f"subtitles={srt_escaped}",
            "-c:v", "libx264", "-c:a", "aac", "-shortest", str(out_video)
        ], check=True)

    print(f"Final video ready: {out_video}")

    if os.environ.get("SKIP_UPLOAD", "false").lower() == "true":
        print("SKIP_UPLOAD is set - not uploading.")
        return

    print("Uploading to YouTube...")
    upload_to_youtube(
        out_video,
        title=meta["title"],
        description=meta["description"] + "\n\n" + " ".join(f"#{h}" for h in meta["hashtags"]),
        tags=meta["hashtags"],
    )


if __name__ == "__main__":
    main()
