#!/usr/bin/env python3
"""Find finished Orca worktrees across every machine, and optionally clear them.

A long-running fleet silts up with worktrees whose PR merged weeks ago. This
finds them and tells you why each one qualifies, on this machine and every paired
one.

    ./.venv/bin/python orca_cleanup.py             # report only, changes nothing
    ./.venv/bin/python orca_cleanup.py --complete  # mark them done on Orca's board
    ./.venv/bin/python orca_cleanup.py --remove    # delete from Orca AND git

`--remove` is the only destructive path, it asks per worktree, and it is refused
outright for anything that isn't provably finished. "Provably" is deliberately
narrow: a MERGED-or-closed linked PR and no live terminals. Absence of evidence
(no PR, no recent activity) is never treated as evidence of doneness — the CLI
exposes no dirty-tree check, so an old-looking worktree may still hold the only
copy of some work.

Author: taek <leekt216@gmail.com>
"""
import subprocess
import sys
import time

import orca_streamdeck as core

# Why a worktree is being kept. Ordered: the first one that applies is reported.
KEEP_REASONS = (
    ("main worktree", lambda w: w.get("isMainWorktree")),
    ("archived", lambda w: w.get("isArchived")),
    ("terminals live", lambda w: w.get("liveTerminalCount")),
    ("no linked PR", lambda w: not (w.get("linkedPR") or {}).get("state")),
    ("PR still open", lambda w: (w.get("linkedPR") or {}).get("state") == "open"),
)


def classify(worktree):
    """(is_candidate, reason). Pure, so the guard rails are testable without a fleet."""
    for reason, applies in KEEP_REASONS:
        if applies(worktree):
            return False, reason
    return True, f"PR {(worktree['linkedPR'] or {}).get('state')}, no terminals"


def scan():
    """[(env, worktree, is_candidate, reason)] across every machine."""
    rows = []
    for env in (None,) + core.fetch_environments():
        wt = core._orca_json(["worktree", "ps", "--json"], env)
        if wt is None:
            print(f"  ! {env or 'local'}: unreachable, skipped", file=sys.stderr)
            continue
        for w in wt.get("worktrees", []):
            ok, reason = classify(w)
            rows.append((env, w, ok, reason))
    return rows


def label(env, w):
    return f"{env or 'local'}/{w.get('repo')}/{w.get('displayName') or '?'}"


def act(env, worktree, remove):
    """Complete or remove one worktree. Returns the CLI's ok flag."""
    wid = worktree["worktreeId"]
    args = (["worktree", "rm", "--worktree", f"id:{wid}"] if remove else
            ["worktree", "set", "--worktree", f"id:{wid}",
             "--workspace-status", "completed"])
    return core._orca_json([*args, "--json"], env) is not None


def main(argv):
    remove, complete = "--remove" in argv, "--complete" in argv
    rows = scan()
    keep = [r for r in rows if not r[2]]
    todo = [r for r in rows if r[2]]
    now = time.time() * 1000

    print(f"{len(rows)} worktrees, {len(todo)} finished\n")
    for env, w, _, reason in todo:
        print(f"  DONE  {label(env, w):58} {core.age_label(w.get('lastActivityAt'), now):>4}"
              f"  ({reason})")
    for env, w, _, reason in keep:
        print(f"  keep  {label(env, w):58} {core.age_label(w.get('lastActivityAt'), now):>4}"
              f"  ({reason})")

    if not todo or not (remove or complete):
        print("\nNothing changed." if not (remove or complete)
              else "\nNothing to do.")
        return
    verb = "REMOVE from Orca and git" if remove else "mark completed"
    print(f"\nAbout to {verb}:")
    for env, w, _, _ in todo:
        # One confirmation each. A batch "y" is how you delete the wrong thing.
        if input(f"  {verb} {label(env, w)}? [y/N] ").strip().lower() != "y":
            print("    skipped")
            continue
        print("    done" if act(env, w, remove) else "    FAILED")


if __name__ == "__main__":
    main(sys.argv[1:])
