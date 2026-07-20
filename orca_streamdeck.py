#!/usr/bin/env python3
"""Orca control pane for any Elgato Stream Deck.

Polls Orca and paints one tile per *agent*, sorted by urgency
(needs input -> idle -> working) and colored by state. The bottom-right key is a
status key: shows how many agents need you, and cycles pages when there are more
agents than keys. The deck dims when nothing needs you and pulses when something
does. (Orca sends its own notifications, so this doesn't duplicate them.)

Tap a tile   -> focus that agent's terminal in Orca (and raise the app).
Hold a tile  -> interrupt that agent (Esc/Ctrl-C to its terminal). Interrupting
                is always safe; there is deliberately no blind "approve" button.

Works on any model (Mini/MK.2/XL/Plus/...) — key count, image size and fonts are
derived from the connected device. Screenless decks (Pedal) are not supported.

Author: taek <leekt216@gmail.com>
"""
import colorsys
import functools
import hashlib
import json
import subprocess
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
GROUP_FILTER = None         # a repo displayName; show only agents in THAT repo's group
GROUP_PAGES = False         # one group per page instead of urgency-flat pagination

# --- Per-project icons: an auto-generated identicon derived from the repo name,
# distinct per project (no network, no external avatars). ---
SHOW_ICONS = True
_ICON_CACHE = {}            # repo name -> base identicon PIL.Image (RGBA)


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
    """Sized identicon for a tile, cached by repo name."""
    name = item.get("repo", "?")
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

def _orca_json(args):
    try:
        out = subprocess.run(["orca", *args], capture_output=True, text=True, timeout=15)
        data = json.loads(out.stdout)
        return data.get("result") if data.get("ok") else None
    except (subprocess.SubprocessError, json.JSONDecodeError, ValueError):
        return None


def build_items(worktrees, terminals, repo_groups=None):
    """Flat, urgency-sorted list of agent tiles. Pure — no I/O, so it's testable.
    Each item: {id, repo, sub, state, handle, worktree_id, group}.
    repo_groups maps repoId -> projectGroupId (None -> group left unset)."""
    repo_groups = repo_groups or {}
    # agent paneKey ("{tabId}:{leafId}") -> terminal handle
    by_pane = {f"{t.get('tabId')}:{t.get('leafId')}": t.get("handle") for t in terminals}
    # worktreeId -> handle of its most recently active terminal (fallback focus)
    recent = {}
    for t in terminals:
        w, h, lo = t.get("worktreeId"), t.get("handle"), t.get("lastOutputAt", 0)
        if w and h and (w not in recent or lo > recent[w][0]):
            recent[w] = (lo, h)

    items = []
    for w in worktrees:
        wid, repo = w["worktreeId"], w.get("repo", "?")
        branch = w.get("displayName") or ""
        group = repo_groups.get(w.get("repoId"))
        agents = w.get("agents") or []
        if agents:
            for a in agents:
                handle = by_pane.get(a.get("paneKey")) or (recent.get(wid) or (0, None))[1]
                items.append({
                    "id": a.get("paneKey") or wid,
                    "repo": repo,
                    "sub": a.get("taskTitle") or a.get("displayName") or a.get("agentType") or branch,
                    # an interrupted/stopped agent is the "error" signal -> red.
                    "state": "interrupted" if a.get("interrupted") else a.get("state"),
                    "handle": handle,
                    "worktree_id": wid,
                    "group": group,
                })
        else:  # worktree with no agent session — show it at worktree level
            items.append({
                "id": wid, "repo": repo, "sub": branch, "state": w.get("status"),
                "handle": (recent.get(wid) or (0, None))[1], "worktree_id": wid,
                "group": group,
            })
    items.sort(key=lambda it: (STATUS.get(it["state"], DEFAULT)[0], it["id"]))
    return items


def fetch_repo_groups():
    """({repoId: groupId}, {repoName: groupId}) from `orca repo list`."""
    repos = (_orca_json(["repo", "list", "--json"]) or {}).get("repos", [])
    by_id = {r["id"]: r.get("projectGroupId") for r in repos if r.get("id")}
    by_name = {r.get("displayName"): r.get("projectGroupId") for r in repos}
    return by_id, by_name


def fetch_items():
    """Query Orca and build the tile list, or None if orca is unreachable."""
    wt = _orca_json(["worktree", "ps", "--json"])
    if wt is None:
        return None
    terms = (_orca_json(["terminal", "list", "--json"]) or {}).get("terminals", [])
    # Only pay for the repo-list query when a group feature needs it (identicons
    # come straight from the repo name, which worktree ps already carries).
    by_id, by_name = fetch_repo_groups() if (GROUP_ACCENT or GROUP_FILTER or GROUP_PAGES) else ({}, {})
    items = build_items(wt.get("worktrees", []), terms, by_id)
    if GROUP_FILTER:
        target = by_name.get(GROUP_FILTER)
        items = [it for it in items if it.get("group") == target]
    return items


def paginate(items, key_count):
    """Pages of items; the last key is always reserved for the status key."""
    per = max(1, key_count - 1)
    pages = [items[i:i + per] for i in range(0, len(items), per)] or [[]]
    return pages


def paginate_grouped(items, key_count):
    """Like paginate, but never mixes groups on a page. Groups appear in order of
    their most-urgent agent (items arrive urgency-sorted)."""
    per = max(1, key_count - 1)
    order, buckets = [], {}
    for it in items:
        g = it.get("group")
        if g not in buckets:
            buckets[g] = []
            order.append(g)
        buckets[g].append(it)
    pages = []
    for g in order:
        b = buckets[g]
        pages += [b[i:i + per] for i in range(0, len(b), per)]
    return pages or [[]]


# --- Actions ---------------------------------------------------------------

def focus_terminal(handle):
    if handle:
        subprocess.run(["orca", "terminal", "switch", "--terminal", handle],
                       capture_output=True, timeout=15)
    subprocess.Popen(["open", "-a", "Orca"])


def interrupt_terminal(handle):
    subprocess.run(["orca", "terminal", "send", "--terminal", handle, "--interrupt"],
                   capture_output=True, timeout=15)


# --- Rendering -------------------------------------------------------------

def render_tile(deck, item):
    img = PILHelper.create_image(deck)
    draw = ImageDraw.Draw(img)
    w, h = img.size
    if item is None:
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
    if GROUP_ACCENT and item.get("group"):
        draw.rectangle([0, 0, max(3, w * 0.09), h], fill=group_color(item["group"]))
    return PILHelper.to_native_format(deck, img)


def render_status(deck, count, page, pages, down=False):
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
    return PILHelper.to_native_format(deck, img)


# --- Main loop -------------------------------------------------------------

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
    state = {"page": 0, "pages": 1, "slots": [None] * n}  # slots[k] = item or None
    press_at = {}  # key -> monotonic time of key-down

    def on_press(_deck, key, pressed):
        if pressed:
            press_at[key] = time.monotonic()
            return
        held = time.monotonic() - press_at.pop(key, time.monotonic())
        with lock:
            if key == nav_key:
                if state["pages"] > 1:
                    state["page"] = (state["page"] + 1) % state["pages"]
                    wake.set()
                return
            item = state["slots"][key] if key < n else None
        if not item:
            return
        action = interrupt_terminal if (held >= LONG_PRESS_SEC and item["handle"]) else focus_terminal
        threading.Thread(target=action, args=(item["handle"],), daemon=True).start()

    deck.set_key_callback(on_press)

    pulse_on = False
    try:
        while True:
            items = fetch_items()
            pulse_on = not pulse_on

            with lock:
                if items is None:
                    state.update(page=0, pages=1, slots=[None] * n)
                    for k in range(n):
                        deck.set_key_image(k, render_status(deck, 0, 0, 1, down=True)
                                           if k == nav_key else render_tile(deck, None))
                    deck.set_brightness(PULSE[1])
                    wake.wait(POLL_SECONDS); wake.clear()
                    continue

                count = sum(1 for it in items if it["state"] in NEEDS_HUMAN)
                pages = paginate_grouped(items, n) if GROUP_PAGES else paginate(items, n)
                page = state["page"] % len(pages)
                page_items = pages[page]
                slots = [None] * n
                for k in range(n):
                    if k == nav_key:
                        deck.set_key_image(k, render_status(deck, count, page, len(pages)))
                    elif k < len(page_items):
                        slots[k] = page_items[k]
                        deck.set_key_image(k, render_tile(deck, page_items[k]))
                    else:
                        deck.set_key_image(k, render_tile(deck, None))
                state.update(page=page, pages=len(pages), slots=slots)
                deck.set_brightness(PULSE[pulse_on] if count else DIM_BRIGHTNESS)

            wake.wait(POLL_SECONDS)
            wake.clear()
    except KeyboardInterrupt:
        pass
    finally:
        deck.reset()
        deck.close()


if __name__ == "__main__":
    main()
