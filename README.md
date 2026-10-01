# herdr-streamdeck

Live Herdr agent controls on an Elgato Stream Deck, with an optional macOS menu
bar app. One tile per recognized agent in your configured local Herdr session
and enabled saved SSH machines. A single Rust binary provides the deck
controller, the Codex approval helper, status inspection, the menu bar, and the
LaunchAgent installer.

## Controls

The default view sorts agents by urgency: **blocked → working → done → idle →
unknown**. Workspace names and stable identicons identify each project. The age
badge measures time since a state change observed by this controller; it resets
when the controller restarts. A dot marks unseen `done` agents.

- **Tap an agent:** focus it directly in Herdr and raise the terminal app.
- **Hold an agent:** open its action page for approval, AUTO, interrupt, and diffs.
- **Tap the bottom-right status key:** cycle pages.
- **Hold the status key:** cycle Codex auto-approval through 30 minutes, one
  hour, forever, and off. The badge shows the remaining window.
- The keys pulse while an agent is blocked, and the display jumps to the most
  urgent page when the blocked count changes from zero to nonzero.

The action page fits a six-key Stream Deck Mini; larger decks use the same layout:

```text
FOCUS   APPRV   AUTO
INTR    DIFFS   BACK
```

**FOCUS** selects the exact agent in Herdr and raises the configured terminal
app. That app must already have a client viewing the target machine and session.
Herdr's public CLI can focus an agent on its server, but cannot switch the local
client's machine selection; select that machine in the sidebar first.
**APPRV** confirms the current Codex approval; hold to decline with Escape.
It is disabled for ordinary questions and unrecognized dialogs. Remote actions
run over SSH on the agent's owning machine, using that machine's saved session.
**AUTO** enables approval until turned off for that specific Codex conversation;
tap again to disarm. Hold AUTO to cycle 30 minutes, one hour, forever, and off.
The key shows `off`, the remaining time, or `forever`; `all` means a session-wide
window also covers this agent. Per-agent AUTO requires Herdr to report a
conversation ID; session-wide AUTO also covers local Codex panes without one,
including newly split panes. **INTR** sends Escape to Codex, or Ctrl-C to another
working agent. Idle agents cannot be interrupted from the deck.
**DIFFS** creates a `Changes` tab in the agent's Herdr workspace, showing staged
and unstaged Git diffs plus untracked filenames in `less`. Press `q` to leave
the viewer and close that tab when finished. **BACK** returns to the overview;
the action page also returns after 30 seconds without a keypress.

## Setup

Requires macOS, Rust 1.85+, Herdr with `api snapshot` and agent control commands
(0.9.0+ for saved SSH machines), and a Stream Deck with at least six display keys.
The Pedal, dials, and touch strips are not supported. The USB HID library is
compiled into the binary; no Homebrew packages are needed.

```sh
cargo build --release
./target/release/herdr-streamdeck status
./run.sh
```

`status` prints machine connectivity and agent tiles as JSON without opening
the USB device or sending input, and exits nonzero if any included machine is
unreachable. `run.sh` quits Elgato's app to release the USB device and runs the
controller. Ctrl-C stops it. Elgato's app remains closed.

## Session and terminal app

Copy `config.example.json` to `~/.config/herdr-streamdeck/config.json`, or let
the service installer create it:

```json
{
  "session": "default",
  "terminal_app": "Ghostty",
  "include_remote_machines": true
}
```

Use a named local Herdr session by changing `session`. `terminal_app` can be
`Ghostty`, `iTerm`, `Terminal`, or another installed
terminal app; an empty string disables raising the app. Restart the services
after changing this configuration. The controller clears inherited `HERDR_*`
context when invoking the CLI so launchd and terminal launches use the same
configured session.

## Remote machines

The controller discovers enabled profiles from `herdr machine list --json`.
Each profile supplies its label, SSH target, and remote session. No duplicate
host list is needed in the Stream Deck configuration. Add a machine in an
interactive terminal, after setting up its SSH credentials:

```sh
ssh herdr-remote
# Exit back to this Mac, then:
herdr machine add herdr-remote --label "Mac Mini Dev"
```

If the profile is already saved, the controller picks it up automatically.
Add `--remote-session NAME` when creating a profile for a named remote session.
Profiles that are renamed, disabled, removed, or retargeted are reflected on
the next poll. Set `include_remote_machines` to `false` for local-only operation.

Local and remote tiles show their machine labels when saved machines are present.
The status key shows the number of online machines and reports connection failures;
the optional menu bar lists each machine and its error. Remote snapshots refresh
in background threads, so an unreachable host does not delay local polling.
Failed snapshots remove that machine's actionable tiles until it reconnects.

Connections use normal OpenSSH config and keys, with `BatchMode=yes` and strict
host-key verification. The controller does not answer SSH prompts or install or
restart remote servers. If a remote stays offline, verify this from a terminal:

```sh
ssh -o BatchMode=yes herdr-remote 'echo connected'
./target/release/herdr-streamdeck status
```

The SSH test should print `connected` without asking for credentials. A terminal
login that needs a passphrase or 1Password approval does not guarantee unattended
access from launchd; load the appropriate SSH agent/key first. The backend finds
Herdr on the remote PATH or common direct, Homebrew, and Nix install paths.
Remote DIFFS uses Git and `less` on that machine without copying helper files.

Use `status --local-only` to check only the local session.

## Background services

From this checkout, after `cargo build --release`:

```sh
./target/release/herdr-streamdeck install             # prepare plists only
./target/release/herdr-streamdeck install --install   # start deck + disarmed helper
./target/release/herdr-streamdeck install --install --menubar
```

The installer generates paths from the current checkout and binary, updates
`~/Library/LaunchAgents`, and restarts the selected services. Every installation
starts auto-approval disarmed. The menu bar app is optional:

```sh
./target/release/herdr-streamdeck menubar
```

Logs live at `~/Library/Logs/herdr-streamdeck.log`, `herdr-autoapprove.log`, and
`herdr-menubar.log`. The LaunchAgent labels are `com.taek.herdr-streamdeck`,
`com.taek.herdr-autoapprove`, and `com.taek.herdr-menubar`.

## Auto-approval behavior

The helper is an independent service, dormant until the deck or menu bar writes
`~/.herdr-streamdeck-armed`. Arming allows it to approve recognized Codex requests
without another human decision. Disarmed, it makes no Herdr calls.

Session-wide windows apply to the configured **local** session. Remote Codex
auto-approval requires explicitly arming AUTO on that remote agent's tile.
Agent identities include the machine, remote target, session, terminal, and
conversation, so a local window cannot authorize an unrelated remote pane.
The helper's `--always` option also remains local unless remote agent identities
are explicitly supplied with `--only`.

It requires all of the following before sending Enter:

- A recognized Codex agent with a live `blocked` state. Conversation-scoped
  windows (including `--only`) additionally require a conversation identity;
  session-wide local windows also cover panes without one.
- A recognized approval dialog in the current detection screen, with the
  one-time Yes option visibly selected.
- An unexpired window for the configured session and, when scoped, that exact
  conversation.
- The same agent identity and approval state on the final check.

Unknown agents, free-text questions, missing screens, changed dialogs, and
selected standing-approval options are skipped. Each unchanged observed modal
is answered once. Failed input is not recorded as an approval. Herdr's CLI does
not provide an atomic compare-and-send operation; the last state check and input
are separate calls. A UI change between them remains possible.

To disarm without the UI, delete the arm file:

```sh
trash ~/.herdr-streamdeck-armed
```

## Migration notes

The project moved from `~/orca/projects/orca-streamdeck` to
`~/workspace/herdr-streamdeck`, preserving its Git history. Runtime commands use
Herdr exclusively. Orca remote discovery was replaced by Herdr's saved SSH
machine catalog. Orca PR badges, pins, and the Orca-board worktree cleanup
command were retired: Herdr's agent snapshot does not expose those Orca records.
Workspaces and Git checkouts are never deleted by the controller.

The Python implementation was replaced by this Rust binary in September 2026.
Agent identities and arm files are byte-compatible: the Rust port hashes the
same `json.dumps(sort_keys=True)` shape, so existing scoped windows stay valid.
The Python installer retired the old Orca LaunchAgents; the Rust installer only
manages the `com.taek.herdr-*` labels.

## Development

```sh
cargo test
cargo clippy --all-targets
./target/release/herdr-streamdeck preview /tmp/keys.png
```

`src/model.rs` holds configuration, identities, and tile construction;
`src/backend.rs` runs Herdr locally or over SSH and polls the fleet;
`src/approval.rs` recognizes Codex dialogs and owns the arm file;
`src/controller.rs` maps deck keys to actions; `src/ui.rs` renders tiles;
`src/menubar.rs` is the AppKit menu bar. Tests in `tests/port.rs` exercise real
CLI-shaped fixtures through a fake command runner: identity collisions across
machines, session routing, SSH argument quoting, approval gating, disconnect
handling, key layouts, auto-approval scope, and Git review output, without
touching an agent or the USB device.
