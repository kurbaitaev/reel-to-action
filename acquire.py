#!/usr/bin/env python3
"""Acquisition layer: turn a URL into local media + metadata.

Instagram → Apify instagram-scraper (proxied, anti-bot-resistant).
Everything else → yt-dlp fallback.

Kept separate from the agent on purpose: acquisition is the fragile,
infrastructure-heavy part and belongs in deterministic code, not in
model turns. This module is also what a future hosted backend will call.
"""

import json
import logging
import os
import pathlib
import re
import shutil
import subprocess
import time
import urllib.request

log = logging.getLogger("reel-to-action.acquire")

TMP = pathlib.Path("/tmp/reel-to-action")
# Read env at call time (not import) — bot.py loads .env after importing this module.

# A lot of reels carry their real content on screen (book titles, lists, numbers)
# rather than in speech. We sample frames from the video so the agent can read them.
# VIDEO_FRAMES=0 disables it.
THIN_TRANSCRIPT = 200  # below this, the reel is probably visual-first → sample more


class AcquireError(RuntimeError):
    pass


_IG_RE = re.compile(r"instagram\.com/(reel|reels|p|tv)/([A-Za-z0-9_-]+)")


def normalize_url(url: str) -> str:
    """Canonicalize an Instagram link to https://www.instagram.com/<reel|p|tv>/<code>/.

    Strips ?igsh=… and other query junk so the same reel maps to one key
    (for the ledger + Notion dedup). Non-Instagram URLs are returned unchanged.
    """
    url = (url or "").strip()
    m = _IG_RE.search(url)
    if m:
        kind = "reel" if m.group(1) == "reels" else m.group(1)  # /reels/ and /reel/ are the same post
        return f"https://www.instagram.com/{kind}/{m.group(2)}/"
    return url


def _shortcode(url: str) -> str:
    """Stable per-reel prefix for temp files, so two reels can't overwrite
    each other's media (the old code fell back to the literal 'reel')."""
    m = _IG_RE.search(url or "")
    return m.group(2) if m else re.sub(r"\W+", "", (url or "x"))[-16:] or "reel"


def _download(src: str, dest: str, timeout: int = 60) -> None:
    """urlretrieve has no timeout — a half-open CDN socket would hang forever."""
    with urllib.request.urlopen(src, timeout=timeout) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)


def _duration(path: str) -> float:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", path],
            capture_output=True, text=True, timeout=30)
        return float(r.stdout.strip())
    except Exception:  # noqa: BLE001
        return 0.0


def video_frames(video_path: str, short: str, n: int) -> list:
    """Sample n evenly-spaced frames so the agent can read on-screen text.

    Skips the first/last 8% — reels usually open on a title card and end on a
    CTA, and the middle is where the actual content sits.
    """
    if n <= 0 or not video_path or not pathlib.Path(video_path).exists():
        return []
    dur = _duration(video_path)
    if dur <= 0:
        return []
    span = dur * 0.84
    start = dur * 0.08
    stamps = [start + span * (i / max(n - 1, 1)) for i in range(n)] if n > 1 else [dur / 2]
    frames = []
    for i, t in enumerate(stamps):
        out = str(TMP / f"{short}-frame{i}.jpg")
        try:
            subprocess.run(
                ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-ss", f"{t:.2f}",
                 "-i", video_path, "-frames:v", "1", "-vf", "scale=720:-2", "-q:v", "4", out],
                capture_output=True, timeout=60, check=True)
            if pathlib.Path(out).stat().st_size > 0:
                frames.append(out)
        except Exception as e:  # noqa: BLE001
            log.warning("frame %d extraction failed (%s)", i, e)
    log.info("sampled %d frame(s) for on-screen text", len(frames))
    return frames


def sweep_stale(max_age_h: int = 24) -> None:
    """Backstop for media left behind by a crashed run — without it the temp dir
    grows forever and stale files can be mistaken for the current reel's."""
    cutoff = time.time() - max_age_h * 3600
    for p in TMP.glob("*"):
        try:
            if p.stat().st_mtime < cutoff:
                shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True)
        except OSError:
            pass


def cleanup(media: dict) -> None:
    """Delete downloaded media once the note is written — nothing here is reused,
    and leaving it behind both fills the disk and lets a later reel pick up a
    stale file."""
    for p in ([media.get("video_path")] + list(media.get("images") or [])
              + list(media.get("frames") or [])):
        if p:
            try:
                pathlib.Path(p).unlink(missing_ok=True)
            except OSError:
                pass
    run_dir = TMP / f"ytdlp-{_shortcode(media.get('source_url') or '')}"
    if run_dir.is_dir():
        shutil.rmtree(run_dir, ignore_errors=True)


def is_instagram(url: str) -> bool:
    return "instagram.com" in url


def acquire(url: str) -> dict:
    """Return {source_url, platform, caption, author, title, video_path, raw}.

    video_path may be None if no video could be downloaded (caption-only).
    Raises AcquireError on hard failure.
    """
    TMP.mkdir(parents=True, exist_ok=True)
    sweep_stale()
    token = os.environ.get("APIFY_TOKEN", "").strip()
    if is_instagram(url) and token:
        return _acquire_apify_instagram(url, token)
    if is_instagram(url) and not token:
        log.warning("Instagram URL but APIFY_TOKEN unset — falling back to yt-dlp (may be IP-blocked)")
    return _acquire_ytdlp(url)


# --- Apify ---------------------------------------------------------------

def _apify_run(actor: str, payload: dict, token: str, timeout: int = 300) -> list:
    endpoint = (
        f"https://api.apify.com/v2/acts/{actor}"
        f"/run-sync-get-dataset-items?token={token}&timeout={timeout}"
    )
    req = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout + 30) as r:
        items = json.loads(r.read().decode())
    return items if isinstance(items, list) else []


def _acquire_apify_instagram(url: str, token: str) -> dict:
    """ONE fast actor call (apple_yang) → transcript + caption + author together.

    The primary actor returns the spoken transcript, the caption, and the author
    in a single ~7s call, so there's no video download and no agent-side
    transcription on the happy path. Only if it returns nothing do we fall back
    to the scraper + video download (agent transcribes via Gemini).
    """
    transcriber = os.environ.get("APIFY_TRANSCRIBER_ACTOR", "apple_yang~instagram-transcripts-scraper").strip()

    short = _shortcode(url)
    transcript, caption, author, video_url, last_err = "", "", None, None, None
    try:
        t = _apify_run(transcriber, {"videoUrl": url}, token)
        if t:
            it = t[0]
            transcript = (it.get("text") or "").strip()
            caption = it.get("title") or ""
            author = it.get("userName") or it.get("userFullName")
            short = it.get("code") or short
            video_url = it.get("videoUrl")  # given up-front — no extra scrape needed
            log.info("transcript via %s (%d chars)", transcriber, len(transcript))
    except Exception as e:  # noqa: BLE001
        last_err = e
        log.warning("primary transcriber failed (%s); falling back", e)

    kind = "video" if transcript else "unknown"
    video_path, images, frames = None, [], []

    if not (transcript and video_url):
        # Missing transcript or media → ask the scraper what this post actually is
        # (photo, carousel, or a video the transcriber choked on).
        scraper = os.environ.get("APIFY_SCRAPER_ACTOR", "apify~instagram-scraper").strip()
        it = {}
        try:
            s = _apify_run(scraper, {"directUrls": [url], "resultsType": "posts",
                                     "resultsLimit": 1, "addParentData": False}, token)
            it = s[0] if s else {}
        except Exception as e:  # noqa: BLE001
            last_err = e
            log.warning("scraper fallback failed (%s)", e)
        caption = caption or it.get("caption") or ""
        author = author or it.get("ownerUsername") or it.get("ownerFullName")
        short = it.get("shortCode") or short
        video_url = video_url or it.get("videoUrl") or it.get("video_url")
        img_urls = _carousel_images(it)
        if not video_url and img_urls:
            # Photo post or carousel — the text lives in the pixels, so this is
            # the whole content. A single-image post used to be dropped here.
            kind = "carousel" if (it.get("type") == "Sidecar" or len(img_urls) > 1) else "image"
            for i, u in enumerate(img_urls[:12]):
                p = str(TMP / f"{short}-slide{i}.jpg")
                try:
                    _download(u, p)
                    images.append(p)
                except Exception as e:  # noqa: BLE001
                    log.warning("image %d download failed (%s)", i, e)
            log.info("%s: downloaded %d image(s)", kind, len(images))

    if video_url and not images:
        kind = "video"
        video_path = str(TMP / f"{short}.mp4")
        try:
            _download(video_url, video_path, timeout=120)
        except Exception as e:  # noqa: BLE001
            log.warning("video download failed (%s)", e)
            video_path = None
        # Sample frames so on-screen text is captured even when the speech
        # doesn't mention it. Visual-first reels (little speech) get more.
        n = int(os.environ.get("VIDEO_FRAMES", "6"))
        if len(transcript) < THIN_TRANSCRIPT:
            n = max(n, 8)
        frames = video_frames(video_path, short, n)
        if transcript and video_path:
            # Transcript already in hand — the video file itself is only needed
            # for agent-side transcription, so drop it and keep the frames.
            pathlib.Path(video_path).unlink(missing_ok=True)
            video_path = None

    if not (transcript or video_path or images or frames or caption):
        detail = f" ({last_err})" if last_err else ""
        raise AcquireError(f"Couldn't get anything back for this post — private, removed, or Apify failed{detail}")

    return {
        "source_url": url,
        "platform": "instagram",
        "kind": kind,
        "caption": caption,
        "author": author,
        "title": (caption[:80] if caption else short),
        "transcript": transcript,
        "detected_language": None,
        "video_path": video_path,
        "images": images,
        "frames": frames,
    }


def _carousel_images(it: dict) -> list:
    """Pull image URLs from an instagram-scraper item (sidecar/carousel or single)."""
    urls = []
    if isinstance(it.get("images"), list):
        urls = [u for u in it["images"] if isinstance(u, str)]
    if not urls and isinstance(it.get("childPosts"), list):
        urls = [c.get("displayUrl") for c in it["childPosts"] if c.get("displayUrl")]
    if not urls and it.get("displayUrl"):
        urls = [it["displayUrl"]]
    return urls


# --- yt-dlp fallback -----------------------------------------------------

def _acquire_ytdlp(url: str) -> dict:
    # Own directory per run: globbing the shared TMP could pick up a *different*
    # reel's leftover video and attach it to this note.
    run_dir = TMP / f"ytdlp-{_shortcode(url)}"
    run_dir.mkdir(parents=True, exist_ok=True)
    out_tmpl = str(run_dir / "%(id)s.%(ext)s")
    try:
        subprocess.run(
            ["yt-dlp", "-o", out_tmpl, "--write-info-json", "--no-playlist", url],
            cwd=run_dir, check=True, capture_output=True, text=True, timeout=300,
        )
    except subprocess.CalledProcessError as e:
        raise AcquireError(f"yt-dlp failed: {e.stderr[-500:]}") from e
    except FileNotFoundError as e:
        raise AcquireError("yt-dlp not installed") from e

    info = sorted(run_dir.glob("*.info.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    meta = json.loads(info[0].read_text()) if info else {}
    vids = sorted(
        [p for p in run_dir.glob("*") if p.suffix in {".mp4", ".mkv", ".webm"}],
        key=lambda p: p.stat().st_mtime, reverse=True,
    )
    video_path = str(vids[0]) if vids else None
    frames = video_frames(video_path, _shortcode(url), int(os.environ.get("VIDEO_FRAMES", "6")))
    return {
        "source_url": url,
        "platform": meta.get("extractor_key", "unknown"),
        "kind": "video",
        "caption": meta.get("description", ""),
        "author": meta.get("uploader") or meta.get("channel"),
        "title": meta.get("title", url),
        "transcript": "",  # yt-dlp path: agent transcribes the video file itself
        "detected_language": None,
        "video_path": video_path,
        "images": [],
        "frames": frames,
    }


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    print(json.dumps(acquire(sys.argv[1]), indent=2))
