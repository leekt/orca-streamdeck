#!/usr/bin/env python3
"""Orca control pane as a macOS menu bar app.

Shares its brains with the Stream Deck controller: the title shows how many
agents need you, and the dropdown lists every agent by urgency, each with its
linked PR and how long it's been quiet. Per agent: Focus (jump to its terminal
in Orca) and Interrupt (Esc/Ctrl-C). At the bottom, the same codex auto-approve
toggle the deck's status key carries. Updates every POLL_SECONDS — it's a
running process, not a rate-limited WidgetKit widget.

Run: ./.venv/bin/python orca_menubar.py

Author: taek <leekt216@gmail.com>
"""
import atexit
import threading
import time

import rumps

import orca_streamdeck as core

GLYPH = {"interrupted": "🔴", "permission": "🟠", "waiting": "🟠",
         "working": "🔵", "done": "🟢", "active": "⚪"}
IDLE_TITLE = "🐳"


class OrcaBar(rumps.App):
    def __init__(self):
        super().__init__(IDLE_TITLE, quit_button="Quit")
        self._sig = None
        self._auto, self._auto_until, self._auto_idx = None, None, -1
        atexit.register(self._stop_auto)     # never outlive the app
        rumps.Timer(self.refresh, core.POLL_SECONDS).start()

    def _act(self, fn, *args):
        # Run the orca call off the UI thread so the menu stays responsive.
        return lambda _: threading.Thread(target=fn, args=args, daemon=True).start()

    @property
    def _auto_on(self):
        return bool(self._auto and self._auto.poll() is None)

    def _stop_auto(self):
        self._auto, self._auto_until = core.disarm_autoapprove(self._auto)
        self._auto_idx = -1

    def _pick_auto(self, idx):
        """Choose a duration — or turn it off by picking the one already running."""
        def chosen(_):
            if self._auto_on and self._auto_idx == idx:
                self._stop_auto()
            else:
                self._auto, self._auto_until = core.arm_autoapprove(
                    self._auto, core.AUTO_DURATIONS[idx], time.time(), idx)
                self._auto_idx = idx
            self._sig = None        # force a rebuild so the checkmark updates
        return chosen

    def refresh(self, _):
        items = core.fetch_items()
        if items is None:
            self.title = "🐳⚠️"
            if self._sig != "down":
                self.menu.clear()
                self.menu = ["Orca unreachable"]
                self._sig = "down"
            return

        count = sum(1 for it in items if it["state"] in core.NEEDS_HUMAN)
        self.title = f"🔴 {count}" if count else IDLE_TITLE

        # Only rebuild the menu when the agent set/states change — avoids
        # closing a submenu the user is navigating every 2s. Auto-approve expiring
        # on its own counts as a change, hence it's in the signature.
        if self._auto_until and time.time() >= self._auto_until:
            self._stop_auto()
        sig = tuple((it["id"], it["state"]) for it in items) + (self._auto_idx,)
        if sig == self._sig:
            return
        self._sig = sig

        self.menu.clear()
        now_ms = time.time() * 1000
        for it in items:
            age = core.age_label(it.get("last_output"), now_ms)
            extra = " · ".join(x for x in (f"#{it['pr']}" if it.get("pr") else "", age) if x)
            where = f"{it['env']}/{it['repo']}" if it.get("env") else it["repo"]
            label = (f"{'•' if it.get('unread') else ' '}{GLYPH.get(it['state'], '⚫')} "
                     f"{where} · {(it['sub'] or '')[:28]}"
                     + (f"  ({extra})" if extra else ""))
            parent = rumps.MenuItem(label)
            parent.add(rumps.MenuItem("Focus", callback=self._act(
                core.focus_terminal, it["handle"], it.get("env"))))
            if it["handle"]:
                parent.add(rumps.MenuItem("Interrupt", callback=self._act(
                    core.interrupt_terminal, it["handle"], it.get("env"))))
            self.menu.add(parent)
        if not items:
            self.menu.add("No agents")
        self.menu.add(rumps.separator)
        left = core.auto_badge(self._auto_until if self._auto_on else None, time.time())
        auto = rumps.MenuItem(f"Codex auto-approve{'  · ' + left[5:] if left else ''}")
        for i, minutes in enumerate(core.AUTO_DURATIONS):
            item = rumps.MenuItem(core.duration_label(minutes), callback=self._pick_auto(i))
            item.state = 1 if self._auto_on and self._auto_idx == i else 0
            auto.add(item)
        self.menu.add(auto)


if __name__ == "__main__":
    OrcaBar().run()
