#!/usr/bin/env python3
"""Orca control pane as a macOS menu bar app.

Shares its brains with the Stream Deck controller: the title shows how many
agents need you, and the dropdown lists every agent by urgency. Per agent:
Focus (jump to its terminal in Orca) and Interrupt (Esc/Ctrl-C). Updates every
POLL_SECONDS — it's a running process, not a rate-limited WidgetKit widget.

Run: ./.venv/bin/python orca_menubar.py

Author: taek <leekt216@gmail.com>
"""
import threading

import rumps

import orca_streamdeck as core

GLYPH = {"interrupted": "🔴", "permission": "🟠", "waiting": "🟠",
         "working": "🔵", "done": "🟢", "active": "⚪"}
IDLE_TITLE = "🐳"


class OrcaBar(rumps.App):
    def __init__(self):
        super().__init__(IDLE_TITLE, quit_button="Quit")
        self._sig = None
        rumps.Timer(self.refresh, core.POLL_SECONDS).start()

    def _act(self, fn, *args):
        # Run the orca call off the UI thread so the menu stays responsive.
        return lambda _: threading.Thread(target=fn, args=args, daemon=True).start()

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
        # closing a submenu the user is navigating every 2s.
        sig = tuple((it["id"], it["state"]) for it in items)
        if sig == self._sig:
            return
        self._sig = sig

        self.menu.clear()
        if not items:
            self.menu = ["No agents"]
            return
        for it in items:
            label = f"{GLYPH.get(it['state'], '⚫')} {it['repo']} · {(it['sub'] or '')[:28]}"
            parent = rumps.MenuItem(label)
            parent.add(rumps.MenuItem("Focus", callback=self._act(core.focus_terminal, it["handle"])))
            if it["handle"]:
                parent.add(rumps.MenuItem(
                    "Interrupt", callback=self._act(core.interrupt_terminal, it["handle"])))
            self.menu.add(parent)


if __name__ == "__main__":
    OrcaBar().run()
