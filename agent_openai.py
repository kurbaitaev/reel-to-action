#!/usr/bin/env python3
"""OpenAI reasoning backend — the alternative to the Claude Code CLI.

Why this exists: the Claude Code CLI authenticates through an interactive
browser login, which cannot run on a server. An API key can, so this backend
is what makes hosting possible. It's also the cheaper-to-operate path when you
don't already pay for a Claude subscription.

Uses the Responses API, which resolves web searches server-side inside a single
request, so there's no client-side tool loop to maintain. Output is constrained
by a strict JSON schema — the marker-based (@@JSON@@) contract the CLI backend
needs is unnecessary here, and the schema can't be "forgotten" by the model.

    OPENAI_API_KEY=sk-...        required
    OPENAI_MODEL=gpt-5.6-terra   optional override
"""

import base64
import json
import logging
import mimetypes
import os
import pathlib
import subprocess

log = logging.getLogger("reel-to-action.openai")

DEFAULT_MODEL = "gpt-5.6-terra"  # vision + web search at 1/2.5 the price of -sol
MAX_IMAGES = 12
# GPT-5.6 does NOT downscale images on its own, so a full-size carousel slide
# would be billed at full resolution. 720px wide keeps the patch count small
# and is plenty to read on-screen text.
MAX_IMAGE_WIDTH = 720

# Searches are billed per call ($0.01), so keep verification focused on the
# domains that actually count as canonical for our item types.
VERIFY_DOMAINS = [
    "goodreads.com", "amazon.com", "open.spotify.com", "podcasts.apple.com",
    "youtube.com", "wikipedia.org", "github.com", "apps.apple.com",
    "investopedia.com", "imdb.com",
]

_ITEM = {
    "type": "object",
    "properties": {
        "type": {"type": "string",
                 "enum": ["book", "podcast", "tool", "product", "resource",
                          "video", "channel", "concept"]},
        "name": {"type": "string"},
        "author": {"type": ["string", "null"]},
        "link": {"type": ["string", "null"]},
        "verified": {"type": "boolean"},
        "verify_note": {"type": ["string", "null"]},
    },
    "required": ["type", "name", "author", "link", "verified", "verify_note"],
    "additionalProperties": False,
}

_SLIDE = {
    "type": "object",
    "properties": {
        "text": {"type": "string"},
        "description": {"type": ["string", "null"]},
    },
    "required": ["text", "description"],
    "additionalProperties": False,
}

# Mirrors the contract in agent_prompt.md. Strict mode requires EVERY property
# to be listed in `required`, so anything optional is typed as nullable instead.
SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "content_type": {
            "type": "string",
            "enum": ["quote", "motivational_quote", "thought", "tip", "educational",
                     "tutorial", "book_recommendation", "podcast_recommendation",
                     "tool_recommendation", "product_recommendation", "resource_list",
                     "story", "opinion", "other"]},
        "kind": {"type": "string", "enum": ["video", "carousel", "image", "article"]},
        "categories": {"type": "array", "items": {"type": "string"}},
        "author": {"type": ["string", "null"]},
        "quote": {"type": ["string", "null"]},
        "context": {"type": ["string", "null"]},
        "main_idea": {"type": ["string", "null"]},
        "main_thought": {"type": ["string", "null"]},
        "takeaway": {"type": ["string", "null"]},
        "useful_for": {"type": ["string", "null"]},
        "points": {"type": "array", "items": {"type": "string"}},
        "steps": {"type": "array", "items": {"type": "string"}},
        "items": {"type": "array", "items": _ITEM},
        "slides": {"type": "array", "items": _SLIDE},
        "why_save": {"type": ["string", "null"]},
        "tags": {"type": "array", "items": {"type": "string"}},
        "description": {"type": "string"},
        "summary": {"type": "string"},
    },
    "required": ["title", "content_type", "kind", "categories", "author", "quote",
                 "context", "main_idea", "main_thought", "takeaway", "useful_for",
                 "points", "steps", "items", "slides", "why_save", "tags",
                 "description", "summary"],
    "additionalProperties": False,
}

# The shared content rules live in agent_prompt.md so both backends stay in
# sync. These few lines override the parts that are specific to the CLI.
_OVERRIDE = """
IMPORTANT — this run differs from the instructions below in three ways:

1. The images are ATTACHED to this message, not supplied as file paths. There is
   no Read tool. Examine EVERY attached image before writing anything; that is
   where on-screen text, book covers, handles and lists live.
2. Ignore anything about `@@JSON@@` / `@@END@@` markers. Your output format is
   enforced by a JSON schema — just answer.
3. Use your built-in web search to verify links. Anything you cannot confirm
   must be verified:false with a short verify_note. Never invent a URL.

For fields that don't apply, use null (or an empty array) rather than inventing
content. Keep "Not clear from the Reel" / "Author not clear" where the rules
below call for them.
"""


def enabled() -> bool:
    return bool(os.environ.get("OPENAI_API_KEY", "").strip())


def _shrink(path: str) -> bytes:
    """Downscale wide images with ffmpeg (already a dependency) so we don't pay
    to send a 1080px carousel slide when 720px reads identically."""
    raw = pathlib.Path(path).read_bytes()
    try:
        r = subprocess.run(
            ["ffmpeg", "-nostdin", "-loglevel", "error", "-i", path,
             "-vf", f"scale='min({MAX_IMAGE_WIDTH},iw)':-2", "-q:v", "5", "-f", "mjpeg", "-"],
            capture_output=True, timeout=60)
        return r.stdout or raw
    except Exception as e:  # noqa: BLE001
        log.debug("image shrink failed for %s (%s) — sending as-is", path, e)
        return raw


def _image_part(path: str) -> dict:
    data = _shrink(path)
    mime = mimetypes.guess_type(path)[0] or "image/jpeg"
    if data[:2] == b"\xff\xd8":  # ffmpeg re-encoded it to jpeg
        mime = "image/jpeg"
    b64 = base64.standard_b64encode(data).decode("ascii")
    return {"type": "input_image", "image_url": f"data:{mime};base64,{b64}", "detail": "high"}


def analyze(instructions: str, context_text: str, image_paths: list[str] | None = None) -> dict:
    """Run one reel through the model and return the note object.

    Raises RuntimeError with a readable message on failure — bot.py turns that
    into a Telegram reply rather than a silent drop.
    """
    try:
        from openai import OpenAI
    except ImportError as e:
        raise RuntimeError(
            "The openai package isn't installed — run: pip install -r requirements.txt") from e

    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        raise RuntimeError("OPENAI_API_KEY is not set")
    model = os.environ.get("OPENAI_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL

    content: list[dict] = [{"type": "input_text", "text": context_text}]
    for p in (image_paths or [])[:MAX_IMAGES]:
        if pathlib.Path(p).exists():
            content.append(_image_part(p))
    n_images = len(content) - 1

    client = OpenAI(api_key=key, timeout=600.0)
    try:
        resp = client.responses.create(
            model=model,
            instructions=_OVERRIDE + "\n\n" + instructions,
            input=[{"role": "user", "content": content}],
            tools=[{"type": "web_search",
                    "search_context_size": "medium",
                    "filters": {"allowed_domains": VERIFY_DOMAINS}}],
            tool_choice="auto",
            text={"format": {"type": "json_schema", "name": "reel_note",
                             "strict": True, "schema": SCHEMA}},
            max_output_tokens=8000,
            store=False,
        )
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"OpenAI request failed: {type(e).__name__}: {e}") from e

    text = (getattr(resp, "output_text", "") or "").strip()
    if not text:
        raise RuntimeError("OpenAI returned no output (possibly hit max_output_tokens)")
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"OpenAI returned invalid JSON: {e}") from e

    searches = sum(1 for i in (getattr(resp, "output", None) or [])
                   if getattr(i, "type", "") == "web_search_call")
    usage = getattr(resp, "usage", None)
    log.info("openai %s: %d image(s), %d search(es), in=%s out=%s",
             model, n_images, searches,
             getattr(usage, "input_tokens", "?"), getattr(usage, "output_tokens", "?"))
    return obj


if __name__ == "__main__":  # tiny credential check: python3 agent_openai.py
    logging.basicConfig(level=logging.INFO)
    if not enabled():
        raise SystemExit("OPENAI_API_KEY is not set (put it in .env)")
    from openai import OpenAI
    c = OpenAI(api_key=os.environ["OPENAI_API_KEY"].strip())
    r = c.responses.create(model=os.environ.get("OPENAI_MODEL", DEFAULT_MODEL),
                           input="Reply with exactly: OK", max_output_tokens=2000)
    print("model reachable ->", (r.output_text or "").strip()[:40])
