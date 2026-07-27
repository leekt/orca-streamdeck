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

Run: ./.venv/bin/python orca_autoapprove.py

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


def poll(sent_at, now):
    """One sweep. `sent_at` maps handle -> when we last pressed Enter there;
    mutated in place. Returns (handle, description) for each modal approved."""
    # Gate on Orca's flag before reading anything: it's already in hand, and the
    # agent's own `state` is no use here (a codex agent mid-command still reports
    # "waiting"). So a quiet fleet costs zero extra orca calls.
    fresh = []
    for it in (core.fetch_items() or []):
        h, env = it["handle"], it.get("env")
        if it.get("agent_type") != AGENT_TYPE or not h or not is_blocked(it.get("title")):
            continue
        if ONLY and h not in ONLY:
            continue
        if REPOS is not None and it["repo"] not in REPOS:
            continue
        if now - sent_at.get(h, float("-inf")) < RETRY_SECONDS:
            continue          # just answered; give the modal time to clear
        tail = read_tail(h, env)
        if wants_approval(tail, it["title"]):
            approve(h, env)
            sent_at[h] = now
            where = f"{env}/{it['repo']}" if env else it["repo"]
            fresh.append((h, f"{where}: {describe(tail)}"))
    return fresh


def main():
    sent_at = {}
    while True:
        for _, what in poll(sent_at, time.monotonic()):
            print(f"approved {what}", flush=True)
        time.sleep(core.POLL_SECONDS)


if __name__ == "__main__":
    main()
