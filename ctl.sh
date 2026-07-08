#!/bin/bash
# Manage the reel-to-action launchd service.
# Usage: ./ctl.sh {start|stop|restart|status|logs|tail}
set -e
LABEL="com.kurbaitaev.reel-to-action"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"
LOGDIR="$(cd "$(dirname "$0")" && pwd)/logs"

case "$1" in
  start)    launchctl bootstrap "$DOMAIN" "$PLIST" && echo "started" ;;
  stop)     launchctl bootout "$DOMAIN/$LABEL" && echo "stopped" ;;
  restart)  launchctl kickstart -k "$DOMAIN/$LABEL" && echo "restarted" ;;
  status)   launchctl print "$DOMAIN/$LABEL" 2>/dev/null | grep -E "state =|pid =|last exit code =" | sed 's/^[[:space:]]*//' || echo "not loaded" ;;
  logs)     cat "$LOGDIR/bot.err.log" "$LOGDIR/bot.out.log" 2>/dev/null ;;
  tail)     tail -f "$LOGDIR/bot.err.log" ;;
  *)        echo "usage: $0 {start|stop|restart|status|logs|tail}" ; exit 1 ;;
esac
