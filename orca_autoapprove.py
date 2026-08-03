#!/usr/bin/env python3
"""Auto-approve every Codex approval modal across the Orca fleet.

Codex doesn't have one approval prompt, it has a family of them — exec, edits,
permissions, network access, MCP tool calls — all sharing a modal whose first
option is a "Yes …" and whose footer reads "Press enter to confirm or esc to
cancel". This watches every codex agent Orca reports and presses Enter whenever
one of those modals is on screen.

It approves EVERYTHING codex asks: shell commands, file writes, network calls,
tool calls — no human in the loop. Claude agents are left alone. Run it only
where you'd be fine with codex's own `--dangerously-bypass-approvals-and-sandbox`.

Runs continuously under its own LaunchAgent and acts only while something has
armed it in ~/.orca-streamdeck-armed — the deck's status key, the menu bar, or
you. Disarmed, it makes no orca calls at all. Being independent is the whole
point: it used to be a child of the Stream Deck process, so when Elgato's app
grabbed the USB back and the deck crash-looped, blind approval died with it
while nobody was watching.

Run: ./.venv/bin/python orca_autoapprove.py --always   # ignore the arm file

Author: taek <leekt216@gmail.com>
"""
import subprocess
import sys
import time

import orca_streamdeck as core

# `--only <handle>` (repeatable) narrows blind approval to specific terminals, so
# the deck can say "trust THIS agent" instead of "trust the fleet".
ONLY = {sys.argv[i + 1] for i, a in enumerate(sys.argv) if a == "--only"
        and i + 1 < len(sys.argv)}

# The pure detection (markers, is_blocked, wants_approval, describe) lives in the
# core so the deck's agent page can show what an agent is asking without importing
# this module — that direction would be an import cycle.
from orca_streamdeck import (MARKERS, SQUASHED, TITLE_MARKER,  # noqa: E402,F401
                             _squash, is_blocked, wants_approval, describe,
                             read_tail)

AGENT_TYPE = "codex"
RETRY_SECONDS = 4.0   # per terminal: leave the screen time to repaint after an Enter
REPOS = None          # set to e.g. {"orchestra-web"} to blind-approve in those
                      # repos only; None means the whole fleet


def approve(handle, env=None):
    """Enter confirms the highlighted option — codex orders these modals with the
    narrowest "Yes, just this once" first, so this never grants a standing rule.
    Refuses an empty handle: the CLI would aim it at whatever terminal is active."""
    return core.send_to_terminal(["terminal", "send", "--enter"], handle, env)


_unreadable = set()      # handles already reported as unverifiable, so we warn once


def poll(sent_at, now):
    """One sweep. `sent_at` maps handle -> when we last pressed Enter there;
    mutated in place. Returns (handle, description) for each modal approved."""
    # Walk flagged TERMINALS, not agents. Orca has no agent record for every pane
    # — a codex asking for approval can come back with agent_type None — and
    # filtering on the agent list silently skipped exactly those.
    fresh = []
    by_handle = {it["handle"]: it for it in (core.fetch_items() or []) if it["handle"]}
    for env in (None,) + core.fetch_environments():
        for h, title in core.fetch_titles(env).items():
            if not is_blocked(title):
                continue
            it = by_handle.get(h, {})
            # A *known* claude agent is never ours. An unknown pane might well be
            # codex, and the modal markers below are codex-specific strings, so
            # they gate it safely on their own.
            if it.get("agent_type") not in (AGENT_TYPE, None):
                continue
            repo = it.get("repo") or "?"
            if ONLY and h not in ONLY:
                continue
            if REPOS is not None and repo not in REPOS:
                continue
            if now - sent_at.get(h, float("-inf")) < RETRY_SECONDS:
                continue      # just answered; give the modal time to clear
            tail = read_tail(h, env)
            where = f"{env}/{repo}" if env else repo
            if wants_approval(tail, title):
                approve(h, env)
                sent_at[h] = now
                fresh.append((h, f"{where}: {describe(tail)}"))
            elif len(tail.strip("• \n")) < 20 and h not in _unreadable:
                # Orca is flagging this pane but has captured no output for it
                # (happens after an Orca restart: it reattaches to the pty, and a
                # codex sitting on a modal never repaints). Can't confirm what's
                # on screen, so refuse to press — but say so instead of silently
                # ignoring it, which is how one sat unanswered.
                _unreadable.add(h)
                print(f"NEEDS YOU {where}: flagged, but no captured output to "
                      f"verify — approve it yourself ({h})", flush=True)
    return fresh


# The deck polls every core.POLL_SECONDS because it's a display; this is a
# reflex, so it runs faster. Each armed tick costs one `terminal list` per machine
# (~0.3s) and nothing else until something is actually flagged.
ARMED_POLL = 0.5
IDLE_SECONDS = 5.0    # how often to re-check the arm file while stood down


def main():
    """Run forever; act only while ARMED_FILE says to.

    The deck and menu bar arm this by writing that file, not by spawning us — so
    a crashed deck (or no deck at all) can't take blind approval down with it.
    `--always` ignores the file, for running this by hand."""
    always = "--always" in sys.argv
    sent_at, was_armed = {}, None
    while True:
        armed = core.armed_state(time.time())
        if not (always or armed):
            if was_armed:
                print("stood down", flush=True)
                was_armed, sent_at = False, {}
            time.sleep(IDLE_SECONDS)    # no orca calls at all while disarmed
            continue
        if not was_armed:
            scope = f" for {armed[2]}" if armed and armed[2] else " fleet-wide"
            print(f"armed{scope}", flush=True)
            was_armed = True
        # A scope in the file beats the command line; the deck owns it at runtime.
        global ONLY
        ONLY = {armed[2]} if (armed and armed[2]) else (ONLY if always else set())
        # Cheap trigger first: skip the full fleet query unless Orca is flagging
        # something. Nothing to answer is by far the common case.
        if core.any_blocked():
            for _, what in poll(sent_at, time.monotonic()):
                print(f"approved {what}", flush=True)
        time.sleep(ARMED_POLL)


if __name__ == "__main__":
    main()
