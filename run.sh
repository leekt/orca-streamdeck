#!/usr/bin/env bash
# Launch the Herdr Stream Deck controller.
# Quits Elgato's Stream Deck app (it holds the USB exclusively), then runs the
# controller. Elgato's app stays closed while this service owns the device.
set -euo pipefail
cd "$(dirname "$0")"

# launchd hands us a minimal PATH; Herdr's installer uses ~/.local/bin.
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

ELGATO="Elgato Stream Deck"
if pgrep -f "$ELGATO.app" >/dev/null; then
  osascript -e "tell application \"$ELGATO\" to quit" 2>/dev/null || pkill -f "$ELGATO.app" || true
  sleep 1
fi

exec ./target/release/herdr-streamdeck run
