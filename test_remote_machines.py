"""Offline multi-machine regressions. All SSH execution is mocked."""
from concurrent.futures import Future
import copy
import json
from pathlib import Path
import shlex
import subprocess
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import herdr_backend as backend
import herdr_streamdeck as core
import herdr_autoapprove as auto
import herdr_ui as ui
from test_herdr_streamdeck import AGENT, SNAPSHOT, PROMPT, item


PROFILE = {"id": "saved-mini", "label": "Mac Mini Dev", "target": "herdr-remote",
           "session": "default", "enabled": True, "selected": False}


def remote_item(**changes):
    result = backend.build_items(copy.deepcopy(SNAPSHOT), machine_id=PROFILE["id"],
                                 machine_label=PROFILE["label"], machine_target=PROFILE["target"])[0]
    result.update(changes)
    return result


class RemoteBackendTests(unittest.TestCase):
    def setUp(self):
        machine = backend.parse_machines(json.dumps([PROFILE]))[0]
        self.backend = backend.RemoteBackend(machine, {"terminal_app": ""})

    def test_catalog_validates_profiles_and_ignores_disabled_machines(self):
        self.assertEqual(backend.parse_machines(json.dumps([PROFILE]))[0].label, "Mac Mini Dev")
        self.assertEqual(backend.parse_machines(json.dumps([dict(PROFILE, enabled=False)])), [])
        for records in [{}, [None], [dict(PROFILE, enabled="yes")], [dict(PROFILE, session=None)],
                        [dict(PROFILE, target="-oProxyCommand=bad")], [dict(PROFILE, id="local")],
                        [PROFILE, PROFILE]]:
            with self.subTest(records=records), self.assertRaises(backend.HerdrError):
                backend.parse_machines(json.dumps(records))

    def test_config_defaults_to_saved_machines_and_accepts_local_only(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            self.assertTrue(backend.load_config(path)["include_remote_machines"])
            path.write_text('{"include_remote_machines":false}')
            self.assertFalse(backend.load_config(path)["include_remote_machines"])
            path.write_text('{"include_remote_machines":"false"}')
            with self.assertRaises(ValueError):
                backend.load_config(path)

    def test_identical_remote_and_local_panes_have_distinct_identities(self):
        remote = remote_item()
        self.assertNotEqual(item()["id"], remote["id"])
        self.assertEqual(remote["id"], remote_item(machine_label="New label")["id"])
        self.assertNotEqual(remote["id"], backend.identity(AGENT, "default", PROFILE["id"], "another-host"))
        with patch.object(self.backend, "run") as run, self.assertRaises(backend.HerdrError):
            self.backend.live_agent(item())
        run.assert_not_called()
        local = backend.Backend({"terminal_app": ""})
        with patch.object(local, "run") as run, self.assertRaises(backend.HerdrError):
            local.live_agent(remote)
        run.assert_not_called()

    def test_remote_shell_preserves_arguments_and_clears_forwarded_context(self):
        # Execute only the quoted command payload against a fake local binary,
        # never the SSH command. This catches shell injection and quoting bugs.
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "herdr"
            executable.write_text('#!/bin/sh\n[ -z "${HERDR_SOCKET_PATH:-}" ] || exit 91\n'
                                  '[ -z "${HERDR_ENV:-}" ] || exit 92\nprintf "%s\\n" "$@"\n')
            executable.chmod(0o700)
            marker = Path(directory) / "unexpected"
            args = ["pane", "run", "w4:p2", f"echo '$HOME'; $(touch {shlex.quote(str(marker))})"]
            self.backend.session = "agents"
            command = self.backend.command(args)
            self.assertIn("BatchMode=yes", command)
            self.assertIn("StrictHostKeyChecking=yes", command)
            self.assertEqual(command[-3:-1], ["--", "herdr-remote"])
            result = subprocess.run(shlex.split(command[-1]), capture_output=True, text=True,
                                    env={"PATH": directory + ":/usr/bin:/bin", "HERDR_SOCKET_PATH": "/wrong",
                                         "HERDR_ENV": "1"}, check=True)
            self.assertEqual(result.stdout.splitlines(), ["--session", "agents", *args])
            self.assertFalse(marker.exists())

    def test_remote_reads_use_explicit_host_and_session(self):
        self.backend.session = "agents"
        response = subprocess.CompletedProcess([], 0, json.dumps({"result": {"snapshot": SNAPSHOT}}), "")
        with patch.object(backend.subprocess, "run", return_value=response) as run:
            self.assertEqual(self.backend.fetch_items()[0]["session"], "agents")
        command = run.call_args.args[0]
        self.assertEqual(command[-2], "herdr-remote")
        self.assertIn("--session agents api snapshot", shlex.split(command[-1])[2])
        self.assertEqual(run.call_args.kwargs["stdin"], subprocess.DEVNULL)

    def test_remote_diffs_resolve_git_and_open_viewer_on_remote_host(self):
        root = "/remote/only/repo ' $(touch never)"
        response = subprocess.CompletedProcess([], 0, root + "\n", "")
        created = {"root_pane": {"pane_id": "w4:p9"}, "tab": {"tab_id": "w4:t9"}}
        with patch.object(self.backend, "live_agent", return_value=dict(AGENT, foreground_cwd=root)), \
                patch.object(backend.Path, "is_dir", side_effect=AssertionError("local path lookup")), \
                patch.object(backend.subprocess, "run", return_value=response) as ssh, \
                patch.object(self.backend, "run", side_effect=[created, {}, {}]) as run:
            self.backend.open_changed(remote_item())
        self.assertEqual(ssh.call_args.args[0][-2], "herdr-remote")
        git_args = shlex.split(shlex.split(ssh.call_args.args[0][-1])[2])
        self.assertEqual(git_args, ["git", "-C", root, "rev-parse", "--show-toplevel"])
        calls = [call.args[0] for call in run.call_args_list]
        self.assertEqual(calls[1][:3], ["pane", "run", "w4:p9"])
        viewer = shlex.split(calls[1][3])
        self.assertEqual(viewer[-1], root)
        self.assertIn("--no-ext-diff --no-textconv", viewer[2])
        self.assertNotIn("review_changes.py", calls[1][3])
        self.assertEqual(calls[2], ["tab", "focus", "w4:t9"])


class FleetTests(unittest.TestCase):
    def setUp(self):
        self.pending = []
        executor = Mock()
        def submit(*_):
            future = Future()
            self.pending.append(future)
            return future
        executor.submit.side_effect = submit
        self.backend = backend.FleetBackend({"session": "default", "terminal_app": ""}, executor=executor)
        self.backend.local.fetch_items = Mock(return_value=[item()])
        self.backend.local.run = Mock(return_value=json.dumps([PROFILE]))
        self.clock = patch.object(backend.time, "monotonic", return_value=100)
        self.now = self.clock.start()
        self.addCleanup(self.clock.stop)
        self.addCleanup(self.backend.close)

    def connect(self):
        self.backend.fetch_items()
        self.pending[-1].set_result([remote_item()])
        return self.backend.fetch_items()

    def test_slow_remote_never_blocks_local_polling(self):
        self.assertEqual(len(self.backend.fetch_items()), 1)
        self.assertEqual(self.backend.machine_status[1]["state"], "connecting")
        self.assertFalse(self.pending[0].done())
        self.assertEqual(len(self.backend.fetch_items()), 1)
        self.assertEqual(self.backend.local.fetch_items.call_count, 2)
        self.assertEqual(len(self.pending), 1)

    def test_aggregates_machines_and_reflects_renames_without_changing_identity(self):
        items = self.connect()
        self.assertEqual(len({it["id"] for it in items}), 2)
        self.assertTrue(all(it["show_machine"] for it in items))
        self.backend.local.run.return_value = json.dumps([dict(PROFILE, label="Renamed Mini")])
        updated = self.backend.fetch_items()
        self.assertEqual(updated[1]["machine_label"], "Renamed Mini")
        self.assertEqual(items[1]["id"], updated[1]["id"])

    def test_remote_disconnect_clears_targets_and_recovers_without_hiding_local(self):
        self.connect()
        self.now.return_value = 103
        self.backend.fetch_items()
        self.pending[-1].set_exception(backend.HerdrError("Permission denied (publickey)"))
        items = self.backend.fetch_items()
        self.assertEqual([it["machine_id"] for it in items], ["local"])
        self.assertEqual(self.backend.machine_status[1]["state"], "offline")
        self.assertIn("publickey", self.backend.machine_status[1]["error"])
        self.now.return_value = 108
        self.backend.fetch_items()
        self.pending[-1].set_result([remote_item()])
        self.assertEqual(len(self.backend.fetch_items()), 2)
        self.assertEqual(self.backend.machine_status[1]["state"], "online")

    def test_local_failure_keeps_remote_agents_available(self):
        self.connect()
        self.backend.local.fetch_items.side_effect = backend.HerdrError("local server down")
        self.assertEqual([it["machine_id"] for it in self.backend.fetch_items()], [PROFILE["id"]])
        self.assertEqual(self.backend.machine_status[0]["state"], "offline")
        self.assertEqual(self.backend.machine_status[1]["state"], "online")

    def test_disabled_profile_drops_tiles_and_rejects_stale_actions(self):
        self.connect()
        self.backend.local.run.return_value = json.dumps([dict(PROFILE, enabled=False)])
        self.assertEqual(len(self.backend.fetch_items()), 1)
        with patch.object(backend.RemoteBackend, "send_keys") as send, self.assertRaises(backend.HerdrError):
            self.backend.send_keys(remote_item(), "enter")
        send.assert_not_called()

    def test_retargeted_profile_discards_in_flight_result(self):
        self.backend.fetch_items()
        old = self.pending[-1]
        old.set_running_or_notify_cancel()
        self.backend.local.run.return_value = json.dumps([dict(PROFILE, target="another-host")])
        self.assertEqual(len(self.backend.fetch_items()), 1)
        old.set_result([remote_item()])
        self.assertEqual(len(self.backend.fetch_items()), 1)
        with self.assertRaises(backend.HerdrError):
            self.backend.target_backend(remote_item())

    def test_invalid_catalog_keeps_local_agents_and_blocks_remote_actions(self):
        self.connect()
        self.backend.local.run.return_value = "broken catalog"
        self.assertEqual(len(self.backend.fetch_items()), 1)
        self.assertEqual(self.backend.machine_status[1]["id"], "catalog")
        with self.assertRaises(backend.HerdrError):
            self.backend.target_backend(remote_item())

    def test_local_only_never_queries_catalog_or_starts_ssh(self):
        self.backend.config["include_remote_machines"] = False
        self.assertEqual(len(self.backend.fetch_items()), 1)
        self.backend.local.run.assert_not_called()
        self.assertEqual(self.pending, [])

    def test_remote_key_action_revalidates_identity_on_its_own_host(self):
        with patch.object(backend.RemoteBackend, "run", side_effect=[{"agent": AGENT}, {}]) as remote_run:
            self.backend.send_keys(remote_item(), "enter", blocked_seq=7)
        self.assertEqual([call.args[0] for call in remote_run.call_args_list],
                         [["agent", "get", "w4:p2"], ["agent", "send-keys", "w4:p2", "enter"]])
        self.backend.local.run.assert_called_once_with(["machine", "list", "--json"], raw=True)


class RemoteControlsTests(unittest.TestCase):
    def test_session_wide_auto_does_not_extend_to_remote_agents(self):
        armed = (core.FOREVER, 2, None)
        self.assertTrue(core.arm_covers(armed, item()))
        self.assertFalse(core.arm_covers(armed, remote_item()))
        self.assertEqual(core.action_state("auto", remote_item(), "", armed), ("off", True))
        with patch.object(core, "fetch_items", return_value=[remote_item()]), \
                patch.object(core, "armed_state", return_value=armed), \
                patch.object(core, "approval_details") as read:
            self.assertEqual(auto.poll({}, 100), [])
        read.assert_not_called()

    def test_remote_auto_requires_its_exact_conversation(self):
        armed = (core.FOREVER, 2, remote_item()["id"])
        self.assertFalse(core.arm_covers(armed, item()))
        with patch.object(core, "fetch_items", return_value=[remote_item()]), \
                patch.object(core, "armed_state", return_value=armed), \
                patch.object(core, "approval_details", return_value=(PROMPT, 7)), \
                patch.object(core, "answer_approval", return_value=True) as answer:
            self.assertEqual(len(auto.poll({}, 100)), 1)
        answer.assert_called_once_with(remote_item(), automatic=True, expected=(PROMPT, 7))

    def test_always_mode_keeps_remote_approval_explicit(self):
        with patch.object(core, "fetch_items", return_value=[remote_item()]), \
                patch.object(core, "approval_details") as read:
            self.assertEqual(auto.poll({}, 100, require_armed=False), [])
        read.assert_not_called()

    def test_remote_disappearance_closes_action_page(self):
        state = {"focused": remote_item(), "focused_at": time.monotonic(), "focused_ask": "command"}
        core.refresh_focus(state, [item()])
        self.assertIsNone(state["focused"])

    def test_small_tiles_show_machine_labels_and_connection_failures(self):
        class Deck:
            def key_image_format(self):
                return {"size": (72, 72), "format": "JPEG", "flip": (False, False), "rotation": 0}
        machines = [{"label": "Local", "state": "online"}, {"label": "Mac Mini Dev", "state": "offline"}]
        self.assertTrue(ui.render_tile(Deck(), remote_item(show_machine=True)))
        self.assertTrue(ui.render_status(Deck(), 0, 0, 1, machines=machines))


if __name__ == "__main__":
    unittest.main()
