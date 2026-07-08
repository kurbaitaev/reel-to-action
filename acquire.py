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
import subprocess
import urllib.request

log = logging.getLogger("reel-to-action.acquire")

TMP = pathlib.Path("/tmp/reel-to-action")
# Read env at call time (not import) — bot.py loads .env after importing this module.


class AcquireError(RuntimeError):
    pass


_IG_RE = re.compile(r"instagram\.com/(reel|p|tv)/([A-Za-z0-9_-]+)")


def normalize_url(url: str) -> str:
    """Canonicalize an Instagram link to https://www.instagram.com/<reel|p|tv>/<code>/.

    Strips ?igsh=… and other query junk so the same reel maps to one key
    (for the ledger + Notion dedup). Non-Instagram URLs are returned unchanged.
    """
    url = (url or "").strip()
    m = _IG_RE.search(url)
    if m:
        return f"https://www.instagram.com/{m.group(1)}/{m.group(2)}/"
    return url


def is_instagram(url: str) -> bool:
    return "instagram.com" in url


def acquire(url: str) -> dict:
    """Return {source_url, platform, caption, author, title, video_path, raw}.

    video_path may be None if no video could be downloaded (caption-only).
    Raises AcquireError on hard failure.
    """
    TMP.mkdir(parents=True, exist_ok=True)
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

    transcript, caption, author, short, video_url = "", "", None, "reel", None
    try:
        t = _apify_run(transcriber, {"videoUrl": url}, token)
        if t:
            it = t[0]
            transcript = (it.get("text") or "").strip()
            caption = it.get("title") or ""
            author = it.get("userName") or it.get("userFullName")
            short = it.get("code") or "reel"
            log.info("transcript via %s (%d chars)", transcriber, len(transcript))
    except Exception as e:  # noqa: BLE001
        log.warning("primary transcriber failed (%s); falling back", e)

    kind = "video" if transcript else "unknown"
    video_path, images = None, []
    if not transcript:
        # No transcript → either a carousel (photos, no audio) or a video the
        # transcriber missed. Use the scraper to get media + detect which.
        scraper = os.environ.get("APIFY_SCRAPER_ACTOR", "apify~instagram-scraper").strip()
        it = {}
        try:
            s = _apify_run(scraper, {"directUrls": [url], "resultsType": "posts",
                                     "resultsLimit": 1, "addParentData": False}, token)
            it = s[0] if s else {}
        except Exception as e:  # noqa: BLE001
            log.warning("scraper fallback failed (%s)", e)
        caption = caption or it.get("caption") or ""
        author = author or it.get("ownerUsername") or it.get("ownerFullName")
        short = it.get("shortCode") or short
        img_urls = _carousel_images(it)
        if (it.get("type") == "Sidecar") or len(img_urls) > 1:
            kind = "carousel"
            for i, u in enumerate(img_urls[:12]):
                p = str(TMP / f"{short}-{i}.jpg")
                try:
                    urllib.request.urlretrieve(u, p)
                    images.append(p)
                except Exception as e:  # noqa: BLE001
                    log.warning("carousel image %d download failed (%s)", i, e)
            log.info("carousel: downloaded %d image(s)", len(images))
        else:
            video_url = it.get("videoUrl") or it.get("video_url")
            if video_url:
                kind = "video"
                video_path = str(TMP / f"{short}.mp4")
                try:
                    urllib.request.urlretrieve(video_url, video_path)
                    log.info("no transcript — downloaded video for agent-side transcription")
                except Exception as e:  # noqa: BLE001
                    log.warning("video download failed (%s)", e)
                    video_path = None

    if not (transcript or video_path or images or caption):
        raise AcquireError("Apify returned no transcript, video, images, or caption (private/removed?)")

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
    out_tmpl = str(TMP / "%(id)s.%(ext)s")
    try:
        subprocess.run(
            ["yt-dlp", "-o", out_tmpl, "--write-info-json", "--no-playlist", url],
            cwd=TMP, check=True, capture_output=True, text=True, timeout=300,
        )
    except subprocess.CalledProcessError as e:
        raise AcquireError(f"yt-dlp failed: {e.stderr[-500:]}") from e
    except FileNotFoundError as e:
        raise AcquireError("yt-dlp not installed") from e

    info = sorted(TMP.glob("*.info.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    meta = json.loads(info[0].read_text()) if info else {}
    vids = sorted(
        [p for p in TMP.glob("*") if p.suffix in {".mp4", ".mkv", ".webm"}],
        key=lambda p: p.stat().st_mtime, reverse=True,
    )
    return {
        "source_url": url,
        "platform": meta.get("extractor_key", "unknown"),
        "kind": "video",
        "caption": meta.get("description", ""),
        "author": meta.get("uploader") or meta.get("channel"),
        "title": meta.get("title", url),
        "transcript": "",  # yt-dlp path: agent transcribes the video file itself
        "detected_language": None,
        "video_path": str(vids[0]) if vids else None,
        "images": [],
    }


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    print(json.dumps(acquire(sys.argv[1]), indent=2))
