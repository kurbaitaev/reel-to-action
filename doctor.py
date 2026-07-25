#!/usr/bin/env python3
"""Check whether this machine can actually run the bot, and say what's missing.

    python3 doctor.py

Every line is either OK, MISSING (must fix), or OPTIONAL (works without it,
but you lose something specific).
"""

import os
import pathlib
import shutil
import subprocess
import sys

PROJ = pathlib.Path(__file__).resolve().parent
OK, BAD, OPT = "\033[32mOK\033[0m", "\033[31mMISSING\033[0m", "\033[33mOPTIONAL\033[0m"
problems = 0


def say(status: str, name: str, detail: str = "") -> None:
    print(f"  [{status}] {name}" + (f" — {detail}" if detail else ""))


def need(cond: bool, name: str, fix: str, detail: str = "") -> None:
    global problems
    if cond:
        say(OK, name, detail)
    else:
        say(BAD, name, fix)
        problems += 1


def optional(cond: bool, name: str, without: str, detail: str = "") -> None:
    say(OK if cond else OPT, name, detail if cond else f"without it: {without}")


def env(key: str) -> str:
    return os.environ.get(key, "").strip()


def load_env() -> None:
    f = PROJ / ".env"
    if not f.exists():
        return
    for line in f.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def ytdlp_version() -> str:
    try:
        return subprocess.run(["yt-dlp", "--version"], capture_output=True, text=True,
                              timeout=20).stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


def main() -> int:
    global problems
    load_env()
    print("\nRequired\n")
    need(sys.version_info >= (3, 10), f"python {sys.version_info.major}.{sys.version_info.minor}",
         "need Python 3.10+ (brew install python)")
    try:
        import telegram  # noqa: F401
        need(True, "python-telegram-bot", "", "installed")
    except ImportError:
        need(False, "python-telegram-bot", "pip install -r requirements.txt")
    need(bool(shutil.which("claude")), "claude CLI",
         "install Claude Code: https://claude.com/product/claude-code")
    need(bool(env("TELEGRAM_BOT_TOKEN")), "TELEGRAM_BOT_TOKEN",
         "get one from @BotFather on Telegram, put it in .env")
    need(bool(env("ALLOWED_USER_IDS")), "ALLOWED_USER_IDS",
         "send /start to your bot to learn your id, then put it in .env "
         "(without it the bot refuses everyone)")

    # Either an API key or a logged-in CLI is needed for the agent to run.
    has_key = bool(env("ANTHROPIC_API_KEY"))
    logged_in = False
    if not has_key:
        try:
            r = subprocess.run(["security", "find-generic-password", "-s",
                                "Claude Code-credentials", "-w"],
                               capture_output=True, text=True, timeout=10)
            logged_in = bool(r.stdout.strip())
        except Exception:  # noqa: BLE001
            logged_in = False
    need(has_key or logged_in, "Claude auth",
         "either run `claude` then /login, or set ANTHROPIC_API_KEY in .env",
         "ANTHROPIC_API_KEY" if has_key else "Claude Code login")

    print("\nInstagram\n")
    ver = ytdlp_version()
    # The Instagram extractor was reworked 2026-06-28; older builds fail with
    # "empty media response" on public reels.
    fresh = ver >= "2026.07.04"
    if not ver:
        need(False, "yt-dlp", "brew install yt-dlp  (needed unless you use Apify)")
    elif fresh:
        say(OK, f"yt-dlp {ver}", "Instagram works without an account")
    else:
        say(BAD, f"yt-dlp {ver}", "too old for Instagram — run: brew upgrade yt-dlp")
        problems += 1
    optional(bool(env("APIFY_TOKEN")), "APIFY_TOKEN (paid)",
             "no speech transcript — notes rely on the caption and on-screen text",
             "reels get a spoken transcript")

    print("\nOptional\n")
    optional(bool(shutil.which("ffmpeg")) and bool(shutil.which("ffprobe")), "ffmpeg",
             "no video frames, so text shown on screen is missed", "frame sampling works")
    optional(bool(env("NOTION_TOKEN") and env("NOTION_DATABASE_ID")), "Notion",
             "notes only go to the local vault + Telegram", "notes sync to Notion")

    print()
    if problems:
        print(f"{problems} thing(s) to fix above, then re-run: python3 doctor.py\n")
        return 1
    print("All good — start it with:  python3 bot.py   (or ./install.sh to run it always)\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
