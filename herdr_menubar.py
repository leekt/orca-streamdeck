#!/usr/bin/env python3
"""Herdr agent status and controls in the macOS menu bar."""
import threading
import time

import rumps
import herdr_streamdeck as core
from herdr_ui import age_label

GLYPH = {"blocked": "🟠", "working": "🔵", "done": "🟢", "idle": "⚪", "unknown": "⚫"}
IDLE_TITLE = "H"


class HerdrBar(rumps.App):
    def __init__(self):
        super().__init__("Herdr", title=IDLE_TITLE, quit_button="Quit")
        self._sig, self._items, self._machines = None, None, []
        self._lock = threading.Lock()
        threading.Thread(target=self._poll, daemon=True).start()
        self._timer = rumps.Timer(self.refresh, core.POLL_SECONDS)
        self._timer.start()

    def _poll(self):
        while True:
            items = core.fetch_items()
            with self._lock:
                self._items = items
                self._machines = core._backend.machine_status
            time.sleep(core.POLL_SECONDS)

    def _act(self, fn, item):
        return lambda _: core.run_action(fn, item)

    def _pick_auto(self, idx):
        def chosen(_):
            armed = core.armed_state(time.time())
            if armed and not armed[2] and armed[1] == idx:
                core.disarm_autoapprove()
            else:
                core.arm_autoapprove(core.AUTO_DURATIONS[idx], time.time(), idx)
            self._sig = None
        return chosen

    def refresh(self, _):
        with self._lock:
            items = self._items
            machines = self._machines
        if items is None:
            self.title = "H ⚠"
            if self._sig != "down":
                self.menu.clear()
                self.menu.add("Herdr unreachable")
                self._sig = "down"
            return
        count = sum(item["state"] in core.NEEDS_HUMAN for item in items)
        offline = any(machine["state"] == "offline" for machine in machines)
        self.title = (f"H · {count}" if count else IDLE_TITLE) + (" ⚠" if offline else "")
        now = time.time()
        armed = core.armed_state(now)
        sig = (tuple((it["id"], it["pane_id"], it["machine_label"], it["label"], it["sub"], it["state"])
                     for it in items), armed, int(now // 60),
               tuple((machine["id"], machine["label"], machine["state"], machine["error"]) for machine in machines))
        if sig == self._sig:
            return
        self._sig = sig
        self.menu.clear()
        for machine in machines:
            symbol = {"online": "🟢", "connecting": "⏳", "offline": "🔴"}[machine["state"]]
            entry = rumps.MenuItem(f"{symbol} {machine['label']} · {machine['state']}")
            if machine["error"]:
                entry.add(rumps.MenuItem(machine["error"][:240]))
            self.menu.add(entry)
        self.menu.add(rumps.separator)
        for item in items:
            age = age_label(item["state_since"], now * 1000)
            label = f"{GLYPH[item['state']]} {item['label']} · {item['sub'][:28]} ({age})"
            if item.get("show_machine"):
                label = f"{item['machine_label']} · {label}"
            parent = rumps.MenuItem(label)
            parent.add(rumps.MenuItem("Focus", callback=self._act(core.focus_terminal, item)))
            if item["state"] in {"working", "blocked"}:
                parent.add(rumps.MenuItem("Interrupt", callback=self._act(core.interrupt_terminal, item)))
            parent.add(rumps.MenuItem("Changes", callback=self._act(core.open_changed, item)))
            self.menu.add(parent)
        if not items:
            self.menu.add("No agents")
        self.menu.add(rumps.separator)
        badge = core.auto_badge(armed[0] if armed and armed[2] is None else None, now)
        auto = rumps.MenuItem(f"Local Codex auto-approve{' · ' + badge if badge else ''}")
        for idx, minutes in enumerate(core.AUTO_DURATIONS):
            entry = rumps.MenuItem(core.duration_label(minutes), callback=self._pick_auto(idx))
            entry.state = int(bool(armed and not armed[2] and armed[1] == idx))
            auto.add(entry)
        auto.add(rumps.MenuItem("Off", callback=lambda _: core.disarm_autoapprove()))
        self.menu.add(auto)


if __name__ == "__main__":
    HerdrBar().run()
