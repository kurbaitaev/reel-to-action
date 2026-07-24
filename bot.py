#!/usr/bin/env python3
"""Telegram bot: send it a reel/video/article link, get actionable items back.

Thin front door — all intelligence lives in a headless Claude Code agent
(see agent_prompt.md). Permissions for the agent are scoped in .claude/settings.json.

Usage:
    python3 bot.py                 # run the bot (needs TELEGRAM_BOT_TOKEN in .env)
    python3 bot.py --test <url>    # run the pipeline once without Telegram
"""

import asyncio
import datetime
import html
import json
import logging
import os
import re
import shutil
import sys
import urllib.parse
import urllib.request
from pathlib import Path

import acquire
import ledger
import notion

PROJECT_DIR = Path(__file__).resolve().parent
PROMPT_FILE = PROJECT_DIR / "agent_prompt.md"
CLAUDE_BIN = shutil.which("claude") or "/opt/homebrew/bin/claude"
AGENT_TIMEOUT_S = 15 * 60
URL_RE = re.compile(r"https?://\S+")

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
# httpx logs full request URLs, and Telegram's URLs embed the bot token — that
# would write the token into every log line. Errors still surface.
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("reel-to-action")

# The watchdog uses this file's mtime to tell "alive and polling" from "stuck".
HEARTBEAT = PROJECT_DIR / "logs" / "heartbeat"


def load_env() -> None:
    env_file = PROJECT_DIR / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


JSON_RE = re.compile(r"@@JSON@@\s*(.*?)\s*@@END@@", re.DOTALL)

def build_prompt(url: str, media: dict) -> str:
    """Compose the agent prompt with pre-acquired media context."""
    ctx = [f"URL: {url}", f"Platform: {media.get('platform')}", f"Kind: {media.get('kind')}"]
    if media.get("author"):
        ctx.append(f"Author: {media['author']}")
    if media.get("caption"):
        ctx.append(f"Caption:\n{media['caption'][:2000]}")
    if media.get("images"):
        paths = "\n".join(media["images"])
        label = "CAROUSEL" if media.get("kind") == "carousel" else "PHOTO POST"
        ctx.append(
            f"{label} — there is no audio, so the images ARE the content. Read EACH of these "
            "local image files with the Read tool, capture the verbatim on-screen text + a short "
            f"description, and fill `slides` (one entry per image, in order):\n{paths}"
        )
    elif media.get("transcript"):
        ctx.append(
            "TRANSCRIPT (verbatim, video — do NOT re-transcribe or paste it back; "
            f"use it to write description/summary/items):\n{media['transcript'][:12000]}"
        )
    elif media.get("video_path"):
        ctx.append(
            f"No transcript. Video downloaded at: {media['video_path']} — transcribe it with "
            f"the gemini-analyze MCP (do NOT run yt-dlp)."
        )
    elif not media.get("frames"):
        ctx.append("No transcript/images — use the caption; fetch the URL if it's an article.")
    # Frames are supplementary: they can accompany a transcript, or (when the
    # speech is thin) carry the content on their own.
    if media.get("frames"):
        paths = "\n".join(media["frames"])
        ctx.append(
            "FRAMES sampled from the video — Read these with the Read tool. Reels often put the "
            "real content on screen (book titles, names, numbered lists, prices, handles) and "
            "never say it out loud. Mine them for anything the transcript is missing and fold it "
            "into `items`/`points`/`quote`. If a frame shows a book cover, product, or account, "
            "that's an `item` — verify it like any other. Do NOT describe camera work, outfits, "
            f"or scenery, and do NOT invent text you cannot actually read:\n{paths}"
        )
    return (
        PROMPT_FILE.read_text()
        + f"\n\n---\nToday's date: {datetime.date.today().isoformat()}\n"
        + "\n".join(ctx)
        + "\n"
    )


async def run_agent(prompt: str) -> str:
    args = [CLAUDE_BIN, "-p", prompt, "--max-turns", "60"]
    model = os.environ.get("CLAUDE_MODEL", "").strip()
    if model:
        args += ["--model", model]
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=PROJECT_DIR,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=AGENT_TIMEOUT_S)
    except asyncio.TimeoutError:
        proc.kill()
        return "⏰ Timed out after 15 minutes. Try again or check the link."
    if proc.returncode != 0:
        log.error("agent failed (%s): %s", proc.returncode, stderr.decode()[-2000:])
        return f"❌ Agent failed:\n{stderr.decode()[-1500:] or stdout.decode()[-1500:]}"
    return stdout.decode().strip() or "❌ Agent returned no output."


async def run_pipeline(url: str, force: bool = False, on_progress=None,
                       media: dict | None = None) -> tuple[str, str | None]:
    """Acquire → reason → persist. Returns (html_message, rich_markdown_or_None).

    on_progress(stage) is an optional async callback fired at real milestones
    ("acquired") so the caller can update a status message in place.
    media, if supplied, skips acquisition (used to re-verify from a stored transcript).
    """
    url = acquire.normalize_url(url)  # strip ?igsh=… so the same reel is one key
    cached = ledger.get(url)
    if cached and cached.get("status") == "done" and not force:
        note = "\n\n(already processed — send /force to redo)"
        md = cached.get("markdown")
        return cached["digest"] + note, (md + "\n\n*(already processed — /force to redo)*" if md else None)

    if media is None:
        # Acquisition is blocking I/O — keep the event loop free.
        try:
            media = await asyncio.to_thread(acquire.acquire, url)
        except acquire.AcquireError as e:
            log.error("acquire failed for %s: %s", url, e)
            return f"❌ Couldn't fetch that link:\n{e}", None

    if on_progress:
        await on_progress("acquired")
    raw = await run_agent(build_prompt(url, media))
    if raw.startswith(("❌", "⏰")):
        return html.escape(raw), None

    obj = _parse_output(raw)
    if obj is None:
        # Couldn't parse structured output — send the cleaned text as-is.
        log.warning("no @@JSON@@ block in agent output for %s", url)
        return html.escape(JSON_RE.sub("", raw).strip()), None

    obj = _sanitize(obj)
    n_bad = _validate_links(obj)
    if n_bad:
        log.info("downgraded %d non-canonical 'verified' link(s) for %s", n_bad, url)

    transcript = media.get("transcript", "") or ""
    # Preserve the original 'added' date on re-process; new reels get today.
    prior = await asyncio.to_thread(notion.existing_date, url) if notion.enabled() else None
    date_iso = prior or datetime.date.today().isoformat()
    # vault write (disk) and Notion sync (network) are independent — run them together
    sinks = [asyncio.to_thread(_write_vault_note, obj, url, transcript, date_iso)]
    if notion.enabled():
        sinks.append(asyncio.to_thread(_sync_notion, obj, media, url, transcript, date_iso))
    # A failing sink must NOT abort delivery or skip ledger.put (else the reel is
    # lost: stuck placeholder, never marked done, pending removed → no recovery).
    for r in await asyncio.gather(*sinks, return_exceptions=True):
        if isinstance(r, Exception):
            log.warning("sink (vault/Notion) failed for %s: %s", url, r)

    message = render_telegram(obj, url)
    rich_md = render_rich(obj, url, transcript)
    ledger.put(url, {
        "status": "done",
        "digest": message,
        "markdown": rich_md,
        "platform": media.get("platform"),
        "ts": datetime.datetime.now().isoformat(timespec="seconds"),
    })
    # The agent has read everything it needs; keeping the media would only fill
    # the disk and risk a later reel picking up a stale file.
    await asyncio.to_thread(acquire.cleanup, media)
    if force:
        # redo = replace: drop older Notion row + vault note for this reel
        n = await asyncio.to_thread(notion.dedupe_by_source, url) if notion.enabled() else 0
        v = _dedupe_vault_by_source(url)
        if n or v:
            log.info("redo cleanup for %s: archived %d notion, removed %d vault dup(s)", url, n, v)
    return message, rich_md


def _dedupe_vault_by_source(url: str) -> int:
    """Keep only the newest vault note for a given reel source; delete older ones."""
    d = PROJECT_DIR / "vault" / "Action Inbox"
    if not d.exists():
        return 0
    matches = []
    for n in d.glob("*.md"):
        try:
            m = re.search(r"^source:\s*(\S+)", n.read_text(), re.M)
        except OSError:
            continue
        if m and m.group(1) == url:
            matches.append(n)
    matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    removed = 0
    for old in matches[1:]:
        try:
            old.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def _parse_output(raw: str) -> dict | None:
    m = JSON_RE.search(raw)
    if not m:
        return None
    try:
        obj = json.loads(m.group(1))
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


_SEARCH_URL = re.compile(
    r"(/search\b|/results\b|[?&]q=|[?&]query=|search_query=|google\.[a-z.]+/search|"
    r"bing\.com/search|duckduckgo\.com)", re.I)


_STR_LISTS = ("points", "steps", "tags", "categories")


def _sanitize(obj: dict) -> dict:
    """The agent is a model, so its JSON shape is a request, not a guarantee.
    Coerce the collection fields to what every renderer downstream assumes —
    one stray list-of-strings in `items` used to crash the whole reel."""
    for key in _STR_LISTS:
        val = obj.get(key)
        if isinstance(val, list):
            obj[key] = [str(v).strip() for v in val if isinstance(v, (str, int, float)) and str(v).strip()]
        elif val is not None:
            obj[key] = [str(val)] if isinstance(val, (str, int, float)) else []
    for key in ("items", "slides"):
        val = obj.get(key)
        obj[key] = [v for v in val if isinstance(v, dict)] if isinstance(val, list) else []
    return obj


def _validate_links(obj: dict) -> int:
    """Make 'verified' trustworthy: a verified item must point at a real canonical URL,
    not a search-results page. Downgrade any that don't (model-independent guard).
    Returns how many were downgraded."""
    downgraded = 0
    for it in obj.get("items") or []:
        if not it.get("verified"):
            continue
        link = (it.get("link") or "").strip()
        if not link.lower().startswith(("http://", "https://")) or _SEARCH_URL.search(link):
            it["verified"] = False
            it["verify_note"] = (it.get("verify_note") or "").strip() or "search link — source not confirmed"
            downgraded += 1
    return downgraded


_QUOTE_TYPES = {"quote", "motivational_quote"}
_REC_TYPES = {"book_recommendation", "podcast_recommendation", "tool_recommendation",
              "product_recommendation", "resource_list"}
_EDU_TYPES = {"educational", "tip"}
_THOUGHT_TYPES = {"thought", "opinion", "story"}
_NA = {"", "not clear from the reel", "author not clear"}


def _esc(s: str) -> str:
    return html.escape((s or "").strip())


def _ok(s: str) -> bool:
    return bool(s) and s.strip().lower() not in _NA


def _slides_html(obj: dict) -> str:
    parts = []
    for i, s in enumerate(obj.get("slides") or [], 1):
        d = _esc(s.get("description"))
        t = _esc(s.get("text"))
        seg = f"<b>Slide {i}</b>" + (f" — <i>{d}</i>" if d else "")
        if t:
            seg += f"\n{t}"
        parts.append(seg)
    return "\n\n".join(parts)


def _detail_text(obj: dict, transcript: str) -> str:
    """Summary + transcript/slides for the collapsible reference block."""
    parts = []
    if _ok(obj.get("summary")):
        parts.append(_esc(obj["summary"]))
    if obj.get("slides"):
        parts.append(_slides_html(obj))
    elif transcript.strip():
        parts.append(_esc(transcript))
    return "\n\n".join(parts)


def _rec_lines(items: list) -> list[str]:
    """Recommended items, one per line, with verify mark + link (no leading bullet)."""
    out = []
    for it in items or []:
        verified = bool(it.get("verified"))
        mark = "✅" if verified else "⚠️"
        name = _esc(it.get("name"))
        link = (it.get("link") or "").strip()
        author = _esc(it.get("author"))
        body = (f'<a href="{html.escape(link, quote=True)}">{name}</a>'
                if link.lower().startswith(("http://", "https://")) else name)
        if author:
            body += f" — {author}"
        line = f"{mark} {body}"
        note = _esc(it.get("verify_note"))
        if not verified and note:
            line += f" — <i>{note}</i>"
        out.append(line)
    return out


def _link_line(url: str) -> str:
    return f'🔗 <a href="{html.escape(url, quote=True)}">Original reel</a>' if url else ""


def _tags_line(obj: dict) -> str:
    tags = [t for t in (obj.get("tags") or []) if t]
    return ("<i>" + " ".join("#" + html.escape(str(t).strip().replace(" ", "_")) for t in tags) + "</i>"
            if tags else "")


# Content-type → ordered content blocks (format-INDEPENDENT). Each block is
# (tag, payload); the tag fixes the role/spacing, the formatters supply the format.
# This is the ONE place that knows the per-type layout — render_telegram and
# render_rich are dumb formatters that walk these blocks.
_BLANK_BEFORE = {"para", "section", "usefulfor", "body"}  # plain-text spacing


def _layout(obj: dict) -> list[tuple]:
    ct = (obj.get("content_type") or "").lower()
    title = _esc(obj.get("title")) or "Reel"
    quote = (obj.get("quote") or "").strip()
    author = _esc(obj.get("author"))
    B: list[tuple] = []

    def hero_or_title() -> None:
        B.append(("hero", (_esc(quote), author)) if _ok(quote) else ("title", title))

    if ct in _QUOTE_TYPES:
        hero_or_title()
        if _ok(obj.get("context")):
            B.append(("para", _esc(obj["context"])))
    elif ct in _THOUGHT_TYPES:
        hero_or_title()
        if _ok(obj.get("main_thought")):
            B.append(("para", _esc(obj["main_thought"])))
        if _ok(obj.get("takeaway")):
            B.append(("takeaway", _esc(obj["takeaway"])))
    elif ct in _REC_TYPES:
        items = obj.get("items") or []
        types = {(it.get("type") or "").lower() for it in items}
        label = ("📚 <b>Concepts</b>" if types and types <= {"concept", "term", "law", "framework"}
                 else "📌 <b>Recommended</b>")
        B.append(("title", title))
        if _ok(quote):
            B.append(("sub", f"“{_esc(quote)}”"))
        B.append(("section", (label, "rec", items)))
    elif ct == "tutorial":
        B.append(("title", title))
        if _ok(quote):
            B.append(("sub", f"“{_esc(quote)}”"))
        steps = [s for s in (obj.get("steps") or []) if (s or "").strip()]
        if steps:
            B.append(("section", ("🪜 <b>How to</b>", "steps", steps)))
        if _ok(obj.get("useful_for")):
            B.append(("usefulfor", _esc(obj["useful_for"])))
    elif ct in _EDU_TYPES:
        B.append(("title", title))
        if _ok(obj.get("main_idea")):
            B.append(("sub", _esc(obj["main_idea"])))
        pts = [p for p in (obj.get("points") or []) if (p or "").strip()]
        if pts:
            B.append(("section", ("🔑 <b>Key points</b>", "bullets", pts)))
    else:
        B.append(("title", title))
        if _ok(obj.get("description")):
            B.append(("sub", _esc(obj["description"])))
        if [it for it in (obj.get("items") or []) if (it.get("name") or "").strip()]:
            B.append(("section", ("", "rec", obj.get("items") or [])))
        elif _ok(obj.get("summary")):
            B.append(("body", _esc(obj["summary"])))
    return B


def render_telegram(obj: dict, url: str = "") -> str:
    """Plain-sendMessage formatter: blank-line spacing, no <details>/transcript."""
    lines: list[str] = []
    for tag, payload in _layout(obj):
        if tag in _BLANK_BEFORE and lines:
            lines.append("")
        if tag == "hero":
            q, a = payload
            lines.append(f"“<b>{q}</b>”")
            if a:
                lines.append(f"— {a}")
        elif tag == "title":
            lines.append(f"<b>{payload}</b>")
        elif tag in ("sub", "para"):
            lines.append(f"<i>{payload}</i>")
        elif tag == "takeaway":
            lines.append(f"💡 <b>{payload}</b>")
        elif tag == "usefulfor":
            lines.append(f"<i>Useful for: {payload}</i>")
        elif tag == "body":
            lines.append(payload)
        elif tag == "section":
            header, kind, items = payload
            if header:
                lines.append(header)
            if kind == "rec":
                lines += _rec_lines(items) or ["<i>Not clear from the Reel.</i>"]
            elif kind == "bullets":
                lines += [f"• {_esc(p)}" for p in items]
            elif kind == "steps":
                lines += [f"{i}. {_esc(s)}" for i, s in enumerate(items, 1)]
    if _ok(obj.get("why_save")):
        lines += ["", f"💾 <i>{_esc(obj['why_save'])}</i>"]
    for x in (_link_line(url), _tags_line(obj)):
        if x:
            lines.append(x)
    return "\n".join(lines)


def render_rich(obj: dict, url: str = "", transcript: str = "") -> str:
    """Rich Message formatter: <br>-joined, real <ol>/<ul> lists, collapsible transcript.

    (Rich HTML collapses '\\n' to spaces, hence <br> + list blocks.)
    """
    blocks: list[str] = []
    for tag, payload in _layout(obj):
        if tag == "hero":
            q, a = payload
            blocks.append(f"“<b>{q}</b>”")
            if a:
                blocks.append(f"— {a}")
        elif tag == "title":
            blocks.append(f"<b>{payload}</b>")
        elif tag in ("sub", "para"):
            blocks.append(f"<i>{payload}</i>")
        elif tag == "takeaway":
            blocks.append(f"💡 <b>{payload}</b>")
        elif tag == "usefulfor":
            blocks.append(f"<i>Useful for: {payload}</i>")
        elif tag == "body":
            blocks.append(payload)
        elif tag == "section":
            header, kind, items = payload
            if header:
                blocks.append(header)
            if kind == "rec":
                li = _rec_lines(items) or ["<i>Not clear from the Reel.</i>"]
            elif kind == "bullets":
                li = [_esc(p) for p in items]
            else:  # steps
                li = [_esc(s) for s in items]
            wrap = "ul" if kind == "bullets" else "ol"
            blocks.append(f"<{wrap}>" + "".join(f"<li>{x}</li>" for x in li) + f"</{wrap}>")
    if _ok(obj.get("why_save")):
        blocks.append(f"💾 <i>{_esc(obj['why_save'])}</i>")
    for x in (_link_line(url), _tags_line(obj)):
        if x:
            blocks.append(x)
    body = "<br>".join(blocks)
    detail = _detail_text(obj, transcript)
    if detail:
        body += f"<details><summary>📄 Full transcript</summary>{detail}</details>"
    return body


def _bot_api(method: str, params: dict) -> dict:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}", data=data)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


async def _send_rich(chat_id: int, body_html: str) -> bool:
    """Send a Rich Message (HTML body). True on success."""
    params = {"chat_id": chat_id, "rich_message": json.dumps({"html": body_html}),
              "disable_web_page_preview": "true"}
    try:
        return bool((await asyncio.to_thread(_bot_api, "sendRichMessage", params)).get("ok"))
    except Exception as e:  # noqa: BLE001
        log.warning("sendRichMessage failed (%s) — falling back to HTML", e)
        return False


async def _send_rich_id(chat_id: int, body_html: str) -> int | None:
    """Send a Rich Message and return its message_id (for later edit-in-place)."""
    params = {"chat_id": chat_id, "rich_message": json.dumps({"html": body_html}),
              "disable_web_page_preview": "true"}
    try:
        r = await asyncio.to_thread(_bot_api, "sendRichMessage", params)
        return r.get("result", {}).get("message_id") if r.get("ok") else None
    except Exception as e:  # noqa: BLE001
        log.warning("sendRichMessage(id) failed (%s)", e)
        return None


async def _edit_rich(chat_id: int, message_id: int, body_html: str) -> bool:
    """Edit a Rich Message in place (editMessageText + rich_message)."""
    params = {"chat_id": chat_id, "message_id": message_id,
              "rich_message": json.dumps({"html": body_html}),
              "disable_web_page_preview": "true"}
    try:
        return bool((await asyncio.to_thread(_bot_api, "editMessageText", params)).get("ok"))
    except Exception as e:  # noqa: BLE001
        log.debug("editMessageText failed (%s)", e)
        return False


async def deliver(bot, chat_id: int, html_msg: str, rich_md: str | None) -> None:
    """Fresh send (no existing message to edit): rich if possible, else chunked HTML."""
    if rich_md and os.environ.get("RICH_MESSAGE", "1") == "1":
        if await _send_rich(chat_id, rich_md):
            return
    for part in chunked(html_msg):
        await bot.send_message(chat_id, part, parse_mode=ParseMode.HTML,
                               disable_web_page_preview=True)


async def process(bot, chat_id: int, url: str, force: bool) -> None:
    """One stable status message, edited in place at real milestones (no flaky drafts).

    Placeholder → "got transcript" → final note — a single message edited via
    editMessageText, so there's no 30s-draft expiry, flicker, or duplicate. The reel is
    marked pending for the duration; an interrupted run resumes on next startup.
    """
    norm = acquire.normalize_url(url)
    ledger.pending_add(norm, chat_id)
    mid = None
    try:
        rich = os.environ.get("RICH_MESSAGE", "1") == "1"
        mid = await _send_rich_id(chat_id, "<b>⏳ Working on your reel…</b>") if rich else None
        if mid is None:
            await bot.send_message(chat_id, f"⏳ Processing {url[:80]}…")

        async def progress(stage: str) -> None:
            if mid and stage == "acquired":
                await _edit_rich(chat_id, mid, "<b>✍️ Got it — writing your note…</b>")

        html_msg, rich_md = await run_pipeline(url, force=force, on_progress=progress)

        # Replace the placeholder in place (no orphaned "Working…" message).
        if mid and await _edit_rich(chat_id, mid, rich_md or html_msg):
            return
        await deliver(bot, chat_id, html_msg, rich_md)  # fresh-send fallback
    except Exception as e:  # noqa: BLE001
        # Anything unhandled must still close the loop — otherwise the user is
        # left watching "⏳ Working…" forever with no idea the reel died.
        log.error("processing failed for %s", url, exc_info=e)
        err = f"❌ That reel broke while processing:\n{type(e).__name__}: {e}\n\nSend /force to retry."
        if not (mid and await _edit_rich(chat_id, mid, html.escape(err))):
            try:
                await bot.send_message(chat_id, err)
            except Exception:  # noqa: BLE001
                pass
    finally:
        ledger.pending_remove(norm)


def _write_vault_note(obj: dict, url: str, transcript: str, date_iso: str) -> str:
    """Bot writes the durable vault note (markdown mirror of the saved note)."""
    d = PROJECT_DIR / "vault" / "Action Inbox"
    d.mkdir(parents=True, exist_ok=True)
    title = (obj.get("title") or "reel").strip()
    safe = re.sub(r"[^\w\- ]", "", title)[:60].strip() or "reel"
    today = date_iso
    fname = f"{today} {safe}.md"
    cats = ", ".join(obj.get("categories") or [])
    tags = " ".join("#" + str(t).strip().replace(" ", "_") for t in (obj.get("tags") or []) if t)
    author = (obj.get("author") or "").strip()
    quote = (obj.get("quote") or "").strip()
    L = ["---", f"source: {url}", f"date: {today}", "type: reel-note",
         f"content_type: {obj.get('content_type', '')}", f"kind: {obj.get('kind', '')}",
         f"categories: [{cats}]", "status: inbox", "---", "", f"# {title}", ""]
    if _ok(quote):
        L.append(f"> {quote}")
        if _ok(author):
            L.append(f"> — {author}")
        L.append("")

    def field(label: str, key: str) -> None:
        v = (obj.get(key) or "").strip()
        if _ok(v):
            L.append(f"**{label}:** {v}")

    field("Context", "context")
    field("Main idea", "main_idea")
    field("Main thought", "main_thought")
    field("Takeaway", "takeaway")
    field("Useful for", "useful_for")
    pts = [p for p in (obj.get("points") or []) if (p or "").strip()]
    if pts:
        L += ["", "## Key points"] + [f"- {p.strip()}" for p in pts]
    steps = [s for s in (obj.get("steps") or []) if (s or "").strip()]
    if steps:
        L += ["", "## Steps"] + [f"{i}. {s.strip()}" for i, s in enumerate(steps, 1)]
    items = obj.get("items") or []
    if items:
        L += ["", "## Recommended"]
        for it in items:
            name = (it.get("name") or "").strip()
            link = (it.get("link") or "").strip()
            au = (it.get("author") or "").strip()
            note = (it.get("verify_note") or "").strip()
            label = f"[{name}]({link})" if link.startswith(("http://", "https://")) else name
            tag = "✅" if it.get("verified") else "⚠️"
            line = f"- [ ] {tag} {label}" + (f" — {au}" if au else "")
            if not it.get("verified") and note:
                line += f" ({note})"
            L.append(line)
    if _ok(obj.get("why_save")):
        L += ["", f"**Why save:** {obj['why_save'].strip()}"]
    L += ["", f"**Original:** {url}"]
    if tags:
        L.append(tags)
    if obj.get("slides"):
        L += ["", "## Slides"]
        for i, s in enumerate(obj["slides"], 1):
            L.append(f"### Slide {i}")
            if (s.get("description") or "").strip():
                L.append(f"*{s['description'].strip()}*")
            if (s.get("text") or "").strip():
                L += ["", s["text"].strip()]
            L.append("")
    elif transcript.strip():
        L += ["", "## Transcript", "", transcript.strip(), ""]
    (d / fname).write_text("\n".join(L))
    return f"Action Inbox/{fname}"


def _sync_notion(obj: dict, media: dict, url: str, transcript: str, date_iso: str) -> str:
    res = notion.push_reel(
        obj,
        source_url=url,
        date_iso=date_iso,
        transcript=transcript,
        platform=media.get("platform", ""),
        author=obj.get("author") or media.get("author") or "",
    )
    if res["created"]:
        return f"🗂 Notion: 1 reel · {res['items']} items"
    return "🗂 Notion: failed"


def chunked(text: str, size: int = 4000):
    """Split on newline boundaries so HTML tags (always within one line) stay intact."""
    buf = ""
    for line in text.split("\n"):
        if len(buf) + len(line) + 1 > size and buf:
            yield buf
            buf = ""
        buf += (line + "\n")
    if buf.strip():
        yield buf


# --- Telegram handlers ---------------------------------------------------

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, ContextTypes, MessageHandler, CommandHandler, filters


def allowed(update: Update) -> bool:
    ids = os.environ.get("ALLOWED_USER_IDS", "").strip()
    if not ids:
        return True  # open until configured — set ALLOWED_USER_IDS in .env!
    return str(update.effective_user.id) in {x.strip() for x in ids.split(",")}


_last_url: dict[int, str] = {}  # per-user most recent link, for /force redo


async def on_start(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Send me a reel / video / article link and I'll turn it into actionable items.\n"
        "Send /force to redo the last one (replaces it, doesn't duplicate).\n"
        f"Your user id: {update.effective_user.id}"
    )


async def on_force(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        await update.message.reply_text("Not authorized.")
        return
    url = _last_url.get(update.effective_user.id)
    if not url:
        await update.message.reply_text("Nothing to redo yet — send me a reel link first.")
        return
    await update.message.reply_text(f"♻️ Redoing {url[:80]}…")
    await process(context.bot, update.effective_chat.id, url, force=True)


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        await update.message.reply_text("Not authorized.")
        return
    urls = URL_RE.findall(update.message.text or "")
    if not urls:
        await update.message.reply_text("Send me a link (Instagram reel, YouTube, TikTok, article).")
        return
    force = "/force" in (update.message.text or "")
    _last_url[update.effective_user.id] = urls[-1]
    for url in urls:
        log.info("processing %s (force=%s)", url, force)
        await process(context.bot, update.effective_chat.id, url, force=force)


async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log handler/network errors instead of leaving them unhandled."""
    log.error("handler error", exc_info=context.error)


MAX_RESUME_ATTEMPTS = 3


async def _heartbeat() -> None:
    """Touch a file every minute so the watchdog can tell alive from stuck."""
    HEARTBEAT.parent.mkdir(exist_ok=True)
    while True:
        try:
            HEARTBEAT.touch()
        except OSError:
            pass
        await asyncio.sleep(60)


async def _resume_pending(app) -> None:
    """On startup, re-process any reels that were interrupted mid-flight."""
    asyncio.create_task(_heartbeat())
    pend = ledger.pending_all()
    if not pend:
        return

    async def _go() -> None:
        for url, rec in list(pend.items()):
            chat_id = rec.get("chat_id")
            if not chat_id:
                ledger.pending_remove(url)
                continue
            # A reel that keeps killing the bot would otherwise be retried on
            # every startup forever, blocking real messages behind it.
            if ledger.pending_attempt(url) > MAX_RESUME_ATTEMPTS:
                ledger.pending_remove(url)
                log.warning("giving up on %s after %d attempts", url, MAX_RESUME_ATTEMPTS)
                try:
                    await app.bot.send_message(
                        chat_id, f"⚠️ Couldn't recover this one after several tries: {url}"
                    )
                except Exception:  # noqa: BLE001
                    pass
                continue
            log.info("resuming interrupted reel %s", url)
            try:
                await app.bot.send_message(chat_id, "↻ Recovering a reel that got interrupted earlier…")
                await process(app.bot, chat_id, url, force=True)
            except Exception as e:  # noqa: BLE001
                log.warning("resume failed for %s: %s", url, e)
                ledger.pending_remove(url)

    asyncio.create_task(_go())  # run after polling starts, don't block startup


def main() -> None:
    load_env()
    if len(sys.argv) >= 3 and sys.argv[1] == "--test":
        html_msg, rich_md = asyncio.run(run_pipeline(sys.argv[2], force=True))
        print("=== RICH HTML ===\n" + (rich_md or "(none)") + "\n\n=== PLAIN FALLBACK ===\n" + html_msg)
        return
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        sys.exit("TELEGRAM_BOT_TOKEN missing — copy .env.example to .env and fill it in.")
    app = Application.builder().token(token).post_init(_resume_pending).build()
    app.add_handler(CommandHandler("start", on_start))
    app.add_handler(CommandHandler("force", on_force))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    app.add_error_handler(_on_error)
    log.info("bot running (polling)")
    app.run_polling()


if __name__ == "__main__":
    main()
