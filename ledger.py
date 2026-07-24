#!/usr/bin/env python3
"""Tiny JSON ledger so processed links are remembered (dedup + resume).

One file, keyed by URL. Good enough for single-user; swap for Postgres
in the multi-user phase.
"""

import json
import logging
import os
import pathlib
import threading
import time

_LOCK = threading.Lock()
_PATH = pathlib.Path(__file__).resolve().parent / "ledger.json"
log = logging.getLogger("reel-to-action.ledger")


def _load_file(path: pathlib.Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            # Never silently start from scratch — that would re-process every
            # reel and duplicate it everywhere. Keep the damaged file for repair.
            backup = path.with_name(f"{path.stem}.corrupt-{int(time.time())}.json")
            try:
                path.rename(backup)
            except OSError:
                pass
            log.error("%s was corrupt — saved as %s and starting empty", path.name, backup.name)
            return {}
    return {}


def _save_file(path: pathlib.Path, data: dict) -> None:
    """Atomic write: a crash mid-save can never truncate the real file."""
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    os.replace(tmp, path)


def _load() -> dict:
    return _load_file(_PATH)


def get(url: str) -> dict | None:
    return _load().get(url)


def put(url: str, record: dict) -> None:
    with _LOCK:
        data = _load()
        data[url] = record
        _save_file(_PATH, data)


# --- pending recovery -----------------------------------------------------
# A reel is marked pending while it's being processed. If the bot is killed
# mid-reel (restart / sleep / crash), the entry survives and is resumed on
# the next startup — so reels are never silently dropped.
_PENDING = pathlib.Path(__file__).resolve().parent / "pending.json"


def _load_pending() -> dict:
    return _load_file(_PENDING)


def pending_add(url: str, chat_id: int) -> None:
    with _LOCK:
        d = _load_pending()
        rec = d.get(url) or {}
        d[url] = {"chat_id": chat_id, "attempts": rec.get("attempts", 0)}
        _save_file(_PENDING, d)


def pending_attempt(url: str) -> int:
    """Count a recovery attempt. Lets the resumer give up on a poison reel."""
    with _LOCK:
        d = _load_pending()
        if url not in d:
            return 0
        n = d[url].get("attempts", 0) + 1
        d[url]["attempts"] = n
        _save_file(_PENDING, d)
        return n


def pending_remove(url: str) -> None:
    with _LOCK:
        d = _load_pending()
        if d.pop(url, None) is not None:
            _save_file(_PENDING, d)


def pending_all() -> dict:
    return _load_pending()
