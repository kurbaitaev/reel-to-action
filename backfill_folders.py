#!/usr/bin/env python3
"""One-off: file every existing note into a folder.

    python3 backfill_folders.py --dry-run     # classify and show, change nothing
    python3 backfill_folders.py               # apply: move files, update Notion

Notes written before folders existed sit flat in vault/Action Inbox. This reads
each one's title and description, asks the model which folder it belongs in
(batched, so it's a handful of calls rather than 166), then moves the file into
vault/<Folder>/ and sets the Notion Folder property.

Idempotent: notes already inside a folder directory are skipped, so it's safe to
re-run after adding new material.
"""

import argparse
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import folders  # noqa: E402
import notion  # noqa: E402

PROJ = pathlib.Path(__file__).resolve().parent
VAULT = PROJ / "vault"
INBOX = VAULT / "Action Inbox"
BATCH = 25


def load_env() -> None:
    f = PROJ / ".env"
    if not f.exists():
        return
    for line in f.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def read_note(p: pathlib.Path) -> dict:
    t = p.read_text()
    fm = re.search(r"^---\n(.*?)\n---", t, re.S)
    meta = {}
    if fm:
        for line in fm.group(1).splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                meta[k.strip()] = v.strip()
    m = re.search(r"^# (.+)$", t, re.M)
    # Older notes have no H1 — their title only exists in the filename, which is
    # "<date> <title> [hash].md". Classifying those on the blurb alone is worse.
    title = m.group(1).strip() if m else re.sub(
        r"^\d{4}-\d{2}-\d{2}\s*|\s*\[[0-9a-f]{6}\]$", "", p.stem).strip()
    body = re.sub(r"^---\n.*?\n---\n", "", t, flags=re.S)
    body = re.sub(r"^# .+$", "", body, flags=re.M).strip()
    return {
        "path": p,
        "source": meta.get("source", ""),
        "title": title,
        "categories": meta.get("categories", ""),
        # First couple of lines carry the description; enough to classify on.
        "blurb": " ".join(body.split())[:320],
        "has_folder": "folder" in meta,
    }


def classify(batch: list[dict]) -> dict:
    """Ask the model for one folder per note. Returns {index: folder}."""
    listing = "\n".join(
        f'{i}. TITLE: {n["title"]}\n   ABOUT: {n["blurb"]}\n   OLD TAGS: {n["categories"]}'
        for i, n in enumerate(batch))
    prompt = (
        folders.RULES
        + "\n\nFile each of the saved notes below into exactly one folder.\n"
          "Reply with ONLY a JSON object mapping the number to the folder name, "
          'e.g. {"0": "Motivation", "1": "Startup"}. No prose.\n\n' + listing
    )
    model = os.environ.get("CLAUDE_MODEL", "").strip()
    args = ["claude", "-p", prompt, "--max-turns", "3"]
    if model:
        args += ["--model", model]
    r = subprocess.run(args, capture_output=True, text=True, timeout=900)
    out = (r.stdout or "").strip()
    m = re.search(r"\{.*\}", out, re.S)
    if not m:
        raise RuntimeError(f"classifier returned no JSON: {out[:200] or r.stderr[:200]}")
    raw = json.loads(m.group(0))
    return {int(k): folders.normalize(v) for k, v in raw.items()}


def apply_note(n: dict, folder: str, dry: bool) -> str:
    dest_dir = VAULT / folders.safe_dirname(folder)
    dest = dest_dir / n["path"].name
    if dry:
        return f"{folder:<18} {n['title'][:52]}"
    dest_dir.mkdir(parents=True, exist_ok=True)
    text = n["path"].read_text()
    if "folder:" not in text.split("---")[1]:
        text = text.replace("---\n", f"---\nfolder: {folder}\n", 1)
    dest.write_text(text)
    if dest.resolve() != n["path"].resolve():
        n["path"].unlink()
    return f"{folder:<18} {n['title'][:52]}"


def update_notion(source_url: str, folder: str) -> bool:
    token = os.environ.get("NOTION_TOKEN", "").strip()
    db = os.environ.get("NOTION_DATABASE_ID", "").strip()
    if not (token and db and source_url):
        return False
    try:
        data = notion._api(f"https://api.notion.com/v1/databases/{db}/query", token, "POST",
                           {"filter": {"property": "Source", "url": {"equals": source_url}},
                            "page_size": 1})
        rows = data.get("results", [])
        if not rows:
            return False
        notion._api(f"https://api.notion.com/v1/pages/{rows[0]['id']}", token, "PATCH",
                    {"properties": {"Folder": {"select": {"name": folder}}}})
        return True
    except Exception as e:  # noqa: BLE001
        print(f"    notion update failed for {source_url[:60]}: {e}")
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    load_env()

    notes = [read_note(p) for p in sorted(INBOX.glob("*.md"))] if INBOX.exists() else []
    if args.limit:
        notes = notes[:args.limit]
    if not notes:
        print("nothing to file — Action Inbox is empty")
        return 0
    print(f"{len(notes)} note(s) to file\n")

    counts, done = {}, 0
    for start in range(0, len(notes), BATCH):
        batch = notes[start:start + BATCH]
        print(f"-- classifying {start + 1}-{start + len(batch)} …")
        try:
            mapping = classify(batch)
        except Exception as e:  # noqa: BLE001
            print(f"   batch failed ({e}); leaving these where they are")
            continue
        for i, n in enumerate(batch):
            folder = mapping.get(i) or folders.DEFAULT
            print("   " + apply_note(n, folder, args.dry_run))
            counts[folder] = counts.get(folder, 0) + 1
            done += 1
            if not args.dry_run and n["source"]:
                update_notion(n["source"], folder)

    print(f"\n{'would file' if args.dry_run else 'filed'} {done} note(s):")
    for f in folders.FOLDERS:
        if counts.get(f):
            print(f"  {counts[f]:>4}  {f}")
    if not args.dry_run and INBOX.exists() and not any(INBOX.iterdir()):
        shutil.rmtree(INBOX)
        print("\nAction Inbox is empty now — removed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
