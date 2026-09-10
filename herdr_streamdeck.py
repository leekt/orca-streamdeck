#!/usr/bin/env python3
"""Stream Deck controls for agents in a configured Herdr session."""
import json
import math
import os
import pathlib
import re
import signal
import tempfile
import threading
import time

from StreamDeck.DeviceManager import DeviceManager
from herdr_backend import FleetBackend, HerdrError
from herdr_ui import (DEFAULT, NEEDS_HUMAN, STATUS, auto_badge, render_action,
                      render_status, render_tile)

POLL_SECONDS = 2.0
LONG_PRESS_SEC = 0.7
DIM_BRIGHTNESS = 100
PULSE = (60, 100)
AGENT_PAGE_IDLE = 30.0
AUTO_DURATIONS = (30, 60, None)
ARMED_FILE = pathlib.Path.home() / ".herdr-streamdeck-armed"
FOREVER = float("inf")
_backend = FleetBackend()
_last_error = None
_last_connections = None
_action_lock = threading.Lock()


def urgency_key(item):
    return (STATUS.get(item["state"], DEFAULT)[0], item.get("state_since") or 0,
            item["id"])


def fetch_items():
    global _last_error, _last_connections
    try:
        items = _backend.fetch_items()
        items.sort(key=urgency_key)
        connections = tuple((machine["label"], machine["state"], machine["error"])
                            for machine in _backend.machine_status)
        if connections != _last_connections:
            for label, state, error in connections:
                print(f"Herdr {label}: {state}" + (f" — {error}" if error else ""), flush=True)
            _last_connections = connections
        if _last_error:
            print("Herdr connected", flush=True)
        _last_error = None
        return items
    except HerdrError as exc:
        message = str(exc)
        if message != _last_error:
            print(f"Herdr unavailable: {message}", flush=True)
        _last_error = message
        return None


def paginate(items, key_count):
    per = max(1, key_count - 1)
    return [items[i:i + per] for i in range(0, len(items), per)] or [[]]


def page_of(items, key_count, item):
    for i, page in enumerate(paginate(items, key_count)):
        if item and any(it["id"] == item["id"] for it in page):
            return i
    return 0


def urgent_first(items):
    return min((it for it in items if it["state"] in NEEDS_HUMAN),
               key=urgency_key, default=None)


def run_action(fn, *args):
    def action():
        with _action_lock:
            try:
                fn(*args)
            except (HerdrError, OSError, ValueError) as exc:
                print(f"Action failed: {exc}", flush=True)
    return threading.Thread(target=action, daemon=True).start()


def focus_terminal(item):
    return _backend.focus(item)


def interrupt_terminal(item):
    return _backend.interrupt(item)


def open_changed(item):
    return _backend.open_changed(item)


def duration_label(minutes):
    return "Forever" if minutes is None else (
        f"{minutes} min" if minutes < 60 else f"{minutes // 60} hour")


def disarm_autoapprove():
    ARMED_FILE.unlink(missing_ok=True)
    return None, None


def arm_autoapprove(minutes, now, idx=0, only=None):
    until = FOREVER if minutes is None else now + minutes * 60
    data = {"session": _backend.session, "until": None if minutes is None else until,
            "index": idx, "only": only}
    with tempfile.NamedTemporaryFile(mode="w", dir=ARMED_FILE.parent,
                                     prefix=".herdr-arm-", delete=False) as f:
        temporary = pathlib.Path(f.name)
        try:
            json.dump(data, f)
            f.flush()
            os.replace(temporary, ARMED_FILE)
        finally:
            temporary.unlink(missing_ok=True)
    return until, only


def cycle_autoapprove(idx, now, only=None):
    idx = -1 if idx >= len(AUTO_DURATIONS) - 1 else idx + 1
    if idx < 0:
        return (*disarm_autoapprove(), idx)
    return (*arm_autoapprove(AUTO_DURATIONS[idx], now, idx, only=only), idx)


def armed_state(now):
    try:
        data = json.loads(ARMED_FILE.read_text())
        if data["session"] != _backend.session:
            return None
        raw_until = data["until"]
        until = FOREVER if raw_until is None else float(raw_until)
        idx, only = data["index"], data.get("only")
        if type(idx) is not int or not 0 <= idx < len(AUTO_DURATIONS):
            return None
        if only is not None and (not isinstance(only, str) or not only):
            return None
        if raw_until is not None and not math.isfinite(until):
            return None
        return (until, idx, only) if until > now else None
    except (OSError, ValueError, TypeError, KeyError):
        return None


def arm_covers(armed, item):
    """Session-wide approval remains local; remote approval requires its tile."""
    if not armed:
        return False
    if armed[2] is not None:
        return armed[2] == item["id"]
    return item.get("machine_id", "local") == "local"


MARKERS = (
    "Press enter to confirm", "Would you like to run the following command?",
    "Would you like to make the following edits?",
    "Would you like to grant these permissions?",
    "Do you want to approve network access to", "needs your approval.",
)


def _squash(text):
    return "".join(text.split()).lower()


def wants_approval(tail, state):
    flat = _squash(tail)
    return (state == "blocked" and "1.yes" in flat and
            "entertosubmitanswer" not in flat and
            any(_squash(marker) in flat for marker in MARKERS))


def selected_once(tail):
    """Auto mode only confirms a visibly selected one-time Yes option."""
    match = re.search(r"(?m)^[ \t]*[›❯>][ \t]*1[.)][ \t]*(.+)$", tail)
    if not match:
        return False
    choice = " ".join(match.group(1).lower().split())
    return choice in {"yes", "yes (y)", "yes, proceed", "yes, proceed (y)"}


def describe(tail):
    for marker in MARKERS[1:]:
        if _squash(marker) in _squash(tail):
            return marker.rstrip("?.")
    return "command approval"


def ask_label(description):
    for word, label in [("command", "command"), ("edits", "edits"),
                        ("permissions", "perms"), ("network", "network")]:
        if word in description.lower():
            return label
    return "tool"


def approval_details(item):
    snapshot = _backend.approval_snapshot(item)
    if snapshot and wants_approval(snapshot[0], "blocked"):
        return snapshot
    return None


def answer_approval(item, deny=False, automatic=False, expected=None):
    snapshot = approval_details(item)
    if not snapshot or (expected is not None and snapshot != expected):
        return False
    if automatic and not selected_once(snapshot[0]):
        return False
    _backend.send_keys(item, "esc" if deny else "enter", blocked_seq=snapshot[1])
    return True


ACTIONS = ("focus", "approve", "auto", "interrupt", "diffs")
ACTION_LOOK = {
    "focus": ("FOCUS", (55, 90, 150)), "approve": ("APPRV", (35, 130, 70)),
    "auto": ("AUTO", (180, 120, 20)), "interrupt": ("INTR", (150, 45, 45)),
    "diffs": ("DIFFS", (70, 70, 80)),
}


def action_state(name, item, ask, armed):
    if not item or not item.get("pane_id"):
        return "unavailable", False
    if name == "approve":
        return ask or "nothing", bool(ask)
    if name == "auto":
        label = "off"
        if arm_covers(armed, item):
            label = "forever" if armed[0] == FOREVER else auto_badge(armed[0], time.time())[5:]
            if armed[2] is None:
                label = "all " + label
        return label, item.get("agent_type") == "codex" and bool(item.get("agent_session"))
    if name == "interrupt":
        return "", item["state"] in {"working", "blocked"}
    if name == "diffs":
        return "", bool(item.get("cwd"))
    return "", True


def agent_action(state, act, item, held, run):
    armed = armed_state(time.time())
    _, enabled = action_state(act, item, state.get("focused_ask", ""), armed)
    if not enabled:
        return {}
    if act == "focus":
        run(focus_terminal, item)
        return {"focused": None}
    if act == "interrupt":
        run(interrupt_terminal, item)
    elif act == "diffs":
        run(open_changed, item)
        return {"focused": None}
    elif act == "approve":
        run(answer_approval, item, held >= LONG_PRESS_SEC)
    elif act == "auto":
        scoped = bool(armed and armed[2] == item["id"])
        if held >= LONG_PRESS_SEC:
            cycle_autoapprove(armed[1] if scoped else -1, time.time(), only=item["id"])
        elif scoped:
            disarm_autoapprove()
        else:
            arm_autoapprove(None, time.time(), AUTO_DURATIONS.index(None), only=item["id"])
        return {"auto_idx": -1}
    return {}


def refresh_focus(state, items):
    focused = state["focused"]
    if not focused:
        return
    fresh = next((it for it in items if it["id"] == focused["id"]), None)
    if fresh is None or time.monotonic() - state["focused_at"] > AGENT_PAGE_IDLE:
        state.update(focused=None, focused_ask="")
        return
    state.update(focused=fresh, focused_ask="")
    if fresh["agent_type"] == "codex" and fresh["state"] == "blocked":
        try:
            approval = approval_details(fresh)
            if approval:
                state["focused_ask"] = ask_label(describe(approval[0]))
        except HerdrError:
            pass


def repaint(deck, state, n, nav_key):
    items = state["items"]
    now = time.time()
    count = sum(it["state"] in NEEDS_HUMAN for it in items)
    armed = armed_state(now)
    if state["focused"]:
        item = state["focused"]
        for key in range(n):
            if key == nav_key:
                image = render_action(deck, "BACK", item["label"][:11], (40, 42, 50))
            elif key < len(ACTIONS):
                name = ACTIONS[key]
                label, color = ACTION_LOOK[name]
                sub, enabled = action_state(name, item, state["focused_ask"], armed)
                image = render_action(deck, label, sub, color, enabled)
            else:
                image = render_tile(deck, None)
            deck.set_key_image(key, image)
        return count
    pages = paginate(items, n)
    page = state["page"] % len(pages)
    slots = [None] * n
    for key in range(n):
        if key == nav_key:
            image = render_status(deck, count, page, len(pages),
                                  auto=auto_badge(armed[0] if armed else None, now),
                                  machines=_backend.machine_status)
        elif key < len(pages[page]):
            slots[key] = pages[page][key]
            image = render_tile(deck, slots[key], now * 1000)
        else:
            image = render_tile(deck, None, wash=STATUS["blocked"][1] if count else None)
        deck.set_key_image(key, image)
    state.update(page=page, pages=len(pages), slots=slots)
    return count


def disconnected(state, n):
    state.update(page=0, pages=1, slots=[None] * n, items=[], focused=None, focused_ask="")


def main():
    decks = DeviceManager().enumerate()
    if not decks:
        raise SystemExit("No Stream Deck found. Is Elgato's app holding the device?")
    deck = next((d for d in decks if d.is_visual()), None)
    if deck is None:
        raise SystemExit("A Stream Deck with screens is required")
    deck.open()
    deck.reset()
    n = deck.key_count()
    if n < 6:
        deck.close()
        raise SystemExit("At least six display keys are required for the action page")
    nav_key = n - 1
    print(f"Connected: {deck.deck_type()} ({n} keys), Herdr session {_backend.session}", flush=True)
    lock, wake, stop = threading.Lock(), threading.Event(), threading.Event()
    state = {"page": 0, "pages": 1, "slots": [None] * n, "items": [],
             "auto_idx": -1, "focused": None, "focused_at": 0.0, "focused_ask": ""}
    press_at = {}

    def shutdown(*_):
        stop.set()
        wake.set()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    def on_press(_deck, key, pressed):
        with lock:
            if pressed:
                # Capture at key-down; polling may reorder tiles during a hold.
                press_at[key] = (time.monotonic(), state["focused"], state["slots"][key])
                return
            pressed_info = press_at.pop(key, None)
            if pressed_info is None:
                return
            started, focused, item = pressed_info
            held = time.monotonic() - started
            if focused:
                if not state["focused"] or focused["id"] != state["focused"]["id"]:
                    return
                state["focused_at"] = time.monotonic()
                if key == nav_key or key >= len(ACTIONS):
                    state["focused"] = None
                else:
                    state.update(**agent_action(state, ACTIONS[key], state["focused"], held, run_action))
            elif key == nav_key:
                if held >= LONG_PRESS_SEC:
                    armed = armed_state(time.time())
                    idx = armed[1] if armed and not armed[2] else -1
                    *_, state["auto_idx"] = cycle_autoapprove(idx, time.time())
                elif state["pages"] > 1:
                    state["page"] = (state["page"] + 1) % state["pages"]
            elif item:
                current = next((it for it in state["items"] if it["id"] == item["id"]), None)
                if current is None:
                    return
                if held < LONG_PRESS_SEC:
                    run_action(focus_terminal, current)
                else:
                    state.update(focused=current, focused_at=time.monotonic(), focused_ask="")
            repaint(deck, state, n, nav_key)
            wake.set()

    deck.set_key_callback(on_press)
    pulse_on, last_count = False, 0
    try:
        while not stop.is_set():
            items = fetch_items()
            with lock:
                focused_state = dict(state)
            if items is not None:
                refresh_focus(focused_state, items)
            with lock:
                if items is None:
                    disconnected(state, n)
                    last_count = 0
                    for key in range(n):
                        deck.set_key_image(key, render_status(deck, 0, 0, 1, down=True)
                                           if key == nav_key else render_tile(deck, None))
                    deck.set_brightness(PULSE[1])
                else:
                    state["items"] = items
                    if state["focused_at"] == focused_state["focused_at"]:
                        state.update(focused=focused_state["focused"], focused_ask=focused_state["focused_ask"])
                    count = sum(it["state"] in NEEDS_HUMAN for it in items)
                    if count and not last_count and not state["focused"]:
                        state["page"] = page_of(items, n, urgent_first(items))
                    last_count = count
                    repaint(deck, state, n, nav_key)
                    pulse_on = not pulse_on
                    deck.set_brightness(PULSE[pulse_on] if count else DIM_BRIGHTNESS)
            wake.wait(POLL_SECONDS)
            wake.clear()
    finally:
        _backend.close()
        deck.reset()
        deck.close()


if __name__ == "__main__":
    main()
