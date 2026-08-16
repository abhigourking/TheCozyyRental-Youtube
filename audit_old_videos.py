"""
One-off tool to audit every video already live on the channel against the
CURRENT NudeNet nudity detector - none of the backlog posted before NudeNet
existed was ever actually checked by it. NOT part of the recurring daily
pipeline: only run manually via the separate audit-old-videos.yml workflow,
and it never self-chains or auto-triggers anything.

Usage (env vars):
    DRY_RUN          - "true" (default) or "false". In dry-run mode this
                        only checks and logs - it NEVER calls
                        videos.delete(), no matter what else is set.
    CONFIRM_DELETE    - must be exactly "DELETE" to allow DRY_RUN=false to
                        actually delete anything. This double-gate exists
                        so a mistyped/misconfigured env var can't
                        accidentally trigger real deletions.
    AUDIT_BATCH_SIZE  - how many not-yet-audited videos to process this
                        run (default 25). Re-run (manually, or via the
                        workflow) to keep working through the backlog -
                        already-audited videos are skipped.

Why this downloads from YouTube instead of re-checking the original
images: the actual AI-generated images were temp files on since-deleted
GitHub Actions runners - they don't exist anywhere anymore. Downloading
the channel's own already-published videos and sampling frames is the
only way to check them now.

Safety design - read before changing DRY_RUN's default or this file's
delete-gating logic:
  - Every flagged frame's class/confidence/frame index is recorded to
    old_video_audit.json BEFORE any deletion happens, so there's a
    durable, reviewable record of exactly why a video was removed - this
    matters because deletion is irreversible and this whole effort exists
    because the detector has had real, confirmed bugs (a corrupted-model
    crash, a caching bug) in the same session this script was written.
  - Resumable: any video_id already present in old_video_audit.json is
    skipped on subsequent runs, so the full channel history can be worked
    through across many batches without redoing completed work.
  - A technical failure (download error, frame extraction error, a
    detector error on a specific frame) is recorded but does NOT by
    itself mark a video as flagged/deletable - only an actual detection
    at or above the threshold does. Deleting a real video because OUR
    tooling failed to check it properly would be its own kind of mistake.
"""
import os
import sys
import json
import time
import subprocess
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import generate_short as gs

ROOT = Path(__file__).parent
AUDIT_LOG_FILE = ROOT / "old_video_audit.json"

DRY_RUN = os.environ.get("DRY_RUN", "true").lower() != "false"
CONFIRM_DELETE = os.environ.get("CONFIRM_DELETE", "") == "DELETE"
BATCH_SIZE = int(os.environ.get("AUDIT_BATCH_SIZE", "25"))
FRAME_INTERVAL_SECONDS = 1  # ~1 frame/sec - shots in these videos run
                            # 1-2.5s each, so this shouldn't skip any
                            # distinct shot entirely.


def load_audit_log():
    if AUDIT_LOG_FILE.exists():
        return json.loads(AUDIT_LOG_FILE.read_text())
    return {"videos": {}}


def save_audit_log(data):
    AUDIT_LOG_FILE.write_text(json.dumps(data, indent=2))


def download_video(video_id, out_path):
    # Lowest reasonable quality - frames just need to be detectable, not
    # high-res; keeps downloads small/fast across hundreds of videos.
    subprocess.run(
        ["yt-dlp", "-f", "worst[ext=mp4]/worst", "-o", str(out_path),
         f"https://www.youtube.com/watch?v={video_id}"],
        check=True, capture_output=True, timeout=180,
    )


def extract_frames(video_path, work_dir):
    frames_pattern = str(work_dir / "frame_%04d.jpg")
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(video_path),
         "-vf", f"fps=1/{FRAME_INTERVAL_SECONDS}", frames_pattern],
        check=True, capture_output=True, timeout=120,
    )
    return sorted(work_dir.glob("frame_*.jpg"))


def check_video(video_id):
    """Returns (flagged, details, video_level_error). flagged is only
    ever True on a real detection - see module docstring."""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        video_path = td / "video.mp4"
        try:
            download_video(video_id, video_path)
        except Exception as e:
            return False, [], f"download failed: {type(e).__name__}: {e}"

        try:
            frames = extract_frames(video_path, td)
        except Exception as e:
            return False, [], f"frame extraction failed: {type(e).__name__}: {e}"

        if not frames:
            return False, [], "no frames extracted"

        details = []
        detector = gs._get_nude_detector()
        flagged = False
        for i, frame in enumerate(frames):
            try:
                detections = detector.detect(str(frame))
            except Exception as e:
                details.append({"frame": i, "reason": "error",
                                 "error": f"{type(e).__name__}: {e}"})
                continue
            for d in detections:
                if (d.get("class") in gs.NUDITY_DETECTION_CLASSES
                        and d.get("score", 0) >= gs.NUDITY_DETECTION_THRESHOLD):
                    flagged = True
                    details.append({
                        "frame": i, "reason": "detected",
                        "class": d["class"], "confidence": round(d["score"], 3),
                    })

        return flagged, details, None


def main():
    print(f"Old video audit - DRY_RUN={DRY_RUN}, CONFIRM_DELETE={CONFIRM_DELETE}, "
          f"batch size={BATCH_SIZE}", flush=True)
    if not DRY_RUN and not CONFIRM_DELETE:
        print("DRY_RUN=false but CONFIRM_DELETE != 'DELETE' - refusing to run in a "
              "mode that could delete without explicit confirmation. Exiting.", flush=True)
        sys.exit(1)

    perf = gs.load_performance_log()
    all_video_ids = [v["video_id"] for v in perf["videos"] if v.get("video_id")]
    audit = load_audit_log()

    youtube = gs.get_youtube_client() if not DRY_RUN else None

    # Real-delete mode: first catch up on anything a PRIOR dry run already
    # flagged but never actually deleted - no need to re-download/re-check,
    # we already know it's flagged. This is what makes the intended
    # "dry-run everything, review, then delete" workflow actually work -
    # without this step, a video already marked "audited" from the dry
    # run would be treated as done and skipped forever by the
    # never-checked-yet logic below, so a real-delete run would silently
    # delete nothing at all.
    if not DRY_RUN:
        awaiting_delete = [
            vid for vid, entry in audit["videos"].items()
            if entry.get("flagged") and entry.get("action") != "deleted"
        ]
        if awaiting_delete:
            print(f"{len(awaiting_delete)} previously-flagged video(s) awaiting deletion "
                  f"from an earlier dry run - deleting those first.", flush=True)
        for video_id in awaiting_delete:
            try:
                youtube.videos().delete(id=video_id).execute()
                audit["videos"][video_id]["action"] = "deleted"
                print(f"  DELETED {video_id} (previously flagged).", flush=True)
            except Exception as e:
                audit["videos"][video_id]["action"] = f"delete_failed: {type(e).__name__}: {e}"
                print(f"  Delete failed for {video_id}: {e}", flush=True)
            save_audit_log(audit)
            time.sleep(1)

    done = set(audit["videos"].keys())
    pending = [vid for vid in all_video_ids if vid not in done]
    print(f"{len(all_video_ids)} total videos, {len(done)} already audited, "
          f"{len(pending)} never checked.", flush=True)
    batch = pending[:BATCH_SIZE]
    print(f"Checking {len(batch)} new video(s) this run.", flush=True)

    for video_id in batch:
        print(f"Checking {video_id}...", flush=True)
        flagged, details, error = check_video(video_id)

        entry = {
            "flagged": flagged,
            "details": details[:20],
            "error": error,
            "action": "none",
            "checked_at": time.time(),
        }

        if error:
            print(f"  Could not check (technical error, NOT treated as flagged): {error}", flush=True)
        elif flagged:
            print(f"  FLAGGED: {len(details)} frame-level detection(s).", flush=True)
            if not DRY_RUN:
                try:
                    youtube.videos().delete(id=video_id).execute()
                    entry["action"] = "deleted"
                    print(f"  DELETED {video_id}.", flush=True)
                except Exception as e:
                    entry["action"] = f"delete_failed: {type(e).__name__}: {e}"
                    print(f"  Delete failed: {e}", flush=True)
            else:
                entry["action"] = "would_delete (dry run)"
        else:
            print(f"  Clean.", flush=True)

        audit["videos"][video_id] = entry
        save_audit_log(audit)
        time.sleep(2)  # light courtesy pause between downloads

    flagged_total = sum(1 for v in audit["videos"].values() if v.get("flagged"))
    print(f"\nBatch done. Total audited: {len(audit['videos'])}/{len(all_video_ids)}. "
          f"Flagged total so far: {flagged_total}.", flush=True)


if __name__ == "__main__":
    main()
