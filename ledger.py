#!/usr/bin/env python3
"""Tiny JSON ledger so processed links are remembered (dedup + resume).

One file, keyed by URL. Good enough for single-user; swap for Postgres
in the multi-user phase.
"""

import json
import pathlib
import threading

_LOCK = threading.Lock()
_PATH = pathlib.Path(__file__).resolve().parent / "ledger.json"


def _load_file(path: pathlib.Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def _load() -> dict:
    return _load_file(_PATH)


def get(url: str) -> dict | None:
    return _load().get(url)


def put(url: str, record: dict) -> None:
    with _LOCK:
        data = _load()
        data[url] = record
        _PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False))


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
        d[url] = {"chat_id": chat_id}
        _PENDING.write_text(json.dumps(d, indent=2, ensure_ascii=False))


def pending_remove(url: str) -> None:
    with _LOCK:
        d = _load_pending()
        if d.pop(url, None) is not None:
            _PENDING.write_text(json.dumps(d, indent=2, ensure_ascii=False))


def pending_all() -> dict:
    return _load_pending()
