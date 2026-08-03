#!/bin/bash
# Install reel-to-action as a systemd user service on a Linux VPS.
# Run as the normal (non-root) user that owns the checkout:
#   ./deploy/install-linux.sh
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="${PYTHON:-$(command -v python3)}"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT="reel-to-action.service"

if [ ! -f "$HERE/.env" ]; then
  echo "error: no .env yet. Run:  cp .env.example .env  then fill it in." >&2
  exit 1
fi

if ! command -v systemctl >/dev/null; then
  echo "error: systemd not found. On macOS use ./install.sh instead." >&2
  exit 1
fi

mkdir -p "$HERE/logs" "$UNIT_DIR"

sed -e "s|__DIR__|$HERE|g" -e "s|__PYTHON__|$PYTHON|g" \
    "$HERE/deploy/$UNIT" > "$UNIT_DIR/$UNIT"

systemctl --user daemon-reload
systemctl --user enable --now "$UNIT"

# Without this the service stops when you log out of SSH.
if command -v loginctl >/dev/null; then
  loginctl enable-linger "$(id -un)" 2>/dev/null \
    || echo "note: could not enable linger — run: sudo loginctl enable-linger $(id -un)"
fi

echo
systemctl --user --no-pager status "$UNIT" | head -5
echo
echo "Done. Useful commands:"
echo "  systemctl --user restart reel-to-action     # after editing the code"
echo "  systemctl --user status reel-to-action"
echo "  journalctl --user -u reel-to-action -f      # live logs"
