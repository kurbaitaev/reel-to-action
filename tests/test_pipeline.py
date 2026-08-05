#!/usr/bin/env python3
"""Regression tests. Every case here is a bug that actually happened.

    python3 -m pytest tests/ -q          (or: python3 tests/test_pipeline.py)

No network, no Apify, no Telegram — Apify calls are stubbed.
"""

import json
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import acquire  # noqa: E402
import agent_openai  # noqa: E402
import bot  # noqa: E402
import folders  # noqa: E402
import ledger  # noqa: E402
import notion  # noqa: E402


# --- agent output --------------------------------------------------------
# The agent is a model, so its JSON shape is a request, not a guarantee. These
# shapes used to raise before any sink ran, leaving a permanent "Working..."
# placeholder and an unrecoverable reel.

def test_sanitize_survives_wrong_types():
    for bad in [
        {"items": ["a string, not a dict"]},
        {"points": [{"unexpected": "dict"}]},
        {"tags": "string-not-list", "steps": None, "slides": "nope"},
        {"items": [{"name": "ok"}, "junk", 42, None]},
        {"categories": 5, "points": [None, "", "keep"]},
    ]:
        obj = bot._sanitize(dict(bad))
        assert isinstance(obj["items"], list)
        assert all(isinstance(i, dict) for i in obj["items"])
        # must render without raising
        bot.render_telegram(obj, "https://instagram.com/reel/X/")
        bot.render_rich(obj, "https://instagram.com/reel/X/", "transcript")


def test_search_urls_never_count_as_verified():
    obj = bot._sanitize({"items": [
        {"name": "A", "link": "https://www.google.com/search?q=a", "verified": True},
        {"name": "B", "link": "https://duckduckgo.com/?q=b", "verified": True},
        {"name": "C", "link": "not-a-url", "verified": True},
        {"name": "D", "link": "https://www.goodreads.com/book/show/1", "verified": True},
    ]})
    assert bot._validate_links(obj) == 3
    assert [i["verified"] for i in obj["items"]] == [False, False, False, True]


# --- Telegram delivery ---------------------------------------------------

def test_blocks_use_native_structure_and_no_raw_html():
    obj = {
        "content_type": "book_recommendation",
        "title": "Books & Ideas",              # & must not arrive as &amp;
        "quote": "a quote",
        "author": "Someone",
        "items": [{"name": "Range", "author": "David Epstein",
                   "link": "https://www.goodreads.com/book/show/41795733-range",
                   "verified": True}],
        "why_save": "worth keeping",
        "tags": ["books", "investing"],
    }
    payload = bot.render_blocks(obj, "https://instagram.com/reel/X/", "line one\nline two")
    types = [b["type"] for b in payload["blocks"]]
    for expected in ("heading", "list", "blockquote", "divider", "details", "footer"):
        assert expected in types, f"missing {expected} block"

    flat = json.dumps(payload)
    # _layout() escapes for the HTML renderers; blocks must carry raw text.
    assert "&amp;" not in flat and "<b>" not in flat, "HTML leaked into native blocks"

    # A verified item must be a real link, and list items wrap blocks (not text).
    lst = next(b for b in payload["blocks"] if b["type"] == "list")
    assert "blocks" in lst["items"][0]
    assert any(p.get("type") == "url" for p in json.loads(flat)["blocks"][
        types.index("list")]["items"][0]["blocks"][0]["text"] if isinstance(p, dict))

    details = next(b for b in payload["blocks"] if b["type"] == "details")
    assert details["summary"] and details["blocks"], "details needs summary + blocks"


def test_long_line_is_split_under_the_limit():
    # A single long line used to exceed Telegram's 4096 cap → BadRequest → lost reel.
    for text in ["x" * 9000, "short\n" + "y" * 9000 + "\nend", "a\nb\nc"]:
        for chunk in bot.chunked(text):
            assert len(chunk) <= 4000


# --- ledger --------------------------------------------------------------

def test_ledger_writes_atomically_and_keeps_corrupt_files():
    tmp = pathlib.Path(tempfile.mkdtemp())
    ledger._PATH, ledger._PENDING = tmp / "ledger.json", tmp / "pending.json"

    ledger.put("u1", {"status": "done"})
    assert ledger.get("u1") == {"status": "done"}
    assert not list(tmp.glob("*.tmp")), "temp file left behind"

    # A crash mid-write used to leave truncated JSON, which was silently read as
    # {} — re-processing and duplicating every reel.
    (tmp / "ledger.json").write_text('{"u1": {"status": "do')
    assert ledger.get("u1") is None
    assert list(tmp.glob("*corrupt*")), "corrupt ledger was not preserved"
    ledger.put("u2", {"status": "done"})
    assert ledger.get("u2") is not None


def test_pending_attempts_give_up():
    tmp = pathlib.Path(tempfile.mkdtemp())
    ledger._PATH, ledger._PENDING = tmp / "ledger.json", tmp / "pending.json"
    ledger.pending_add("p1", 42)
    assert [ledger.pending_attempt("p1") for _ in range(3)] == [1, 2, 3]
    ledger.pending_remove("p1")
    assert ledger.pending_all() == {}
    assert ledger.pending_attempt("gone") == 0


# --- url normalization ---------------------------------------------------

def test_all_instagram_link_forms_collapse_to_one_key():
    forms = [
        "https://www.instagram.com/reel/ABC/",
        "https://www.instagram.com/reels/ABC/",
        "https://instagram.com/reel/ABC/?igsh=xyz",
        "https://www.instagram.com/share/reel/ABC/",
    ]
    assert len({acquire.normalize_url(u) for u in forms}) == 1


def test_tracking_params_stripped_but_real_params_kept():
    assert acquire.normalize_url(
        "https://www.threads.com/@u/post/X?xmt=AQ&slof=1") == "https://www.threads.com/@u/post/X"
    # unknown hosts are left alone
    assert acquire.normalize_url("https://example.com/a?q=keep") == "https://example.com/a?q=keep"


# --- acquisition branches ------------------------------------------------

def _stub_apify(item):
    """Stub both Apify calls: transcriber finds nothing, scraper returns `item`.
    A token must be present or acquire() takes the yt-dlp path and hits network."""
    import os
    os.environ["APIFY_TOKEN"] = "test-token"

    def fake_run(actor, payload, token, timeout=300):
        return [] if "transcripts" in actor else [item]
    acquire._apify_run = fake_run
    acquire._download = lambda src, dest, timeout=60: pathlib.Path(dest).write_bytes(b"jpg")
    acquire.video_frames = lambda *a, **k: []


def test_single_photo_post_is_not_dropped():
    # Single images used to fall through to the video branch and be lost entirely.
    _stub_apify({"type": "Image", "caption": "c", "ownerUsername": "u",
                 "shortCode": "P1", "displayUrl": "https://x/i.jpg"})
    m = acquire.acquire("https://www.instagram.com/p/P1/")
    assert m["kind"] == "image" and len(m["images"]) == 1
    acquire.cleanup(m)


def test_carousel_downloads_every_slide_with_unique_names():
    _stub_apify({"type": "Sidecar", "caption": "c", "ownerUsername": "u", "shortCode": "C1",
                 "childPosts": [{"displayUrl": f"https://x/{i}.jpg"} for i in range(5)]})
    m = acquire.acquire("https://www.instagram.com/p/C1/")
    assert m["kind"] == "carousel" and len(m["images"]) == 5
    assert len(set(m["images"])) == 5, "slide filenames collided"
    acquire.cleanup(m)
    assert not [p for p in acquire.TMP.glob("C1*")], "media not cleaned up"


def test_tweet_urls_normalize_and_are_detected():
    for u in ["https://twitter.com/NASA/status/123",
              "https://x.com/NASA/status/123?s=20&t=abc",
              "https://x.com/NASA/status/123"]:
        assert acquire.is_twitter(u)
        assert acquire.normalize_url(u) == "https://x.com/NASA/status/123"
    assert not acquire.is_twitter("https://x.com/NASA")  # profile, not a post


def test_text_only_tweet_still_produces_a_note():
    """yt-dlp refuses tweets without video ("No video could be found"), which
    lost the note entirely. The Apify path must handle text and photo tweets."""
    import os
    os.environ["APIFY_TOKEN"] = "test-token"
    acquire._apify_run = lambda actor, payload, token, timeout=300: [{
        "fullText": "a tweet with no video at all",
        "author": {"userName": "someone", "name": "Some One"},
        "lang": "en",
        "extendedEntities": {"media": [
            {"type": "photo", "media_url_https": "https://pbs.twimg.com/media/x.jpg"}]},
    }]
    acquire._download = lambda src, dest, timeout=60: pathlib.Path(dest).write_bytes(b"jpg")
    m = acquire.acquire("https://x.com/someone/status/999")
    assert m["platform"] == "twitter"
    assert m["kind"] == "image" and len(m["images"]) == 1
    assert m["author"] == "someone"
    assert "no video" in m["caption"]
    acquire.cleanup(m)


# --- folders -------------------------------------------------------------

def test_every_note_gets_exactly_one_valid_folder():
    # The folder decides where the note is filed, so it can never be blank or
    # something the model invented.
    for bad in [{}, {"folder": ""}, {"folder": None}, {"folder": "Nonsense"},
                {"folder": 42}, {"folder": ["Startup"]}]:
        obj = bot._sanitize(dict(bad))
        assert obj["folder"] in folders.FOLDERS, f"{bad} -> {obj['folder']}"


def test_folder_normalizer_tolerates_near_misses():
    cases = {
        "Startup": "Startup", "startup tips": "Startup", "Fundraising": "Startup",
        "AI Tools": "Tools & AI", "tooling": "Tools & AI",
        "content": "Content Ideas", "Content Ideas": "Content Ideas",
        "motivation": "Motivation", "quotes": "Motivation",
        "Learning & Self": "Learning & Self",
    }
    for given, want in cases.items():
        assert folders.normalize(given) == want, f"{given} -> {folders.normalize(given)}"


def test_folder_directory_name_matches_the_notion_value():
    """They must be the same string. When the disk said "Tools and AI" and
    Notion said "Tools & AI", it read as two different folders."""
    for f in folders.FOLDERS:
        d = folders.safe_dirname(f)
        assert d == f, f"disk name {d!r} differs from Notion value {f!r}"
        assert "/" not in d, f"{d!r} would nest directories"


# --- openai backend ------------------------------------------------------

def test_openai_schema_obeys_strict_mode():
    """OpenAI strict mode rejects the whole request unless every object lists
    all its properties in `required` and sets additionalProperties: false."""
    def check(node, path="root"):
        if node.get("type") == "object":
            assert node.get("additionalProperties") is False, f"{path}: additionalProperties"
            props, req = set(node.get("properties", {})), set(node.get("required", []))
            assert props == req, f"{path}: required != properties ({props ^ req})"
            for k, v in node["properties"].items():
                check(v, f"{path}.{k}")
        elif node.get("type") == "array":
            check(node["items"], path + "[]")

    check(agent_openai.SCHEMA)
    # The renderers, Notion sink and vault writer all read these.
    for field in ("title", "content_type", "items", "points", "steps", "slides",
                  "tags", "categories", "description", "summary", "why_save", "quote"):
        assert field in agent_openai.SCHEMA["properties"], f"schema is missing {field}"


def test_openai_backend_is_off_without_a_key():
    import os
    saved = os.environ.pop("OPENAI_API_KEY", None)
    try:
        assert agent_openai.enabled() is False
    finally:
        if saved:
            os.environ["OPENAI_API_KEY"] = saved


# --- notion --------------------------------------------------------------

def test_property_values_match_the_columns_actual_type():
    # Matching on name and assuming a shape made Notion 400 the whole page,
    # losing the note while Telegram still looked fine.
    assert notion._prop_value("url", "https://x/") == {"url": "https://x/"}
    assert notion._prop_value("rich_text", "https://x/")["rich_text"][0]["text"]["content"]
    assert notion._prop_value("select", "instagram") == {"select": {"name": "instagram"}}
    assert len(notion._prop_value("multi_select", ["A", "B"])["multi_select"]) == 2
    assert notion._prop_value("date", "2026-07-24") == {"date": {"start": "2026-07-24"}}
    assert notion._prop_value("checkbox", True) == {"checkbox": True}
    # unsettable / empty
    assert notion._prop_value("formula", "x") is None
    assert notion._prop_value("rich_text", "") is None
    assert notion._prop_value("url", None) is None


def test_unknown_author_sentinel_is_rejected():
    assert not notion._ok("Author not clear")
    assert not notion._ok("Not clear from the Reel")
    assert not notion._ok("")
    assert notion._ok("Bill Gurley")


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
            except Exception as e:  # noqa: BLE001
                failed += 1
                print(f"  FAIL  {name}: {type(e).__name__}: {e}")
    print(f"\n{'FAILED' if failed else 'all passed'} ({failed} failure(s))")
    sys.exit(1 if failed else 0)
