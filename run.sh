#!/usr/bin/env bash
# Launch the Orca Stream Deck control pane.
# Quits Elgato's Stream Deck app (it holds the USB exclusively), runs the
# controller, and relaunches Elgato's app on exit.
set -euo pipefail
cd "$(dirname "$0")"

# launchd hands us a minimal PATH; orca and brew live here.
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

ELGATO="Elgato Stream Deck"
if pgrep -f "$ELGATO.app" >/dev/null; then
  echo "Quitting $ELGATO to free the device..."
  osascript -e "quit app \"$ELGATO\"" || true
  sleep 1
fi

# streamdeck's HID backend needs Homebrew's libhidapi on the dyld path.
BREW_PREFIX="$(brew --prefix 2>/dev/null || echo /opt/homebrew)"
export DYLD_LIBRARY_PATH="$BREW_PREFIX/lib${DYLD_LIBRARY_PATH:+:$DYLD_LIBRARY_PATH}"
exec ./.venv/bin/python -u orca_streamdeck.py
