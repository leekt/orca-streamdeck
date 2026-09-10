#!/usr/bin/env python3
"""Opt-in Codex approval helper for the configured Herdr session."""
import argparse
import hashlib
import time

import herdr_streamdeck as core
from herdr_backend import HerdrError

RETRY_SECONDS = 4.0
ARMED_POLL = 0.5
IDLE_SECONDS = 5.0


def poll(sent_at, now, only=None, require_armed=True):
    """Confirm each observed modal once; never retry a stale unchanged screen."""
    approved = []
    items = core.fetch_items()
    if items is None:
        return approved
    live_ids = {item["id"] for item in items}
    for key in set(sent_at) - live_ids:
        del sent_at[key]
    for item in items:
        key = item["id"]
        if (item["state"] != "blocked" or item["agent_type"] != "codex" or
                not item.get("agent_session") or (only and key not in only)):
            continue
        if require_armed and not core.arm_covers(core.armed_state(time.time()), item):
            continue
        if not require_armed and item.get("machine_id", "local") != "local" and not only:
            continue
        previous = sent_at.get(key)
        if previous and now - previous[0] < RETRY_SECONDS:
            continue
        try:
            details = core.approval_details(item)
            if not details or not core.selected_once(details[0]):
                continue
            token = (details[1], hashlib.sha256(details[0].encode()).hexdigest())
            if previous and previous[1] == token:
                continue
            # Re-check expiry/scope immediately before the action, since reads
            # may take time and the user can disarm us during a poll.
            armed = core.armed_state(time.time())
            if require_armed and not core.arm_covers(armed, item):
                continue
            if core.answer_approval(item, automatic=True, expected=details):
                sent_at[key] = now, token
                approved.append((key, f"{item['label']} ({item['pane_id']}): {core.describe(details[0])}"))
        except HerdrError as exc:
            print(f"Skipped {item['label']}: {exc}", flush=True)
    return approved


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--always", action="store_true", help="explicitly ignore the arm file")
    parser.add_argument("--only", action="append", help="limit to agent identity from herdr_backend.py")
    args = parser.parse_args()
    sent_at, was_armed = {}, False
    while True:
        armed = core.armed_state(time.time())
        if not (args.always or armed):
            if was_armed:
                print("stood down", flush=True)
                was_armed = False
            time.sleep(IDLE_SECONDS)
            continue
        if not was_armed:
            print(f"armed for Herdr session {core._backend.session}", flush=True)
            was_armed = True
        only = {armed[2]} if armed and armed[2] else set(args.only or [])
        for _, description in poll(sent_at, time.monotonic(), only, not args.always):
            print(f"approved {description}", flush=True)
        time.sleep(ARMED_POLL)


if __name__ == "__main__":
    main()
