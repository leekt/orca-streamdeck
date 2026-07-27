# orca-streamdeck

Turn any Elgato **Stream Deck** into a live control pane for [Orca](https://www.orcastudio.io/) —
an attention-router across your fleet of agent sessions. Each key is one agent,
colored by state; tap to jump to it, hold to interrupt it. A DIY take on the
same idea as OpenAI's Codex Micro, on hardware you already own.

Two faces over the same brains: the **Stream Deck** pane (`orca_streamdeck.py`)
and a **macOS menu bar app** (`orca_menubar.py`) — use either or both.

## What it does

- Polls `orca worktree ps` + `orca terminal list` every 2s and paints **one tile
  per agent**, urgency-sorted, colored by state (Codex-Micro-style legend):
  - 🔴 **STOPPED** — interrupted/errored agent (needs you most)
  - 🟡 **needs input** — `permission` (blocked on a prompt) / `waiting`
  - 🔵 **working** — in progress
  - 🟢 **done** — finished, your move
  - ⚪ **idle** — attached, nothing pending
- Order: **urgency** (stopped → needs-input → working → done → idle), then your
  **pinned** worktrees, then **longest-ignored first**. A pin deliberately does
  *not* outrank urgency — nothing should bury a stopped agent.
- Each tile carries, in its corners: **age** since that agent last said anything
  (`45s` / `12m` / `4h` / `2d`), its linked **`#PR`**, and a dot when Orca marks
  the worktree **unread**. Without the age every `done` tile looks equally fresh,
  and the one you abandoned an hour ago hides among those that just finished.
- **Bottom-right = status key**: big count of agents needing you (or "all clear"),
  `page x/y`, press to cycle pages. **Hold** it to cycle codex auto-approve
  through 30m / 1h / forever / off (amber countdown badge — see below).
- Deck **pulses bright** when something needs you. (Orca sends its own
  notifications, so this doesn't add duplicate alerts.)
- **Tap** a tile → open that agent's **page** (below). **Hold** → interrupt it.
- The **empty keys glow** with the fleet's worst state, so the unit reads from
  across the room instead of needing you to read 80px tiles. A calm fleet stays
  dark — the wash only appears for stopped/needs-you.
- When the needs-you count goes **0 → N**, the deck **jumps to the page** holding
  the most urgent agent, rather than leaving you to find which page it's on.

## Agent page

Tapping a tile drills into one agent. The status key becomes **Back**, and the
page falls back to the fleet after 30s idle.

```
┌───────┬───────┬───────┐
│ FOCUS │ APPRV │ AUTO  │   APPRV: tap = Enter, hold = Esc (deny)
├───────┼───────┼───────┤   AUTO:  auto-approve THIS agent for 30m
│ INTR  │ DIFFS │ BACK  │   DIFFS: open its changed files as diffs
└───────┴───────┴───────┘
```

- **APPRV** shows what the agent is actually asking (`command`, `edits`, `perms`,
  `network`, `tool`) and is greyed out when there's nothing pending. Tap approves
  that one modal; hold sends Esc, which codex maps to *"No, and tell Codex what to
  do differently"* — a refusal, not a kill.
- **AUTO** scopes the auto-approver to this single terminal (`--only <handle>`),
  so "trust this agent" doesn't mean "trust the fleet". Press again to stop. A
  scoped arming is deliberately **not** persisted across restarts — resuming it
  would re-target a handle that may no longer exist.
- Keys grey out when they can't act (no terminal, no worktree, not a codex agent).
- Closing a terminal is deliberately **not** on this page. It's one keypress from
  a tile, and it's unrecoverable.

Everything here is per-agent and deliberate; *blind* fleet-wide approval is the
separate opt-in daemon below. Actions carry the agent's machine, so the page
works the same for a remote agent.

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

## Menu bar app

A `rumps` menu bar app sharing the deck's logic — no Stream Deck required:

```sh
./.venv/bin/python orca_menubar.py
```

The title shows the needs-you count (`🔴 3`, or `🐳` when clear); the dropdown
lists agents by urgency with their `#PR` and age, each with **Focus** and
**Interrupt**, plus the codex auto-approve duration submenu. Updates every
`POLL_SECONDS` (a running process, so — unlike a WidgetKit widget — it's live).
Always-on: `cp com.taek.orca-menubar.plist ~/Library/LaunchAgents/ && launchctl
load -w ~/Library/LaunchAgents/com.taek.orca-menubar.plist`.

## Codex auto-approve (opt-in, dangerous)

**Hold the status key** to cycle how long it stays armed:

| holds | state | badge |
|---|---|---|
| 1 | armed 30 min | `AUTO 30m` (counts down) |
| 2 | armed 1 hour | `AUTO 1h` |
| 3 | armed until you stop it | `AUTO ON` |
| 4 | off | — |

The badge counts the window down, because the point of a window is knowing it's
closing. The menu bar app has the same three durations as a submenu (click the
checked one to turn it off). Or run it standalone, no expiry:

```sh
./.venv/bin/python orca_autoapprove.py
```

Timed windows exist because blind approval is a mode you will forget you left on
— `Forever` is there when you mean it. The deadline is written to
`~/.orca-streamdeck-armed`, so a deck crash (launchd restarts it) comes back
armed rather than silently dropping to off, while a lapsed window can't be
resurrected. Arming also `pkill`s any other auto-approver first: two of them
would each press Enter on the same modal.

Watches every **codex** agent in the fleet and presses Enter on its approval
modals, so codex never sits blocked. Codex has a family of them — exec, edits,
permission escalation, network access, MCP tool calls — all matched by their
headers plus the shared `Press enter to confirm` footer (strings taken from the
codex binary). Enter picks the highlighted option, which codex renders as the
narrowest **"Yes, just this once"**, so no standing rule is ever granted.

- Fires only when **two independent signals agree**, because neither works alone:
  - **Liveness** — Orca's tab title says `Action Required`. Codex's *output* has
    no liveness at all: a quiesced agent draws its modal once and never repaints,
    so nothing in the transcript says whether it's still up. Orca clears this the
    instant the agent unblocks, which is what stops repeat presses at stale text.
  - **Kind** — a modal marker in the last 40 lines. The title can't tell an
    approval apart from an agent that simply finished and wants a prompt.
  The agent's own `waiting` state is useless here (codex reports it mid-command),
  and `terminal show`'s `preview` is too small — 300 chars of the same stream,
  and codex keeps printing after drawing the modal, so it often sits further back.
- The tail read only happens for terminals Orca has already flagged, so a quiet
  fleet costs no extra `orca` calls.
- Every approval logs **what** it agreed to — `approved orchestra-web: Would you
  like to make the following edits` — not just an opaque terminal handle.
- `REPOS = {"orchestra-web"}` in `orca_autoapprove.py` limits it to named repos
  (default `None` = whole fleet). Worth setting if any worktree holds credentials.
- At most one Enter per terminal per `RETRY_SECONDS` (4s), so it can't spam but
  still answers codex asking three times in a row.
- **Claude agents are untouched**, and codex's ask-the-user *question* widget is
  skipped (Enter there would submit a blank answer).

This is a blind yes to everything codex asks. Run it only on worktrees where
you'd be comfortable with codex's own `--dangerously-bypass-approvals-and-sandbox`.
No LaunchAgent ships for it on purpose — arm it deliberately, per session.

## Run always-on (LaunchAgent)

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
`DIM_BRIGHTNESS`, `PULSE`, `AUTO_DURATIONS`, and the `STATUS` color/urgency legend.

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

- `orca_streamdeck.py` — the Stream Deck controller
- `orca_menubar.py` — the macOS menu bar app (shares the controller's logic)
- `orca_autoapprove.py` — opt-in codex approval auto-presser (see above)
- `run.sh` — Stream Deck launcher (device handoff, launchd-safe PATH/dyld)
- `com.taek.orca-streamdeck.plist` / `com.taek.orca-menubar.plist` — LaunchAgents
- `test_orca_streamdeck.py` — offline checks
