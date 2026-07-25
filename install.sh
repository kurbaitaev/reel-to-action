#!/bin/bash
# Install reel-to-action as a pair of launchd services (macOS): the bot itself,
# plus a watchdog that restarts it if it dies. Safe to re-run — it reinstalls.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
LABEL="${SERVICE_LABEL:-com.$(id -un).reel-to-action}"
AGENTS="$HOME/Library/LaunchAgents"
DOMAIN="gui/$(id -u)"
PYTHON="${PYTHON:-$(command -v python3)}"

if [ -z "$PYTHON" ]; then
  echo "error: python3 not found. Install it (brew install python) and re-run." >&2
  exit 1
fi

if [ ! -f "$HERE/.env" ]; then
  echo "error: no .env yet. Run:  cp .env.example .env  then fill in your bot token." >&2
  exit 1
fi

mkdir -p "$HERE/logs" "$AGENTS"

render() {  # template -> plist
  sed -e "s|__LABEL__|$LABEL|g" \
      -e "s|__PYTHON__|$PYTHON|g" \
      -e "s|__DIR__|$HERE|g" \
      -e "s|__HOME__|$HOME|g" "$1"
}

for pair in "bot.plist.template:$LABEL" "watchdog.plist.template:$LABEL-watchdog"; do
  tpl="${pair%%:*}"; label="${pair##*:}"
  render "$HERE/launchd/$tpl" > "$AGENTS/$label.plist"
  launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
  launchctl bootstrap "$DOMAIN" "$AGENTS/$label.plist"
  echo "installed + started: $label"
done

echo
echo "Done. The bot starts at login and restarts if it crashes."
echo "  ./ctl.sh status    # check it"
echo "  ./ctl.sh tail      # watch the log"
echo
echo "Note: this is a LaunchAgent, so it only runs while you're logged in and"
echo "the Mac is awake. That's required — the Claude CLI reads its login from"
echo "your login keychain."
