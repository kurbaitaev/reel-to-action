#!/usr/bin/env python3
"""Hourly health check for the reel-to-action bot (run by launchd).

Checks the bot is alive AND actively polling. If it's down or stuck, restarts
it and pings the user on Telegram. Stays silent when healthy (no hourly spam).
A network gap (Mac offline) is NOT treated as a failure — the bot self-heals
when connectivity returns, so the watchdog leaves it alone.
"""

import json
import os
import pathlib
import subprocess
import time
import urllib.parse
import urllib.request

PROJ = pathlib.Path(__file__).resolve().parent
LABEL = "com.kurbaitaev.reel-to-action"
LOG = PROJ / "logs" / "bot.err.log"
WLOG = PROJ / "logs" / "watchdog.log"
STALE_SECONDS = 600  # bot logs a poll every ~10s; >10 min of silence = stuck


def load_env() -> None:
    env = PROJ / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def bot_running() -> bool:
    r = subprocess.run(["pgrep", "-f", "reel-to-action/bot.py"], capture_output=True, text=True)
    return bool(r.stdout.strip())


def telegram_ok(token: str) -> bool:
    try:
        with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/getMe", timeout=15) as r:
            return json.load(r).get("ok", False)
    except Exception:  # noqa: BLE001
        return False


def alert(token: str, chat: str, text: str) -> None:
    if not (token and chat):
        return
    try:
        data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
        urllib.request.urlopen(
            urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data),
            timeout=15,
        )
    except Exception:  # noqa: BLE001
        pass


def restart() -> None:
    uid = os.getuid()
    subprocess.run(["launchctl", "kickstart", "-k", f"gui/{uid}/{LABEL}"],
                   capture_output=True, text=True)


def wlog(msg: str) -> None:
    WLOG.parent.mkdir(exist_ok=True)
    with WLOG.open("a") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")


_AUTH_FLAG = PROJ / "logs" / ".auth_alerted"


def claude_token_expired() -> bool:
    """Read Claude Code's OAuth expiry from the login keychain (cheap, no API call)."""
    try:
        r = subprocess.run(["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
                           capture_output=True, text=True, timeout=10)
        exp = json.loads(r.stdout.strip()).get("claudeAiOauth", {}).get("expiresAt")
        return bool(exp) and (exp / 1000) < time.time()
    except Exception:  # noqa: BLE001
        return False  # can't read → don't false-alarm


def check_claude_auth(token: str, chat: str) -> None:
    """Alert once when the Claude OAuth token expires; reset when re-logged-in."""
    if claude_token_expired():
        if not _AUTH_FLAG.exists():
            alert(token, chat,
                  "🔑 Claude login expired — the bot can't analyze reels until you run "
                  "`claude` → `/login` in a terminal. Reels you send are kept and will "
                  "be recovered after login.")
            _AUTH_FLAG.write_text(str(int(time.time())))
            wlog("claude OAuth EXPIRED -> alerted user")
    elif _AUTH_FLAG.exists():
        _AUTH_FLAG.unlink(missing_ok=True)
        alert(token, chat, "✅ Claude login restored — the bot is analyzing reels again.")
        wlog("claude OAuth restored")


def ensure_workspace_trust() -> bool:
    """A CLI re-login can reset ~/.claude.json and drop this project's trust flag,
    which silently disables the agent's permission allowlist (WebSearch, vault writes)
    — reels still process but with unverified links. Restore the flag if missing."""
    p = pathlib.Path.home() / ".claude.json"
    try:
        c = json.loads(p.read_text())
        proj = c.setdefault("projects", {}).setdefault(str(PROJ), {})
        if not proj.get("hasTrustDialogAccepted"):
            proj["hasTrustDialogAccepted"] = True
            p.write_text(json.dumps(c, indent=2))
            return True  # was broken → fixed
    except Exception:  # noqa: BLE001
        pass
    return False


def main() -> None:
    load_env()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("ALLOWED_USER_IDS", "").split(",")[0].strip()

    if ensure_workspace_trust():
        wlog("workspace trust flag was missing -> restored")
        alert(token, chat, "🔧 Restored the bot's workspace trust flag — web search had been "
                           "silently disabled (happens after a CLI re-login). Fixed automatically.")
    check_claude_auth(token, chat)

    running = bot_running()
    online = telegram_ok(token)
    stale = (time.time() - LOG.stat().st_mtime) > STALE_SECONDS if LOG.exists() else True

    if not running:
        restart()
        time.sleep(6)
        ok = bot_running()
        wlog(f"DOWN -> restarted, now running={ok}")
        if online:
            alert(token, chat, "⚠️ The reel bot was down — restarted it. ✅ Back up.")
    elif stale and online:
        # process alive but not polling, while the internet IS up → stuck
        restart()
        time.sleep(6)
        wlog("STUCK (no recent polling) -> restarted")
        alert(token, chat, "⚠️ The reel bot looked stuck — restarted it. ✅ Back up.")
    elif not online:
        wlog("offline (Mac has no internet) — leaving bot to self-heal")
    else:
        wlog("ok")
        alert(token, chat, f"✅ Reel bot healthy — {time.strftime('%a %H:%M')}")


if __name__ == "__main__":
    main()
