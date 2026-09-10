"""Herdr CLI adapter. No USB or macOS UI dependencies."""

import argparse
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time

CONFIG_PATH = Path.home() / ".config/herdr-streamdeck/config.json"
TIMEOUT = 8
STATES = {"blocked", "working", "done", "idle", "unknown"}


class HerdrError(RuntimeError):
    pass


def load_config(path=CONFIG_PATH):
    config = {"session": "default", "terminal_app": "Ghostty"}
    if path.exists():
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            raise ValueError("Herdr Stream Deck config must be a JSON object")
        for key in config:
            if key in data:
                if not isinstance(data[key], str) or (key == "session" and not data[key]):
                    raise ValueError(f"Invalid {key} in {path}")
                config[key] = data[key]
        if "include_remote_machines" in data:
            if type(data["include_remote_machines"]) is not bool:
                raise ValueError(f"Invalid include_remote_machines in {path}")
            config["include_remote_machines"] = data["include_remote_machines"]
    config.setdefault("include_remote_machines", True)
    return config


def identity(agent, session, machine_id="local", machine_target=None):
    """A pane location alone cannot identify its current occupant."""
    if not agent.get("pane_id") or not agent.get("terminal_id") or not agent.get("agent"):
        return None
    parts = [session, agent["terminal_id"], agent["agent"], agent.get("agent_session")]
    if machine_id != "local":
        parts.extend([machine_id, machine_target])
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()


def build_items(snapshot, session="default", machine_id="local", machine_label="Local",
                machine_target=None):
    """One tile per recognized agent, including agents launched by hand."""
    workspaces = {w["workspace_id"]: w for w in snapshot.get("workspaces", [])}
    tabs = {t["tab_id"]: t for t in snapshot.get("tabs", [])}
    items = []
    for agent in snapshot.get("agents", []):
        key = identity(agent, session, machine_id, machine_target)
        if key is None:
            continue
        workspace = workspaces.get(agent.get("workspace_id"), {})
        tab = tabs.get(agent.get("tab_id"), {})
        cwd = agent.get("foreground_cwd") or agent.get("cwd")
        label = workspace.get("label") or (Path(cwd).name if cwd else "workspace")
        state = agent.get("agent_status", "unknown")
        items.append({
            "id": key, "session": session, "pane_id": agent["pane_id"],
            "machine_id": machine_id, "machine_label": machine_label,
            "machine_target": machine_target,
            "terminal_id": agent["terminal_id"], "agent_session": agent.get("agent_session"),
            "workspace_id": agent.get("workspace_id"), "tab_id": agent.get("tab_id"),
            "label": label, "cwd": cwd,
            "sub": agent.get("title") or agent.get("name") or
                   agent.get("terminal_title_stripped") or tab.get("label") or agent["agent"],
            "agent_type": agent["agent"], "state": state if state in STATES else "unknown",
            "state_change_seq": agent.get("state_change_seq", 0),
            "state_since": None, "unread": state == "done",
        })
    return items


class Backend:
    def __init__(self, config=None, executable=None):
        self.config = load_config() if config is None else config
        self.session = self.config.get("session", "default")
        self.executable = executable or shutil.which("herdr") or "herdr"
        self.machine_id, self.machine_label, self.machine_target = "local", "Local", None
        self._observed = {}

    def command(self, args):
        # The default session has its own path. Never inherit a caller's socket
        # or focused-pane context; a launchd service must resolve the same target.
        prefix = [] if self.session == "default" else ["--session", self.session]
        return [self.executable, *prefix, *args]

    def run(self, args, *, raw=False):
        env = {k: v for k, v in os.environ.items() if not k.startswith("HERDR_")}
        try:
            result = subprocess.run(self.command(args), capture_output=True, text=True,
                                    timeout=TIMEOUT, env=env, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError) as exc:
            raise HerdrError(str(exc)) from exc
        if result.returncode:
            raise HerdrError((result.stderr or result.stdout).strip() or "Herdr command failed")
        if raw:
            return result.stdout
        try:
            data = json.loads(result.stdout)
            if not isinstance(data, dict) or not isinstance(data.get("result"), dict):
                raise ValueError("missing result object")
            return data["result"]
        except (ValueError, TypeError) as exc:
            raise HerdrError(f"Invalid Herdr response: {exc}") from exc

    def snapshot(self):
        snapshot = self.run(["api", "snapshot"]).get("snapshot")
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("agents"), list):
            raise HerdrError("Herdr snapshot has no agent list")
        return snapshot

    def fetch_items(self):
        items = build_items(self.snapshot(), self.session, self.machine_id,
                            self.machine_label, self.machine_target)
        now = time.time() * 1000
        observed = {}
        for item in items:
            signature = (item["state"], item["state_change_seq"])
            previous = self._observed.get(item["id"])
            since = previous[1] if previous and previous[0] == signature else now
            observed[item["id"]] = signature, since
            item["state_since"] = since
        self._observed = observed
        return items

    def live_agent(self, item):
        if not item or item.get("session") != self.session or not item.get("pane_id"):
            raise HerdrError("No agent target in this session")
        if (item.get("machine_id", "local") != self.machine_id or
                item.get("machine_target") != self.machine_target):
            raise HerdrError("Agent belongs to a different machine")
        agent = self.run(["agent", "get", item["pane_id"]]).get("agent")
        if not isinstance(agent, dict) or identity(
                agent, self.session, self.machine_id, self.machine_target) != item.get("id"):
            raise HerdrError("Agent changed or exited; select its current tile")
        return agent

    def read_tail(self, item):
        self.live_agent(item)
        # CLI reads return plain text, unlike snapshot/get. Detection reads are
        # passive and exclude historical scrollback containing old dialogs.
        return self.run(["agent", "read", item["pane_id"], "--source", "detection"], raw=True)

    def send_keys(self, item, *keys, blocked_seq=None):
        agent = self.live_agent(item)
        if blocked_seq is not None and (agent.get("agent_status") != "blocked" or
                                        agent.get("state_change_seq", 0) != blocked_seq):
            raise HerdrError("Approval changed before input could be sent")
        return self.run(["agent", "send-keys", item["pane_id"], *keys])

    def raise_terminal(self):
        app = self.config.get("terminal_app", "")
        if app and sys.platform == "darwin":
            result = subprocess.run(["open", "-a", app], capture_output=True, text=True,
                                    timeout=TIMEOUT)
            if result.returncode:
                raise HerdrError(f"Could not open {app}: {result.stderr.strip()}")

    def focus(self, item):
        self.live_agent(item)
        result = self.run(["agent", "focus", item["pane_id"]])
        self.raise_terminal()
        return result

    def interrupt(self, item):
        agent = self.live_agent(item)
        if agent.get("agent_status") not in {"working", "blocked"}:
            raise HerdrError("Agent is no longer working or blocked")
        # Esc is Codex's interrupt key; Ctrl-C can exit a ready Codex session.
        key = "esc" if item.get("agent_type") == "codex" else "ctrl+c"
        return self.send_keys(item, key)

    def approval_snapshot(self, item):
        before = self.live_agent(item)
        if before.get("agent") != "codex" or before.get("agent_status") != "blocked":
            return None
        tail = self.read_tail(item)
        after = self.live_agent(item)
        if (after.get("agent_status") != "blocked" or
                after.get("state_change_seq") != before.get("state_change_seq")):
            return None
        return tail, after.get("state_change_seq", 0)

    def open_changed(self, item):
        agent = self.live_agent(item)
        cwd = agent.get("foreground_cwd") or agent.get("cwd")
        if not cwd or not Path(cwd).is_dir():
            raise HerdrError("Agent has no accessible working directory")
        check = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"],
                               capture_output=True, text=True, timeout=TIMEOUT)
        if check.returncode:
            raise HerdrError("This agent's directory is not a Git checkout")
        root = check.stdout.strip()
        created = self.run(["tab", "create", "--workspace", agent["workspace_id"],
                            "--cwd", root, "--label", "Changes", "--no-focus"])
        pane_id = (created.get("root_pane") or {}).get("pane_id")
        tab_id = (created.get("tab") or {}).get("tab_id")
        if not pane_id or not tab_id:
            raise HerdrError("Herdr did not return the new review pane")
        script = Path(__file__).with_name("review_changes.py")
        command = shlex.join([sys.executable, str(script), root])
        self.run(["pane", "run", pane_id, command])
        self.run(["tab", "focus", tab_id])
        self.raise_terminal()


@dataclass(frozen=True)
class Machine:
    id: str
    label: str
    target: str
    session: str


def parse_machines(text):
    """Read the public CLI catalog; never infer IDs from a display label."""
    try:
        records = json.loads(text)
        if not isinstance(records, list):
            raise ValueError("expected a list (Herdr 0.9.0+ is required)")
        machines, seen = [], set()
        for record in records:
            if (not isinstance(record, dict) or
                    any(not isinstance(record.get(key), str) or not record[key]
                        for key in ("id", "label", "target", "session")) or
                    type(record.get("enabled")) is not bool):
                raise ValueError("invalid machine profile")
            if record["id"] == "local" or record["id"] in seen:
                raise ValueError("duplicate or reserved machine ID")
            seen.add(record["id"])
            if record["target"].startswith("-") or any(c.isspace() for c in record["target"]):
                raise ValueError("invalid SSH target")
            if record["enabled"]:
                machines.append(Machine(*(record[key] for key in ("id", "label", "target", "session"))))
        return machines
    except (ValueError, TypeError) as exc:
        raise HerdrError(f"Cannot read saved machines: {exc}") from exc


class RemoteBackend(Backend):
    """Use ordinary, non-interactive SSH against one saved Herdr session."""

    def __init__(self, machine, config):
        super().__init__({**config, "session": machine.session}, "herdr")
        self.machine_id, self.machine_label = machine.id, machine.label
        self.machine_target = machine.target

    def ssh_command(self, script):
        return ["/usr/bin/ssh", "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                "-o", "ConnectTimeout=3", "-o", "ServerAliveInterval=3",
                "-o", "ServerAliveCountMax=1", "--", self.machine_target,
                shlex.join(["/bin/sh", "-c", script])]

    def command(self, args):
        # A non-login SSH shell may not include Herdr's install directory in PATH.
        # Clear forwarded Herdr context before resolving the explicit session.
        command = shlex.join(super().command(args)[1:])
        script = """for name in $(env | sed -n 's/^\\(HERDR_[A-Za-z0-9_]*\\)=.*/\\1/p'); do
    unset "$name"
done
for bin in "$(command -v herdr)" "$HOME/.local/bin/herdr" /opt/homebrew/bin/herdr /usr/local/bin/herdr "$HOME/.nix-profile/bin/herdr"; do
    if [ -n "$bin" ] && [ -x "$bin" ]; then exec "$bin" """ + command + """; fi
done
printf '%s\\n' 'Herdr executable not found on remote machine' >&2
exit 127"""
        return self.ssh_command(script)

    def open_changed(self, item):
        agent = self.live_agent(item)
        cwd = agent.get("foreground_cwd") or agent.get("cwd")
        if not cwd:
            raise HerdrError("Agent has no working directory")
        # Resolve the checkout on its owning host; remote paths must never be
        # tested with local Path.is_dir() or handed to the local Git executable.
        try:
            check = subprocess.run(self.ssh_command(shlex.join(
                ["git", "-C", cwd, "rev-parse", "--show-toplevel"])),
                capture_output=True, text=True, timeout=TIMEOUT, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError) as exc:
            raise HerdrError(str(exc)) from exc
        if check.returncode:
            raise HerdrError(check.stderr.strip() or "Remote directory is not a Git checkout")
        root = check.stdout.strip()
        if not root:
            raise HerdrError("Remote Git returned no checkout path")
        created = self.run(["tab", "create", "--workspace", agent["workspace_id"],
                            "--cwd", root, "--label", "Changes", "--no-focus"])
        pane_id = (created.get("root_pane") or {}).get("pane_id")
        tab_id = (created.get("tab") or {}).get("tab_id")
        if not pane_id or not tab_id:
            raise HerdrError("Herdr did not return the remote review pane")
        # The remote viewer needs only Git and less, with no copied helper files.
        script = """cd "$1" || exit
{
    printf '\\nStatus (includes untracked files)\\n'
    git -c color.ui=always status --short
    printf '\\nStaged changes\\n'
    git -c color.ui=always diff --cached --no-ext-diff --no-textconv
    printf '\\nUnstaged changes\\n'
    git -c color.ui=always diff --no-ext-diff --no-textconv
} | less -R"""
        self.run(["pane", "run", pane_id, shlex.join(["/bin/sh", "-c", script, "herdr-changes", root])])
        self.run(["tab", "focus", tab_id])
        self.raise_terminal()


@dataclass
class RemotePoll:
    backend: RemoteBackend
    future: Future | None = None
    items: list = field(default_factory=list)
    state: str = "connecting"
    error: str = ""
    next_poll: float = 0


class FleetBackend:
    """Local polling stays responsive while each remote snapshot runs separately."""

    def __init__(self, config=None, executable=None, executor=None):
        self.local = Backend(config, executable)
        self.config, self.session = self.local.config, self.local.session
        self.executor = executor or ThreadPoolExecutor(max_workers=4, thread_name_prefix="herdr-ssh")
        self.remotes = {}
        self.machine_status = []

    def saved_machines(self):
        if not self.config.get("include_remote_machines", True):
            return []
        return parse_machines(self.local.run(["machine", "list", "--json"], raw=True))

    def fetch_items(self):
        statuses, items = [], []
        try:
            items = list(self.local.fetch_items())
            statuses.append({"id": "local", "label": "Local", "state": "online", "error": ""})
        except HerdrError as exc:
            statuses.append({"id": "local", "label": "Local", "state": "offline", "error": str(exc)})
        try:
            machines = self.saved_machines()
        except HerdrError as exc:
            # Hide remote targets until the catalog is readable again. An old
            # profile may have been disabled, removed, or pointed at another host.
            machines = []
            statuses.append({"id": "catalog", "label": "Saved machines", "state": "offline", "error": str(exc)})
        enabled = {machine.id: machine for machine in machines}
        for key in list(self.remotes):
            poll = self.remotes[key]
            machine = enabled.get(key)
            if (machine is None or machine.target != poll.backend.machine_target or
                    machine.session != poll.backend.session):
                if poll.future:
                    poll.future.cancel()
                del self.remotes[key]
        now = time.monotonic()
        for machine in machines:
            poll = self.remotes.get(machine.id)
            if poll is None:
                poll = self.remotes[machine.id] = RemotePoll(RemoteBackend(machine, self.config))
            if poll.future is not None and poll.future.done():
                try:
                    poll.items = poll.future.result()
                    poll.state, poll.error = "online", ""
                except (HerdrError, OSError, subprocess.SubprocessError) as exc:
                    poll.items = []
                    poll.state, poll.error = "offline", str(exc)
                poll.future = None
                poll.next_poll = now + (4 if poll.state == "offline" else 2)
            if poll.future is None and now >= poll.next_poll:
                poll.future = self.executor.submit(poll.backend.fetch_items)
            items.extend({**item, "machine_label": machine.label} for item in poll.items)
            statuses.append({"id": machine.id, "label": machine.label, "state": poll.state, "error": poll.error})
        self.machine_status = statuses
        return [{**item, "show_machine": bool(machines)} for item in items]

    def target_backend(self, item):
        if not item:
            raise HerdrError("No agent target")
        if item.get("machine_id", "local") == "local":
            if item.get("machine_target") is not None:
                raise HerdrError("Invalid local machine target")
            return self.local
        # Re-read saved profiles for actions, including actions from an old tile.
        for machine in self.saved_machines():
            if (machine.id == item.get("machine_id") and machine.target == item.get("machine_target") and
                    machine.session == item.get("session")):
                return RemoteBackend(machine, self.config)
        raise HerdrError("Remote machine changed, was disabled, or was removed")

    def live_agent(self, item):
        return self.target_backend(item).live_agent(item)

    def read_tail(self, item):
        return self.target_backend(item).read_tail(item)

    def send_keys(self, item, *keys, blocked_seq=None):
        return self.target_backend(item).send_keys(item, *keys, blocked_seq=blocked_seq)

    def focus(self, item):
        return self.target_backend(item).focus(item)

    def interrupt(self, item):
        return self.target_backend(item).interrupt(item)

    def approval_snapshot(self, item):
        return self.target_backend(item).approval_snapshot(item)

    def open_changed(self, item):
        return self.target_backend(item).open_changed(item)

    def close(self):
        self.executor.shutdown(wait=False, cancel_futures=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Read Herdr agent tiles without opening the Stream Deck")
    parser.add_argument("--check", action="store_true", help="also verify agent identity and passive screen reads")
    parser.add_argument("--local-only", action="store_true", help="skip saved SSH machines")
    parser.add_argument("--status", action="store_true", help="include machine connectivity alongside agent tiles")
    args = parser.parse_args()
    backend = None
    try:
        config = load_config()
        if args.local_only:
            config["include_remote_machines"] = False
        backend = FleetBackend(config)
        items = backend.fetch_items()
        # A one-shot inspection waits for the initial bounded SSH probes. The
        # controller and menu bar use non-blocking polling instead.
        for poll in backend.remotes.values():
            if poll.future:
                try:
                    poll.future.result()
                except (HerdrError, OSError, subprocess.SubprocessError):
                    pass
        if backend.remotes:
            items = backend.fetch_items()
        if args.check:
            for item in items:
                agent = backend.live_agent(item)
                screen = backend.read_tail(item)
                print(json.dumps({"machine": item["machine_label"], "pane": item["pane_id"], "state": agent["agent_status"],
                                  "identity_checked": True, "screen_characters": len(screen)}))
        else:
            print(json.dumps({"machines": backend.machine_status, "agents": items} if args.status else items, indent=2))
        if any(machine["state"] != "online" for machine in backend.machine_status):
            raise HerdrError("Some machines are unavailable; use --status for details")
    except (HerdrError, ValueError, OSError) as exc:
        raise SystemExit(str(exc)) from exc
    finally:
        if backend is not None:
            backend.close()
