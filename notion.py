#!/usr/bin/env python3
"""Notion sink: push extracted action items into a Notion database.

Uses an internal integration token (NOTION_TOKEN) + a database the integration
has been shared with (NOTION_DATABASE_ID). No-op if either is unset, so the bot
degrades gracefully when Notion isn't configured.

Item shape (produced by the agent as a JSON sidecar):
    {"name": str, "type": "watch|read|try|advice", "link": str,
     "why": str, "author": str}
"""

import json
import logging
import os
import urllib.request

import folders

log = logging.getLogger("reel-to-action.notion")

API = "https://api.notion.com/v1/pages"
VERSION = "2022-06-28"

# item type -> (checklist section heading) in display order
def enabled() -> bool:
    return bool(os.environ.get("NOTION_TOKEN") and os.environ.get("NOTION_DATABASE_ID"))


def _api(url: str, token: str, method: str = "GET", body: dict | None = None) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {token}", "Notion-Version": VERSION,
                 "Content-Type": "application/json"},
        method=method,
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def _has_source_url(token: str, db_id: str) -> bool:
    """Both lookups filter on Source being a url column. If it's been renamed or
    retyped, the query 400s — which used to silently break /force dedup (extra
    live pages) and date preservation (dates reset to today)."""
    if _db_props(token, db_id).get("Source") == "url":
        return True
    log.warning("no 'Source' url column in the Notion DB — dedup and date "
                "preservation are disabled. Add one to enable them.")
    return False


def existing_date(source_url: str) -> str | None:
    """Return the Date (YYYY-MM-DD) of the current live page for this reel, if any.

    Lets a re-process preserve the original 'added' date instead of resetting to today.
    """
    token = os.environ.get("NOTION_TOKEN", "").strip()
    db_id = os.environ.get("NOTION_DATABASE_ID", "").strip()
    if not (token and db_id and source_url) or not _has_source_url(token, db_id):
        return None
    try:
        data = _api(f"https://api.notion.com/v1/databases/{db_id}/query", token, "POST",
                    {"filter": {"property": "Source", "url": {"equals": source_url}}, "page_size": 1})
        rows = data.get("results", [])
        if rows:
            return ((rows[0]["properties"].get("Date", {}) or {}).get("date") or {}).get("start")
    except Exception as e:  # noqa: BLE001
        log.warning("existing_date query failed: %s", e)
    return None


def dedupe_by_source(source_url: str) -> int:
    """Keep only the newest page for a given reel Source; archive older ones.

    Used after a /force redo so reprocessing replaces rather than duplicates.
    """
    token = os.environ.get("NOTION_TOKEN", "").strip()
    db_id = os.environ.get("NOTION_DATABASE_ID", "").strip()
    if not (token and db_id and source_url) or not _has_source_url(token, db_id):
        return 0
    try:
        data = _api(f"https://api.notion.com/v1/databases/{db_id}/query", token, "POST",
                    {"filter": {"property": "Source", "url": {"equals": source_url}}})
    except Exception as e:  # noqa: BLE001
        log.warning("dedupe query failed: %s", e)
        return 0
    rows = sorted(data.get("results", []), key=lambda r: r.get("created_time", ""))
    archived = 0
    for r in rows[:-1]:  # keep the newest, archive the rest
        try:
            _api(f"https://api.notion.com/v1/pages/{r['id']}", token, "PATCH", {"archived": True})
            archived += 1
        except Exception as e:  # noqa: BLE001
            log.warning("archiving old page failed: %s", e)  # don't count it as archived
    return archived


def _post(payload: dict, token: str) -> tuple[bool, str, str]:
    """Create the page. Returns (ok, error, page_id)."""
    try:
        d = _api(API, token, "POST", payload)
        return True, "", d.get("id", "")
    except urllib.error.HTTPError as e:
        return False, f"{e.code} {e.read().decode()[:300]}", ""
    except Exception as e:  # noqa: BLE001
        return False, str(e), ""


def _append_blocks(page_id: str, blocks: list, token: str) -> None:
    """Notion accepts at most 100 blocks per call, so overflow is appended in
    batches. Without this the tail of a long note was silently dropped."""
    for i in range(0, len(blocks), 100):
        try:
            _api(f"https://api.notion.com/v1/blocks/{page_id}/children", token, "PATCH",
                 {"children": blocks[i:i + 100]})
        except Exception as e:  # noqa: BLE001
            log.warning("appending blocks %d+ failed: %s", i, e)
            return


def _prop_value(ptype: str, value) -> dict | None:
    """Build a property payload for the column's ACTUAL type.

    The database is hand-customized, so a column may be renamed *or* retyped.
    Matching on name alone and assuming a shape made Notion 400 the whole page
    — which lost the entire note while Telegram still looked fine.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    s = str(value)
    if ptype == "title":
        return {"title": [{"text": {"content": s[:2000]}}]}
    if ptype == "rich_text":
        return {"rich_text": [{"text": {"content": s[:2000]}}]}
    if ptype == "select":
        return {"select": {"name": s[:100]}}
    if ptype == "multi_select":
        vals = value if isinstance(value, list | tuple) else [value]
        return {"multi_select": [{"name": str(v)[:100]} for v in vals if str(v).strip()]}
    if ptype == "url":
        return {"url": s}
    if ptype == "date":
        return {"date": {"start": s}}
    if ptype == "number":
        try:
            return {"number": float(value) if not isinstance(value, bool) else None}
        except (TypeError, ValueError):
            return None
    if ptype == "checkbox":
        return {"checkbox": bool(value)}
    if ptype in ("email", "phone_number"):
        return {ptype: s}
    return None  # formula/rollup/relation/etc — not ours to set


def _heading(text: str) -> dict:
    return {"object": "block", "type": "heading_3",
            "heading_3": {"rich_text": [{"type": "text", "text": {"content": text[:2000]}}]}}


def _para(text: str, color: str = "default") -> dict:
    return {"object": "block", "type": "paragraph",
            "paragraph": {"rich_text": [{"type": "text", "text": {"content": text[:1900]}}]
                          if text else [], "color": color}}


def _todo(item: dict) -> dict:
    """Checklist line: ✅/⚠️ verify mark + linked title + grey verify note."""
    mark = "✅ " if item.get("verified") else "⚠️ "
    name = (item.get("name") or "").strip()[:1700]
    link = (item.get("link") or "").strip()
    title = {"type": "text", "text": {"content": mark + name}}
    if link.lower().startswith(("http://", "https://")):
        title["text"]["link"] = {"url": link}
    rich = [title]
    note = (item.get("verify_note") or "").strip()
    if note:
        rich.append({"type": "text", "text": {"content": f" — {note[:1700]}"},
                     "annotations": {"color": "gray"}})
    return {"object": "block", "type": "to_do", "to_do": {"rich_text": rich, "checked": False}}


def _toggle(label: str, text: str) -> dict:
    """A collapsible block; text is chunked into ≤1900-char paragraph children."""
    chunks = [text[i:i + 1900] for i in range(0, len(text), 1900)] or [""]
    return {"object": "block", "type": "toggle",
            "toggle": {"rich_text": [{"type": "text", "text": {"content": label}}],
                       "children": [_para(c) for c in chunks[:50]]}}


def _db_props(token: str, db_id: str) -> dict:
    """Return {property_name: type} for the database, so we only set columns that exist
    (the user customizes this DB; hard-coding column names breaks saves)."""
    try:
        d = _api(f"https://api.notion.com/v1/databases/{db_id}", token)
        return {k: v.get("type") for k, v in d.get("properties", {}).items()}
    except Exception as e:  # noqa: BLE001
        log.warning("could not read DB schema: %s", e)
        return {}


def _bullet(text: str) -> dict:
    return {"object": "block", "type": "bulleted_list_item",
            "bulleted_list_item": {"rich_text": [{"type": "text", "text": {"content": text[:1900]}}]}}


def _numbered(text: str) -> dict:
    return {"object": "block", "type": "numbered_list_item",
            "numbered_list_item": {"rich_text": [{"type": "text", "text": {"content": text[:1900]}}]}}


def _callout(text: str, emoji: str = "💾") -> dict:
    return {"object": "block", "type": "callout",
            "callout": {"icon": {"emoji": emoji},
                        "rich_text": [{"type": "text", "text": {"content": text[:1900]}}]}}


def _quote_text(text: str, author: str = "") -> dict:
    rich = [{"type": "text", "text": {"content": text[:1800]}}]
    if author:
        rich.append({"type": "text", "text": {"content": f" — {author[:200]}"},
                     "annotations": {"color": "gray", "italic": True}})
    return {"object": "block", "type": "quote", "quote": {"rich_text": rich}}


def _para_label(label: str, value: str) -> dict:
    return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": [
        {"type": "text", "text": {"content": f"{label}: "}, "annotations": {"bold": True}},
        {"type": "text", "text": {"content": value[:1800]}}]}}


_NA = {"", "not clear from the reel", "author not clear"}


def _ok(v) -> bool:
    return bool(v) and str(v).strip().lower() not in _NA


def push_reel(obj: dict, source_url: str, date_iso: str,
              transcript: str = "", platform: str = "", author: str = "") -> dict:
    """Create ONE Notion page from the content-type-aware note object.

    Schema-aware: only sets columns that currently exist (the user customizes the DB).
    Body adapts to the content: hero quote, labeled fields, key points, steps,
    recommendations, why-save, tags, and a transcript/slides toggle.
    """
    token = os.environ.get("NOTION_TOKEN", "").strip()
    db_id = os.environ.get("NOTION_DATABASE_ID", "").strip()
    if not (token and db_id):
        return {"created": 0, "items": 0, "error": "not configured"}

    items = [it for it in (obj.get("items") or []) if (it.get("name") or "").strip()]
    title = (obj.get("title") or "Reel").strip()
    description = (obj.get("description") or "").strip()
    summary = (obj.get("summary") or "").strip()
    quote = (obj.get("quote") or "").strip()
    author = (author or obj.get("author") or "").strip()
    cats =[c.strip() for c in (obj.get("categories") or []) if c and c.strip()]
    schema = _db_props(token, db_id)

    title_prop = next((n for n, t in schema.items() if t == "title"), "Name")
    props = {title_prop: {"title": [{"text": {"content": title[:2000]}}]}}

    def setp(name: str, value) -> None:
        """Set a column if it exists, formatted for whatever type it actually is."""
        ptype = schema.get(name)
        if not ptype or name == title_prop:
            return
        payload = _prop_value(ptype, value)
        if payload is not None:
            props[name] = payload

    setp("Source", source_url)
    setp("Folder", folders.normalize(obj.get("folder")))
    setp("Date", date_iso)
    setp("Items", len(items))
    if cats:
        setp("Category", cats)
    if _ok(author):
        setp("Author", author[:200])
    if platform:
        setp("Platform", platform)
    if description:
        setp("Summary", description)
        setp("Hook / key idea", description)
    if summary:
        setp("Takeaways", summary)

    children = []
    if _ok(quote):
        children.append(_quote_text(quote, author if _ok(author) else ""))
    for label, key in [("Context", "context"), ("Main idea", "main_idea"),
                       ("Main thought", "main_thought"), ("Takeaway", "takeaway"),
                       ("Useful for", "useful_for")]:
        v = (obj.get(key) or "").strip()
        if _ok(v):
            children.append(_para_label(label, v))
    pts = [p for p in (obj.get("points") or []) if (p or "").strip()]
    if pts:
        children.append(_heading("🔑 Key points"))
        children += [_bullet(p.strip()) for p in pts]
    steps = [s for s in (obj.get("steps") or []) if (s or "").strip()]
    if steps:
        children.append(_heading("🪜 Steps"))
        children += [_numbered(s.strip()) for s in steps]
    if items:
        children.append(_heading("📌 Recommended"))
        children += [_todo(it) for it in items]
    if _ok(obj.get("why_save")):
        children.append(_callout(obj["why_save"].strip(), "💾"))
    tags = [t for t in (obj.get("tags") or []) if t]
    if tags:
        children.append(_para(" ".join("#" + str(t).strip().replace(" ", "_") for t in tags), color="gray"))

    if obj.get("slides"):
        slide_text = "\n\n".join(
            f"Slide {i}" + (f" — {s.get('description', '')}" if s.get("description") else "")
            + (f"\n{s.get('text', '')}" if s.get("text") else "")
            for i, s in enumerate(obj["slides"], 1))
        children.append(_toggle("📄 Slides (verbatim text)", slide_text))
    elif transcript.strip():
        children.append(_toggle("📄 Transcript (verbatim)", transcript.strip()))

    ok, err, page_id = _post(
        {"parent": {"database_id": db_id}, "properties": props, "children": children[:100]},
        token,
    )
    if not ok:
        log.warning("notion reel page failed: %s", err)
        return {"created": 0, "items": len(items), "error": err}
    # Anything past Notion's 100-block limit goes in follow-up calls. The
    # transcript toggle lives at the end, so without this a long list reel
    # silently lost the verbatim record — the whole point of saving it.
    if len(children) > 100 and page_id:
        _append_blocks(page_id, children[100:], token)
    return {"created": 1, "items": len(items), "error": ""}
