#!/bin/bash
# Manage the reel-to-action launchd service.
# Usage: ./ctl.sh {install|start|stop|restart|status|logs|tail}
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
# Defaults to com.<your-username>.reel-to-action; override with SERVICE_LABEL.
LABEL="${SERVICE_LABEL:-com.$(id -un).reel-to-action}"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"
LOGDIR="$HERE/logs"

case "$1" in
  install)  "$HERE/install.sh" ;;
  start)    launchctl bootstrap "$DOMAIN" "$PLIST" && echo "started" ;;
  stop)     launchctl bootout "$DOMAIN/$LABEL" && echo "stopped" ;;
  restart)  launchctl kickstart -k "$DOMAIN/$LABEL" && echo "restarted" ;;
  status)   launchctl print "$DOMAIN/$LABEL" 2>/dev/null | grep -E "state =|pid =|last exit code =" | sed 's/^[[:space:]]*//' || echo "not loaded" ;;
  logs)     cat "$LOGDIR/bot.err.log" "$LOGDIR/bot.out.log" 2>/dev/null ;;
  tail)     tail -f "$LOGDIR/bot.err.log" ;;
  *)        echo "usage: $0 {install|start|stop|restart|status|logs|tail}" ; exit 1 ;;
esac
