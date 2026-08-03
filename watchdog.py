#!/usr/bin/env python3
"""Hourly health check for the reel-to-action bot (run by launchd).

Checks the bot is alive AND actively polling (via logs/heartbeat, touched every
60s by the bot). If it's down or stuck, restarts it and pings the user on
Telegram. Sends a visible "healthy" ping each run — the user asked for proof it's
actually checking. A network gap (Mac offline) is NOT treated as a failure — the
bot self-heals when connectivity returns, so the watchdog leaves it alone.
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
HEARTBEAT = PROJ / "logs" / "heartbeat"  # bot touches this every 60s while polling
WLOG = PROJ / "logs" / "watchdog.log"
STALE_SECONDS = 600  # >10 min without a heartbeat = stuck


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
# The OAuth *access* token expires hourly and Claude Code refreshes it silently on
# next use, so "expired" alone is not a broken login — alerting on it produced ~2
# false alarms a day. Only a login that stays expired this long is really dead.
AUTH_GRACE_S = 6 * 3600
_AUTH_SEEN = PROJ / "logs" / ".auth_expired_since"


_AUTH_FAILED = PROJ / "logs" / ".auth_failed"  # written by bot.py on a real 401


def claude_token_expired() -> bool:
    """True when the login genuinely needs an interactive `claude` → /login.

    Two signals, because neither alone is reliable:
      1. bot.py saw an actual authentication failure from the agent. This is
         ground truth and beats any guess from the keychain.
      2. The keychain has no usable session: expired (or expiresAt 0/absent,
         which is what a logged-out state looks like) AND no refresh token.
         An earlier version treated a falsy expiresAt as "no expiry set, so
         fine" — so a fully logged-out account read as healthy and three
         reels failed with no alert.
    """
    if _AUTH_FAILED.exists():
        return True
    try:
        r = subprocess.run(["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
                           capture_output=True, text=True, timeout=10)
        blob = json.loads(r.stdout.strip()).get("claudeAiOauth", {})
        exp = blob.get("expiresAt") or 0
        has_session = bool(exp) and (exp / 1000) >= time.time()
        if has_session:
            _AUTH_SEEN.unlink(missing_ok=True)
            return False
        if blob.get("refreshToken"):
            # Access tokens expire hourly and refresh silently; only alert if
            # that never happens over several checks.
            first = float(_AUTH_SEEN.read_text()) if _AUTH_SEEN.exists() else time.time()
            _AUTH_SEEN.write_text(str(first))
            return (time.time() - first) > AUTH_GRACE_S
        return True  # no valid session and nothing to refresh with
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
        _AUTH_FAILED.unlink(missing_ok=True)
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
    # No heartbeat file yet (fresh clone / just-upgraded bot) means "unknown", not
    # "stuck" — treating it as stuck would restart a healthy bot every cycle.
    stale = (time.time() - HEARTBEAT.stat().st_mtime) > STALE_SECONDS if HEARTBEAT.exists() else False

    if not running:
        restart()
        time.sleep(6)
        ok = bot_running()
        wlog(f"DOWN -> restarted, now running={ok}")
        if online:
            alert(token, chat, "⚠️ The reel bot was down — restarted it. ✅ Back up."
                  if ok else "🚨 The reel bot is down and the restart FAILED — needs a look.")
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
