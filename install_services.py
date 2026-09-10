#!/usr/bin/env python3
"""Write relocatable LaunchAgent configs, or install with --install."""
import argparse
from pathlib import Path
import os
import plistlib
import shutil
import subprocess

ROOT = Path(__file__).resolve().parent
SERVICES = ("streamdeck", "autoapprove", "menubar")


def service_plist(name, root=ROOT):
    label = f"com.taek.herdr-{name}"
    command = ([str(root / "run.sh")] if name == "streamdeck" else
               [str(root / ".venv/bin/python"), "-u", str(root / f"herdr_{name}.py")])
    log = str(Path.home() / "Library/Logs" / f"herdr-{name}.log")
    return {
        "Label": label, "ProgramArguments": command, "WorkingDirectory": str(root),
        "EnvironmentVariables": {
            "PATH": f"{Path.home()}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        },
        "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 30,
        "StandardOutPath": log, "StandardErrorPath": log,
    }


def unload(label):
    target = f"gui/{os.getuid()}/{label}"
    loaded = subprocess.run(["launchctl", "print", target], capture_output=True)
    if loaded.returncode == 0:
        subprocess.run(["launchctl", "bootout", target], check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install", action="store_true", help="install and start deck + disarmed helper")
    parser.add_argument("--menubar", action="store_true", help="also install the optional menu bar app")
    args = parser.parse_args()
    for name in SERVICES:
        path = ROOT / f"com.taek.herdr-{name}.plist"
        path.write_bytes(plistlib.dumps(service_plist(name)))
        print(f"Prepared {path}")
    if not args.install:
        return
    agents = Path.home() / "Library/LaunchAgents"
    agents.mkdir(parents=True, exist_ok=True)
    (Path.home() / "Library/Logs").mkdir(parents=True, exist_ok=True)
    config = Path.home() / ".config/herdr-streamdeck"
    backup = config / "migration-backup"
    backup.mkdir(parents=True, exist_ok=True)
    for name in SERVICES:
        label = f"com.taek.orca-{name}"
        unload(label)
        old = agents / f"{label}.plist"
        if old.exists():
            destination = backup / old.name
            if destination.exists():
                raise RuntimeError(f"Backup already exists: {destination}")
            old.rename(destination)
    # Starting/reinstalling the service does not grant approval authority.
    for filename in [".orca-streamdeck-armed", ".herdr-streamdeck-armed"]:
        path = Path.home() / filename
        if path.exists():
            destination = backup / (filename + ".disabled")
            if destination.exists():
                destination = backup / (filename + f".{os.getpid()}.disabled")
            path.rename(destination)
    if not (config / "config.json").exists():
        shutil.copy2(ROOT / "config.example.json", config / "config.json")
    names = ["streamdeck", "autoapprove"] + (["menubar"] if args.menubar else [])
    for name in names:
        label = f"com.taek.herdr-{name}"
        unload(label)
        target = agents / f"{label}.plist"
        shutil.copy2(ROOT / target.name, target)
        subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(target)], check=True)
        print(f"Started {label}")


if __name__ == "__main__":
    main()
