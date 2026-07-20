# orca-streamdeck

Turn any Elgato **Stream Deck** into a live control pane for [Orca](https://www.orcastudio.io/) —
an attention-router across your fleet of agent sessions. Each key is one agent,
colored by state; tap to jump to it, hold to interrupt it. A DIY take on the
same idea as OpenAI's Codex Micro, on hardware you already own.

## What it does

- Polls `orca worktree ps` + `orca terminal list` every 2s and paints **one tile
  per agent**, urgency-sorted, colored by state (Codex-Micro-style legend):
  - 🔴 **STOPPED** — interrupted/errored agent (needs you most)
  - 🟡 **needs input** — `permission` (blocked on a prompt) / `waiting`
  - 🔵 **working** — in progress
  - 🟢 **done** — finished, your move
  - ⚪ **idle** — attached, nothing pending
- Urgency order: **stopped → needs-input → working → done → idle**.
- **Bottom-right = status key**: big count of agents needing you (or "all clear"),
  `page x/y`, press to cycle pages.
- Deck **pulses bright** when something needs you; a macOS notification fires on
  the transition into "needs you".
- **Tap** a tile → focus that agent's terminal in Orca (raises the app).
- **Hold** a tile (≥0.7s) → interrupt that agent (Esc/Ctrl-C to its terminal).
  Interrupting is always safe; there is deliberately **no blind "approve"** key —
  tap-to-jump is the approve path.

Adapts to any model (Mini/MK.2/XL/Plus/Plus XL): key count, image size and fonts
come from the device. Dials/touch strips (Plus/Plus XL) aren't used yet.
Screenless decks (Pedal) aren't supported.

## Setup

```sh
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
brew install hidapi          # the HID backend the library needs
```

Requires the Orca CLI reachable (`orca status`) and macOS.

## Run

```sh
./run.sh
```

Quits Elgato's Stream Deck app (it holds the USB device exclusively) and runs the
controller. Ctrl-C to stop; Elgato's app is left closed.

### Run always-on (LaunchAgent)

Auto-starts at login and restarts on crash:

```sh
cp com.taek.orca-streamdeck.plist ~/Library/LaunchAgents/ \
  && launchctl load -w ~/Library/LaunchAgents/com.taek.orca-streamdeck.plist
```

Logs to `~/Library/Logs/orca-streamdeck.log`. Apply config changes with
`launchctl kickstart -k gui/$(id -u)/com.taek.orca-streamdeck`. To remove:

```sh
launchctl unload -w ~/Library/LaunchAgents/com.taek.orca-streamdeck.plist
```

## Config

Tunables at the top of `orca_streamdeck.py`: `POLL_SECONDS`, `LONG_PRESS_SEC`,
`DIM_BRIGHTNESS`, `PULSE`, and the `STATUS` color/urgency legend.

**Project-group features** (Orca groups repos via `projectGroupId`) — all default
**off**, so the pane stays urgency-flat until you enable one:

- `GROUP_ACCENT = True` — draw a per-group color stripe down the left of each tile
  (color derived from the group id, since repo `badgeColor`s are all default).
- `GROUP_FILTER = "<repo name>"` — show only agents in that repo's group
  (e.g. `"sra-dashboard"` → the whole SRA group).
- `GROUP_PAGES = True` — one group per page instead of urgency-flat pagination.

Enabling any of these (or icons below) adds one `orca repo list` query per poll.

**Per-project icons** (`SHOW_ICONS`, default **on**): each tile shows an
auto-generated **identicon** — a GitHub-style block pattern derived from the repo
name, distinct per project, colored by the name hash. No network, no external
avatars. Set `SHOW_ICONS = False` for the text-only layout.

## Test

```sh
./.venv/bin/python test_orca_streamdeck.py
```

Offline checks for urgency order, pagination, and the agent→terminal
(`paneKey` → handle) mapping.

## Files

- `orca_streamdeck.py` — the controller
- `run.sh` — launcher (device handoff, launchd-safe PATH/dyld)
- `com.taek.orca-streamdeck.plist` — LaunchAgent for always-on
- `test_orca_streamdeck.py` — offline checks
