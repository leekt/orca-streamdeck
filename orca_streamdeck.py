#!/usr/bin/env python3
"""Orca control pane for any Elgato Stream Deck.

Polls Orca and paints one tile per *agent*, sorted by urgency
(needs input -> idle -> working) and colored by state. The bottom-right key is a
status key: shows how many agents need you, and cycles pages when there are more
agents than keys. The deck dims when nothing needs you and pulses when something
does. (Orca sends its own notifications, so this doesn't duplicate them.)

Tap a tile   -> open that agent's page: focus / approve / trust / interrupt /
                diffs, with Back on the status key.
Hold a tile  -> interrupt that agent (Esc/Ctrl-C to its terminal).
Hold status  -> cycle orca_autoapprove.py, which blind-approves every codex modal
                fleet-wide: off -> 30m -> 1h -> forever -> off. The key shows an
                amber badge counting the window down ("AUTO 24m" / "AUTO ON").

Works on any model (Mini/MK.2/XL/Plus/...) — key count, image size and fonts are
derived from the connected device. Screenless decks (Pedal) are not supported.

Author: taek <leekt216@gmail.com>
"""
import colorsys
import concurrent.futures
import functools
import hashlib
import json
import pathlib
import subprocess
import sys
import threading
import time

from PIL import Image, ImageDraw, ImageFont
from StreamDeck.DeviceManager import DeviceManager
from StreamDeck.ImageHelpers import PILHelper

POLL_SECONDS = 2.0
LONG_PRESS_SEC = 0.7        # hold >= this to interrupt instead of focus
DIM_BRIGHTNESS = 100        # when nothing needs you
PULSE = (60, 100)           # brightness pulse endpoints when attention needed

# --- Project-group features (all default OFF: the pane stays urgency-flat with
# no group visuals until you turn one on). Orca groups repos via projectGroupId. ---
GROUP_ACCENT = False        # draw a per-group color stripe down the left of each tile

# --- Remote machines: Orca pairs with other runtimes (`orca environment list`),
# and every CLI command takes --environment. Each paired machine is polled next to
# the local one, so one pane covers the whole fleet wherever it runs. ---
INCLUDE_REMOTES = True
REMOTE_TIMEOUT = 8          # a sleeping mac mini must not stall the pane

# --- Per-project icons: an auto-generated identicon derived from the repo name,
# distinct per project (no network, no external avatars). ---
SHOW_ICONS = True
_ICON_CACHE = {}            # env:repo -> base identicon PIL.Image (RGBA)


def group_color(group_id):
    """Stable, vivid color derived from a group id (repo badgeColors are all the
    same default, so we hash the id into a distinct hue instead)."""
    if not group_id:
        return (90, 90, 90)
    hue = int(hashlib.md5(group_id.encode()).hexdigest(), 16) % 360 / 360
    r, g, b = colorsys.hsv_to_rgb(hue, 0.65, 0.95)
    return (round(r * 255), round(g * 255), round(b * 255))


def _identicon(name, size):
    """Deterministic GitHub-style identicon from a repo name: a 5x5, vertically
    mirrored block pattern colored by the name hash. Transparent background so the
    tile's state color shows between blocks."""
    h = hashlib.md5((name or "?").encode()).digest()
    r, g, b = colorsys.hsv_to_rgb(h[-1] / 255, 0.6, 0.9)
    fg = (round(r * 255), round(g * 255), round(b * 255), 255)
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    pad = size * 0.12
    cell = (size - 2 * pad) / 5
    for i in range(15):                # 3 cols x 5 rows, mirrored into 5 cols
        if h[i] & 1:
            col, row = i // 5, i % 5
            for c in (col, 4 - col):
                x, y = pad + c * cell, pad + row * cell
                d.rectangle([x, y, x + cell, y + cell], fill=fg)
    return im


def icon_image(item, size):
    """Sized identicon for a tile, cached per machine+repo — the same repo checked
    out on two machines has to look different, since both can be on screen."""
    name = f"{item.get('env') or ''}:{item.get('repo', '?')}"
    if name not in _ICON_CACHE:
        _ICON_CACHE[name] = _identicon(name, 128)
    return _ICON_CACHE[name].resize((size, size))

# Urgency ranking (lower sorts to the front) + color legend, aligned to the
# OpenAI Codex Micro convention: red=stopped/error, amber=needs input,
# green=done, white=idle, blue=working. (rank, color, label).
STATUS = {
    "interrupted": (0, (200, 40, 40), "STOPPED"),    # interrupted/errored — red
    "permission":  (1, (215, 140, 20), "NEEDS YOU"), # blocked on a prompt — amber
    "waiting":     (2, (215, 140, 20), "WAITING"),   # waiting for input — amber
    "working":     (3, (30, 90, 190), "working"),    # in progress — blue
    "done":        (4, (35, 130, 70), "done"),       # finished, your move — green
    "active":      (5, (210, 214, 222), "idle"),     # idle / attached — white
}
DEFAULT = (6, (90, 90, 90), "")
NEEDS_HUMAN = {"permission", "waiting"}


def age_label(ms, now_ms):
    """Compact time since an agent last said anything: 45s / 12m / 4h / 2d. Blank
    when it never spoke. Without this every 'done' tile looks equally fresh, and
    the one you abandoned an hour ago hides among the ones that just finished."""
    if not ms:
        return ""
    secs = max(0, int(now_ms) - int(ms)) // 1000    # now_ms is a float clock
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= size:
            return f"{secs // size}{unit}"
    return f"{secs}s"


def text_color(bg):
    """Black on light tiles, white on dark — so 'idle' white tiles stay legible."""
    r, g, b = bg
    return (20, 20, 20) if (0.299 * r + 0.587 * g + 0.114 * b) > 150 else (255, 255, 255)

# ponytail: hard-coded macOS system fonts; swap paths if they ever move.
FONT_PATHS = ["/System/Library/Fonts/Helvetica.ttc",
              "/System/Library/Fonts/Supplemental/Arial.ttf"]


@functools.lru_cache(maxsize=16)
def load_font(size):
    for p in FONT_PATHS:
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            continue
    return ImageFont.load_default()


# --- Orca queries ----------------------------------------------------------

def _orca_cmd(args, env):
    """`orca ...`, aimed at a paired machine when env is set (None = this one)."""
    return ["orca", *args] + (["--environment", env] if env else [])


def _orca_json(args, env=None):
    try:
        out = subprocess.run(_orca_cmd(args, env), capture_output=True, text=True,
                             timeout=REMOTE_TIMEOUT if env else 15)
        data = json.loads(out.stdout)
        return data.get("result") if data.get("ok") else None
    except (subprocess.SubprocessError, json.JSONDecodeError, ValueError):
        return None


@functools.lru_cache(maxsize=1)
def fetch_environments():
    """Names of the paired remote runtimes. Looked up once per process — pair a new
    machine and you restart the pane."""
    if not INCLUDE_REMOTES:
        return ()
    envs = (_orca_json(["environment", "list", "--json"]) or {}).get("environments", [])
    return tuple(e["name"] for e in envs if e.get("name"))


def urgency_key(it):
    """Urgency first — a pin shouldn't bury a stopped agent — then your pins, then
    oldest-untouched first, so the agent you've ignored longest surfaces."""
    return (STATUS.get(it["state"], DEFAULT)[0], not it["pinned"],
            it["last_output"] or 0, it["id"])


def build_items(worktrees, terminals, repo_groups=None, env=None):
    """Flat, urgency-sorted list of agent tiles. Pure — no I/O, so it's testable.
    Each item: {id, repo, sub, state, agent_type, handle, title, last_output,
    pinned, unread, pr, worktree_id, group}.
    repo_groups maps repoId -> projectGroupId (None -> group left unset)."""
    repo_groups = repo_groups or {}
    # agent paneKey ("{tabId}:{leafId}") -> terminal handle
    by_pane = {f"{t.get('tabId')}:{t.get('leafId')}": t.get("handle") for t in terminals}
    # handle -> tab title; Orca writes "[ ! ] Action Required | <repo>" here when
    # an agent is blocked on the human, which is a live signal its output isn't.
    titles = {t.get("handle"): t.get("title") or "" for t in terminals}
    last_out = {t.get("handle"): t.get("lastOutputAt") for t in terminals}
    # worktreeId -> handle of its most recently active terminal (fallback focus)
    recent = {}
    for t in terminals:
        # lastOutputAt can be missing OR null -> coerce to 0 for comparison.
        w, h, lo = t.get("worktreeId"), t.get("handle"), t.get("lastOutputAt") or 0
        if w and h and (w not in recent or lo > recent[w][0]):
            recent[w] = (lo, h)

    items = []
    for w in worktrees:
        wid, repo = w["worktreeId"], w.get("repo", "?")
        branch = w.get("displayName") or ""
        group = repo_groups.get(w.get("repoId"))
        # Orca already tracks these per worktree; the pane used to throw them away.
        shared = {"repo": repo, "worktree_id": wid, "group": group, "env": env,
                  "pinned": bool(w.get("isPinned")), "unread": bool(w.get("unread")),
                  "pr": (w.get("linkedPR") or {}).get("number")}
        agents = w.get("agents") or []
        if agents:
            for a in agents:
                handle = by_pane.get(a.get("paneKey")) or (recent.get(wid) or (0, None))[1]
                items.append({
                    **shared,
                    "id": a.get("paneKey") or wid,
                    "sub": a.get("taskTitle") or a.get("displayName") or a.get("agentType") or branch,
                    # an interrupted/stopped agent is the "error" signal -> red.
                    "state": "interrupted" if a.get("interrupted") else a.get("state"),
                    "agent_type": a.get("agentType"),
                    "handle": handle,
                    "title": titles.get(handle, ""),
                    "last_output": last_out.get(handle) or w.get("lastOutputAt"),
                })
        else:  # worktree with no agent session — show it at worktree level
            handle = (recent.get(wid) or (0, None))[1]
            items.append({
                **shared, "id": wid, "sub": branch, "state": w.get("status"),
                "agent_type": None, "handle": handle,
                "title": titles.get(handle, ""),
                "last_output": last_out.get(handle) or w.get("lastOutputAt"),
            })
    items.sort(key=urgency_key)
    return items


TAIL_LINES = 40             # how far back to look for a modal (not always last)


def read_tail(handle, env=None):
    """The last TAIL_LINES of a terminal's output, as one blob.

    Not `terminal show`'s preview: that's 300 chars of the same stream, and a
    modal often sits further back than that (the transcript keeps printing after
    it's drawn), so it silently misses exactly the case we care about."""
    r = _orca_json(["terminal", "read", "--terminal", handle,
                    "--limit", str(TAIL_LINES), "--json"], env) or {}
    return "".join((r.get("terminal") or {}).get("tail") or [])


def fetch_titles(env=None):
    """{handle: tab title} for one machine — the cheap half of a poll."""
    ts = (_orca_json(["terminal", "list", "--json"], env) or {}).get("terminals", [])
    return {t["handle"]: t.get("title") or "" for t in ts if t.get("handle")}


def any_blocked():
    """Is any machine flagging a tab as needing the human?

    One `terminal list` per machine (~0.27s here) against ~0.55s for a full
    fetch_items, because the title IS the whole trigger. Lets the auto-approver
    poll twice as often for the same money and only pay for the rest when there's
    actually something to answer."""
    envs = (None,) + fetch_environments()
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(envs)) as pool:
        return any(is_blocked(t)
                   for titles in pool.map(fetch_titles, envs)
                   for t in titles.values())


def fetch_repo_groups(env=None):
    """({repoId: groupId}, {repoName: groupId}) from `orca repo list`."""
    repos = (_orca_json(["repo", "list", "--json"], env) or {}).get("repos", [])
    by_id = {r["id"]: r.get("projectGroupId") for r in repos if r.get("id")}
    by_name = {r.get("displayName"): r.get("projectGroupId") for r in repos}
    return by_id, by_name


def fetch_env_items(env=None):
    """Tiles for one machine, or None if that Orca is unreachable."""
    wt = _orca_json(["worktree", "ps", "--json"], env)
    if wt is None:
        return None
    terms = (_orca_json(["terminal", "list", "--json"], env) or {}).get("terminals", [])
    # Only pay for the repo-list query when a group feature needs it (identicons
    # come straight from the repo name, which worktree ps already carries).
    by_id, _ = fetch_repo_groups(env) if GROUP_ACCENT else ({}, {})
    return build_items(wt.get("worktrees", []), terms, by_id, env)


def fetch_items():
    """Every machine's tiles in one urgency-sorted list, or None when the *local*
    Orca is unreachable (that's the pane being down; a remote being asleep just
    means its agents drop off until it wakes)."""
    envs = (None,) + fetch_environments()
    if len(envs) == 1:
        return fetch_env_items()
    # In parallel: a poll should cost one round trip, not one per machine.
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(envs)) as pool:
        results = list(pool.map(fetch_env_items, envs))
    if results[0] is None:
        return None
    items = [it for got in results if got for it in got]
    items.sort(key=urgency_key)
    return items


def paginate(items, key_count):
    """Pages of items; the last key is always reserved for the status key."""
    per = max(1, key_count - 1)
    pages = [items[i:i + per] for i in range(0, len(items), per)] or [[]]
    return pages



# --- Actions ---------------------------------------------------------------

def send_to_terminal(args, handle, env=None):
    """Run an `orca terminal` command against ONE terminal, or do nothing.

    The guard is the point: with an empty --terminal the CLI falls back to "the
    active terminal in the current worktree", so a lost handle doesn't fail, it
    hits whatever is in front of you — on a machine you may not be looking at."""
    if not handle:
        return False
    subprocess.run(_orca_cmd([*args, "--terminal", handle], env),
                   capture_output=True, timeout=15)
    return True


def focus_terminal(handle, env=None):
    send_to_terminal(["terminal", "switch"], handle, env)
    if not env:     # raising the local app can't help you see another machine
        subprocess.Popen(["open", "-a", "Orca"])


def interrupt_terminal(handle, env=None):
    send_to_terminal(["terminal", "send", "--interrupt"], handle, env)


ESC = "\033"


def deny_terminal(handle, env=None):
    """Escape out of whatever modal is up — codex maps esc to "No, and tell Codex
    what to do differently", so this is a refusal, not a kill."""
    send_to_terminal(["terminal", "send", "--text", ESC], handle, env)


def open_changed(worktree_id, env=None):
    """Open every git-changed file for a worktree as diffs in Orca's editor —
    "it says it's done" straight to reviewing what it actually wrote."""
    if worktree_id:
        subprocess.run(_orca_cmd(["file", "open-changed", "--mode", "diff",
                                  "--worktree", f"id:{worktree_id}"], env),
                       capture_output=True, timeout=15)
        if not env:
            subprocess.Popen(["open", "-a", "Orca"])


# The auto-approver is NOT a child of this process. It runs under its own
# LaunchAgent and reads ARMED_FILE every poll; arming here only writes that file.
# It used to be a child — until Elgato's app grabbed the USB back, the deck
# crash-looped, and blind approval died with it while nobody was watching. A
# remote control must not be life support for the thing it controls.
#
# How long a single arming lasts. None = until you disarm it; the deck's status
# key cycles off -> 30m -> 1h -> forever -> off, so the timed options stay the
# ones you reach first — blind approval is a mode you WILL forget you left on.
AUTO_DURATIONS = (30, 60, None)
ARMED_FILE = pathlib.Path.home() / ".orca-streamdeck-armed"
FOREVER = float("inf")      # an expiry that never arrives, so "forever" needs no
                            # special case in the compare or on disk


def duration_label(minutes):
    return "Forever" if minutes is None else (
        f"{minutes} min" if minutes < 60 else f"{minutes // 60} hour")


def disarm_autoapprove():
    """Stand the auto-approver down. Returns (None, None) for (expiry, scope)."""
    ARMED_FILE.unlink(missing_ok=True)
    return None, None


def arm_autoapprove(minutes, now, idx=0, only=None):
    """Arm for `minutes` (None = forever), optionally scoped to one terminal
    handle. Returns (expiry epoch, scope). Writing the file IS the arming — the
    daemon picks it up on its next poll, whether or not this deck survives."""
    until = FOREVER if minutes is None else now + minutes * 60
    ARMED_FILE.write_text(f"{until} {idx} {only}" if only else f"{until} {idx}")
    return until, only


def cycle_autoapprove(idx, now):
    """What holding the status key does: step to the next duration, wrapping back
    to off after the last one. Returns (expiry, scope, duration index)."""
    idx = -1 if idx >= len(AUTO_DURATIONS) - 1 else idx + 1
    if idx < 0:
        return (*disarm_autoapprove(), idx)
    return (*arm_autoapprove(AUTO_DURATIONS[idx], now, idx), idx)


def armed_state(now):
    """(expiry, duration index, scoped handle or None), or None when disarmed or
    lapsed. The single source of truth for whether anything is auto-approving."""
    try:
        parts = ARMED_FILE.read_text().split()
        until, idx = float(parts[0]), int(parts[1])
        only = parts[2] if len(parts) > 2 else None
    except (OSError, ValueError, IndexError):
        return None
    return (until, idx, only) if until > now else None


# --- Rendering -------------------------------------------------------------

def render_tile(deck, item, now_ms=0, wash=None):
    img = PILHelper.create_image(deck)
    draw = ImageDraw.Draw(img)
    w, h = img.size
    if item is None:
        if wash:    # empty keys glow with the fleet's worst state, so the unit
            draw.rectangle([0, 0, w, h],     # reads from across the room
                           fill=tuple(round(c * 0.35) for c in wash))
        return PILHelper.to_native_format(deck, img)
    _, color, label = STATUS.get(item["state"], DEFAULT)
    fg = text_color(color)
    draw.rectangle([0, 0, w, h], fill=color)
    if SHOW_ICONS:
        # state color stays the frame/background; icon centered, name below.
        icon = icon_image(item, round(min(w, h) * 0.52))
        img.paste(icon, ((w - icon.width) // 2, round(h * 0.06)), icon)
        draw.text((w // 2, h - h * 0.12), item["repo"][:12],
                  font=load_font(round(w * 0.13)), anchor="mm", fill=fg)
    else:
        sub_fg = tuple(int(c * 0.55 + f * 0.45) for c, f in zip(color, fg))  # muted
        draw.text((w // 2, h // 2 - h * 0.15), item["repo"][:11],
                  font=load_font(round(w * 0.19)), anchor="mm", fill=fg)
        draw.text((w // 2, h // 2 + h * 0.07), (item["sub"] or "")[:14],
                  font=load_font(round(w * 0.13)), anchor="mm", fill=sub_fg)
        draw.text((w // 2, h - h * 0.13), label, font=load_font(round(w * 0.13)),
                  anchor="mm", fill=fg)
    # Corner marks, each short enough to stay legible on a Mini key: age top-right,
    # linked PR top-left, and an unread dot bottom-right.
    corner = load_font(round(w * 0.12))
    age = age_label(item.get("last_output"), now_ms)
    if age:
        draw.text((w - w * 0.04, h * 0.04), age, anchor="ra", font=corner, fill=fg)
    if item.get("pr"):
        draw.text((w * 0.04, h * 0.04), f"#{item['pr']}", anchor="la", font=corner, fill=fg)
    if item.get("unread"):
        r = max(2, w * 0.045)
        draw.ellipse([w - w * 0.05 - 2 * r, h - h * 0.05 - 2 * r,
                      w - w * 0.05, h - h * 0.05], fill=fg)
    if item.get("env"):     # lives on another machine — stripe down the right edge
        draw.rectangle([w - max(3, w * 0.06), 0, w, h], fill=group_color(item["env"]))
    if GROUP_ACCENT and item.get("group"):
        draw.rectangle([0, 0, max(3, w * 0.09), h], fill=group_color(item["group"]))
    return PILHelper.to_native_format(deck, img)


# --- Codex approval modals --------------------------------------------------
# Pure detection, shared by the auto-approver (which presses Enter on these) and
# the agent page (which shows you what an agent is stuck on). Headers + the shared
# confirm footer, lifted from the codex 0.145.0 binary's string table; the footer
# catches modal kinds not listed, the headers survive a redraw clipping the footer.
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
    """What is being said yes to — the modal's kind plus the text it asks about.
    "approved <handle>" alone leaves no audit trail of what a blind daemon agreed
    to on your behalf, and the agent page needs the same string on its key."""
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


# --- Agent page: tap a tile to drill into one agent -------------------------
# Each entry: (label, subtitle-key, color). The subtitle is filled in per agent.
AGENT_PAGE_IDLE = 30.0      # seconds before the deck falls back to the fleet
ACTIONS = ("focus", "approve", "auto", "interrupt", "diffs")
ACTION_LOOK = {
    "focus":     ("FOCUS", (55, 90, 150)),
    "approve":   ("APPRV", (35, 130, 70)),
    "auto":      ("AUTO", (180, 120, 20)),
    "interrupt": ("INTR", (150, 45, 45)),
    "diffs":     ("DIFFS", (70, 70, 80)),
}


def render_action(deck, label, sub="", color=(60, 60, 66), enabled=True):
    """One key on the agent page: a verb, and what it will do right now."""
    img = PILHelper.create_image(deck)
    draw = ImageDraw.Draw(img)
    w, h = img.size
    if not enabled:
        color = tuple(round(c * 0.3) for c in color)
    draw.rectangle([0, 0, w, h], fill=color)
    fg = text_color(color) if enabled else (120, 120, 126)
    draw.text((w // 2, h // 2 - (h * 0.12 if sub else 0)), label,
              font=load_font(round(w * 0.19)), anchor="mm", fill=fg)
    if sub:
        draw.text((w // 2, h // 2 + h * 0.18), sub[:12],
                  font=load_font(round(w * 0.12)), anchor="mm", fill=fg)
    return PILHelper.to_native_format(deck, img)


def action_state(name, item, ask, auto_only):
    """(subtitle, enabled) for one action key, given the agent it's aimed at."""
    if name == "approve":
        return (ask or "nothing", bool(ask))
    if name == "auto":
        return ("on" if auto_only == item["handle"] else "this one",
                item.get("agent_type") == "codex")
    if name == "interrupt":
        return ("", bool(item["handle"]))
    if name == "diffs":
        return ("", bool(item.get("worktree_id")))
    return ("", True)


def render_agent_page(deck, item, n, nav_key, ask, auto_only, now_ms):
    """The whole drill-down: one key per action, status key becomes Back."""
    images = {}
    for k in range(n):
        if k == nav_key:
            images[k] = render_action(deck, "BACK", item["repo"][:11], (40, 42, 50))
        elif k < len(ACTIONS):
            name = ACTIONS[k]
            label, color = ACTION_LOOK[name]
            sub, enabled = action_state(name, item, ask, auto_only)
            images[k] = render_action(deck, label, sub, color, enabled)
        else:
            images[k] = render_tile(deck, None)
    return images


def auto_badge(until, now):
    """Status-key badge for the auto-approver: how much longer it stays armed.
    Blank when off. Time remaining beats the word "on" — the whole point of the
    window is knowing it's closing."""
    if not until:
        return ""
    if until == FOREVER:
        return "AUTO ON"
    return "AUTO " + age_label(now * 1000, until * 1000)   # elapsed helper, run backwards


def render_status(deck, count, page, pages, down=False, auto=""):
    img = PILHelper.create_image(deck)
    draw = ImageDraw.Draw(img)
    w, h = img.size
    if down:
        draw.rectangle([0, 0, w, h], fill=(200, 40, 40))
        draw.text((w // 2, h // 2), "ORCA\nDOWN", font=load_font(round(w * 0.16)),
                  anchor="mm", align="center", fill=(255, 255, 255))
        return PILHelper.to_native_format(deck, img)
    draw.rectangle([0, 0, w, h], fill=(200, 40, 40) if count else (40, 42, 50))
    if count:
        draw.text((w // 2, h // 2 - h * 0.12), str(count),
                  font=load_font(round(w * 0.4)), anchor="mm", fill=(255, 255, 255))
        draw.text((w // 2, h - h * 0.18), "NEED YOU", font=load_font(round(w * 0.13)),
                  anchor="mm", fill=(255, 255, 255))
    else:
        draw.text((w // 2, h // 2), "all clear", font=load_font(round(w * 0.15)),
                  anchor="mm", fill=(150, 155, 165))
    if pages > 1:
        draw.text((w - w * 0.02, h * 0.06), f"{page + 1}/{pages}",
                  font=load_font(round(w * 0.12)), anchor="ra", fill=(230, 230, 235))
    if auto:  # codex auto-approve is armed — amber, the same "hands off" hue
        draw.text((w * 0.04, h * 0.06), auto, font=load_font(round(w * 0.12)),
                  anchor="la", fill=(240, 170, 40))
    return PILHelper.to_native_format(deck, img)


# --- Main loop -------------------------------------------------------------

def repaint(deck, state, n, nav_key):
    """Draw the current page from the items already in `state` and return the
    needs-you count. Split out from the poll so a page flip repaints instantly
    instead of waiting on a fresh round of `orca` calls. Callers hold the lock."""
    items = state["items"]
    now = time.time()
    count = sum(1 for it in items if it["state"] in NEEDS_HUMAN)
    armed = armed_state(now)                    # the daemon's own source of truth
    if state["focused"]:                        # drilled into one agent
        for k, img in render_agent_page(deck, state["focused"], n, nav_key,
                                        state["focused_ask"],
                                        armed[2] if armed else None,
                                        now * 1000).items():
            deck.set_key_image(k, img)
        return count

    pages = paginate(items, n)
    page = state["page"] % len(pages)
    page_items = pages[page]
    badge = auto_badge(armed[0] if armed else None, now)
    # Worst state in the fleet washes the empty keys — only when it's something
    # you'd want to notice, so a calm fleet stays dark.
    worst = min((STATUS.get(it["state"], DEFAULT) for it in items),
                key=lambda s: s[0], default=DEFAULT)
    wash = worst[1] if worst[0] <= STATUS["waiting"][0] else None
    slots = [None] * n
    for k in range(n):
        if k == nav_key:
            deck.set_key_image(k, render_status(deck, count, page, len(pages),
                                                auto=badge))
        elif k < len(page_items):
            slots[k] = page_items[k]
            deck.set_key_image(k, render_tile(deck, page_items[k], now * 1000))
        else:
            deck.set_key_image(k, render_tile(deck, None, wash=wash))
    state.update(page=page, pages=len(pages), slots=slots)
    return count


ASK_KINDS = ((" command", "command"), (" edits", "edits"), ("permissions", "perms"),
             ("network", "network"), ("approval", "tool"))


def ask_label(description):
    """Squeeze describe()'s sentence into the handful of characters a key holds."""
    low = description.lower()
    for needle, short in ASK_KINDS:
        if needle in low:
            return short
    return "approve?"


def urgent_first(items):
    """The agent that most needs you, or None when the fleet is calm."""
    blocked = [it for it in items if it["state"] in NEEDS_HUMAN
               or it["state"] == "interrupted"]
    return min(blocked, key=urgency_key, default=None)


def page_of(items, key_count, item):
    """Which page an item lands on, so the deck can jump straight there."""
    if not item:
        return 0
    pages = paginate(items, key_count)
    for i, page in enumerate(pages):
        if any(it["id"] == item["id"] for it in page):
            return i
    return 0


def refresh_focus(state, items):
    """Keep the open agent page pointing at live data: re-bind it to this poll's
    item, drop it once it's gone or idle, and refresh what it's waiting on."""
    focused = state["focused"]
    if not focused:
        return
    if time.monotonic() - state["focused_at"] > AGENT_PAGE_IDLE:
        state.update(focused=None, focused_ask="")
        return
    fresh = next((it for it in items if it["id"] == focused["id"]), None)
    if fresh is None:                   # the agent went away while you were in it
        state.update(focused=None, focused_ask="")
        return
    state["focused"] = fresh
    ask = ""
    if fresh.get("agent_type") == "codex" and is_blocked(fresh.get("title")):
        tail = read_tail(fresh["handle"], fresh.get("env"))
        if wants_approval(tail, fresh["title"]):
            ask = ask_label(describe(tail))
    state["focused_ask"] = ask


def agent_action(state, act, item, held, run):
    """Perform one agent-page action and return the state changes it implies.
    `run` fires the orca call off-thread so the deck stays responsive."""
    handle, env = item["handle"], item.get("env")
    if act == "focus":
        run(focus_terminal, handle, env)
        return {"focused": None}                # you're leaving the deck anyway
    if act == "interrupt":
        run(interrupt_terminal, handle, env)
    elif act == "diffs":
        run(open_changed, item.get("worktree_id"), env)
        return {"focused": None}
    elif act == "approve":
        # tap says yes to what's on screen, hold escapes out of it instead
        run(deny_terminal if held >= LONG_PRESS_SEC else send_to_terminal_enter,
            handle, env)
    elif act == "auto":
        armed = armed_state(time.time())
        if armed and armed[2] == handle:        # already trusting this one -> stop
            disarm_autoapprove()
            return {"auto_idx": -1}
        arm_autoapprove(AUTO_DURATIONS[0], time.time(), 0, only=handle)
        return {"auto_idx": -1}                 # scoped, so not a fleet duration
    return {}


def send_to_terminal_enter(handle, env=None):
    """Press Enter — the same key the auto-approver sends, but because you asked."""
    send_to_terminal(["terminal", "send", "--enter"], handle, env)


def main():
    decks = DeviceManager().enumerate()
    if not decks:
        raise SystemExit("No Stream Deck found. Is Elgato's app still holding it?")
    deck = decks[0]
    deck.open()
    deck.reset()
    n = deck.key_count()
    nav_key = n - 1
    print(f"Connected: {deck.deck_type()} ({n} keys)")

    lock = threading.Lock()
    wake = threading.Event()
    # slots[k] = item or None; auto_idx = which AUTO_DURATIONS entry the status
    # key last selected (-1 = off). The armed window itself lives in ARMED_FILE.
    state = {"page": 0, "pages": 1, "slots": [None] * n, "items": [],
             "auto_idx": -1,
             # focused = the agent whose page is open; focused_ask = what it's
             # waiting on, refreshed by the poll loop while that page is up
             "focused": None, "focused_at": 0.0, "focused_ask": ""}
    press_at = {}  # key -> monotonic time of key-down

    resumed = armed_state(time.time())
    if resumed:                                 # the daemon is already acting on it
        state["auto_idx"] = resumed[1]
        print(f"Codex auto-approve is {auto_badge(resumed[0], time.time())}")

    def on_press(_deck, key, pressed):
        if pressed:
            press_at[key] = time.monotonic()
            return
        held = time.monotonic() - press_at.pop(key, time.monotonic())
        run = lambda fn, *a: threading.Thread(target=fn, args=a, daemon=True).start()
        with lock:
            focused = state["focused"]
            if focused:                         # --- agent page ---
                state["focused_at"] = time.monotonic()
                if key == nav_key or key >= len(ACTIONS):
                    state["focused"] = None     # Back
                else:
                    act = ACTIONS[key]
                    state.update(**agent_action(state, act, focused, held, run))
                repaint(deck, state, n, nav_key)
                wake.set()
                return

            if key == nav_key:                  # --- fleet: status key ---
                if held >= LONG_PRESS_SEC:      # hold = next auto-approve duration
                    *_, state["auto_idx"] = cycle_autoapprove(
                        state["auto_idx"], time.time())
                elif state["pages"] > 1:
                    state["page"] = (state["page"] + 1) % state["pages"]
                repaint(deck, state, n, nav_key)   # don't wait for the next poll
                wake.set()
                return

            item = state["slots"][key] if key < n else None
            if item and held < LONG_PRESS_SEC:  # tap a tile = open its page
                state.update(focused=item, focused_at=time.monotonic(),
                             focused_ask="")
                repaint(deck, state, n, nav_key)
                wake.set()
                return
        if item and item["handle"]:             # hold a tile = interrupt, as before
            run(interrupt_terminal, item["handle"], item.get("env"))

    deck.set_key_callback(on_press)

    pulse_on = False
    last_count = 0      # needs-you count from the previous poll, for the 0 -> N jump
    try:
        while True:
            items = fetch_items()
            pulse_on = not pulse_on

            with lock:
                if items is None:
                    state.update(page=0, pages=1, slots=[None] * n, items=[])
                    for k in range(n):
                        deck.set_key_image(k, render_status(deck, 0, 0, 1, down=True)
                                           if k == nav_key else render_tile(deck, None))
                    deck.set_brightness(PULSE[1])
                    wake.wait(POLL_SECONDS); wake.clear()
                    continue

                state["items"] = items
                refresh_focus(state, items)
                count = sum(1 for it in items if it["state"] in NEEDS_HUMAN)
                # Nothing needed you a moment ago and now something does: go to it,
                # rather than leaving you to hunt for which page it's on.
                if count and not last_count and not state["focused"]:
                    state["page"] = page_of(items, n, urgent_first(items))
                last_count = count
                repaint(deck, state, n, nav_key)
                deck.set_brightness(PULSE[pulse_on] if count else DIM_BRIGHTNESS)

            wake.wait(POLL_SECONDS)
            wake.clear()
    except KeyboardInterrupt:
        pass
    finally:
        # Nothing to tear down: the auto-approver is not ours to kill, and the
        # armed window must outlive us.
        deck.reset()
        deck.close()


if __name__ == "__main__":
    main()
