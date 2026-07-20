# orca-streamdeck

Turns any Elgato **Stream Deck** into a live control pane for Orca — an
attention-router across your fleet of agent sessions.

## What it does

- Polls `orca worktree ps` + `orca terminal list` every 2s and paints **one tile
  per agent**, sorted by urgency (needs input → idle → working), colored by state:
  - 🔴 `permission` — agent blocked on a prompt (**needs you**)
  - 🟠 `waiting` — waiting for input
  - 🟢 `active` — idle/attached · dim green `done`
  - 🔵 `working` — busy, leave alone
- **Bottom-right = status key**: big count of agents needing you (or "all clear"),
  `page x/y`, and press to cycle pages.
- Deck **dims** when nothing needs you and **pulses bright** when something does;
  a macOS notification fires on the transition into "needs you".
- **Tap** a tile → focus that agent's terminal in Orca (raises the app).
- **Hold** a tile (≥0.7s) → interrupt that agent (Esc/Ctrl-C to its terminal).
  Interrupting is always safe; there is deliberately **no blind "approve"** key —
  tap-to-jump is the approve path.

Adapts to any model (Mini/MK.2/XL/Plus/Plus XL): key count, image size and fonts
come from the device. Dials/touch strips (Plus/Plus XL) aren't used yet.
Screenless decks (Pedal) aren't supported.

## Run (manual)

```sh
./run.sh
```

Quits Elgato's Stream Deck app (it holds the USB exclusively) and runs the
controller. Ctrl-C to stop. Elgato's app is left closed.

## Run always-on (LaunchAgent)

Auto-starts at login and restarts on crash. Installing a login-persistent
service is intentionally a manual step — run it yourself:

```sh
cp com.taek.orca-streamdeck.plist ~/Library/LaunchAgents/ \
  && launchctl load -w ~/Library/LaunchAgents/com.taek.orca-streamdeck.plist
```

Logs to `~/Library/Logs/orca-streamdeck.log`. To stop/remove:

```sh
launchctl unload -w ~/Library/LaunchAgents/com.taek.orca-streamdeck.plist
```

## Requirements

- Python venv with `streamdeck` + `pillow` (`.venv/`, see `requirements.txt`)
- Homebrew `hidapi` (`brew install hidapi`) — the HID backend
- Orca CLI reachable (`orca status`)

## Files

- `orca_streamdeck.py` — the controller
- `run.sh` — launcher (device handoff, launchd-safe PATH/dyld)
- `com.taek.orca-streamdeck.plist` — LaunchAgent for always-on
- `test_orca_streamdeck.py` — offline checks (urgency, pagination, paneKey→handle)
