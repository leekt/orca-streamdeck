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
import time

import orca_streamdeck as core

AGENT_TYPE = "codex"
RETRY_SECONDS = 4.0   # per terminal: leave the screen time to repaint after an Enter
TAIL_LINES = 40       # how far back to look for the modal (it isn't always last)
REPOS = None          # set to e.g. {"orchestra-web"} to blind-approve in those
                      # repos only; None means the whole fleet

# Modal headers + the shared confirm footer, lifted from the codex 0.145.0
# binary's string table. The footer alone catches modal kinds not listed here;
# the headers survive a redraw that clips the footer.
MARKERS = (
    "Press enter to confirm",                        # every approval modal
    "Would you like to run the following command?",  # exec
    "Would you like to make the following edits?",   # patch / apply_patch
    "Would you like to grant these permissions?",    # sandbox escalation
    "Do you want to approve network access to",      # network
    "needs your approval.",                          # MCP tool call
)
# Deliberately NOT matched: codex's ask-the-user question widget ("enter to
# submit answer"). Enter there would submit a blank answer; that one is yours.

# Orca's own per-tab blocked flag ("[ ! ] Action Required | <repo>"). This is the
# LIVE half of the signal: codex's output stream has no liveness at all — a
# quiesced agent draws its modal once and never repaints, so "is a modal still
# up?" is unanswerable from the terminal alone. Orca clears this the moment the
# agent unblocks, which is what stops us pressing Enter at stale modal text.
TITLE_MARKER = "action required"


def _squash(s):
    """Drop all whitespace — the terminal comes back as TUI redraw frames with
    spaces eaten ("2.No,andtellCodexwhattododifferently"), so spaced substrings
    don't survive."""
    return "".join(s.split()).lower()


SQUASHED = tuple(_squash(mk) for mk in MARKERS)


def is_blocked(title):
    """Orca says this tab is waiting on the human. Free — the title rides along on
    the `terminal list` that fetch_items already makes."""
    return TITLE_MARKER in (title or "").lower()


def wants_approval(tail, title):
    """True if this terminal is blocked *on an approval modal*. Pure.

    Needs both halves: the title alone can't tell an approval apart from an agent
    that simply finished, and the tail alone has no notion of "still on screen"."""
    return is_blocked(title) and any(mk in _squash(tail) for mk in SQUASHED)


def describe(tail):
    """What we're about to say yes to, for the log — the modal's kind plus the
    text it's asking about. "approved <handle>" alone leaves no audit trail of
    what a blind daemon agreed to on your behalf."""
    flat = " ".join(tail.split())
    for raw, squashed in zip(MARKERS[1:], SQUASHED[1:]):   # [0] is the generic footer
        i = flat.find(raw)
        if i >= 0:
            return f"{raw.rstrip('?.')} — {flat[max(0, i - 90):i].strip()}".rstrip(" —")
        if squashed in _squash(flat):       # redraw ate the spaces; kind only
            return raw.rstrip("?.")
    # exec modals often carry no header at all, just the option list — the command
    # being approved is whatever precedes it.
    i = flat.find("1. Yes")
    return flat[max(0, i - 90):i].strip() if i > 0 else "approval modal"


def read_tail(handle, env=None):
    """The last TAIL_LINES of output, as one blob.

    Not `terminal show`'s preview: that's 300 chars of the same stream, and a
    modal often sits further back than that (the transcript keeps printing after
    it's drawn), so it silently misses exactly the case we care about."""
    r = core._orca_json(["terminal", "read", "--terminal", handle,
                         "--limit", str(TAIL_LINES), "--json"], env) or {}
    return "".join((r.get("terminal") or {}).get("tail") or [])


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
