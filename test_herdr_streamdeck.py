"""Offline integration and controller regression tests; no agents or USB touched."""
import copy
import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import herdr_backend as backend
import herdr_streamdeck as core
import herdr_autoapprove as auto
import herdr_ui as ui
from review_changes import review
from install_services import service_plist


AGENT = {
    "agent": "codex", "agent_status": "blocked", "pane_id": "w4:p2",
    "terminal_id": "term_live", "workspace_id": "w4", "tab_id": "w4:t2",
    "agent_session": {"agent": "codex", "kind": "id", "value": "conversation-1"},
    "foreground_cwd": "/tmp/project", "terminal_title_stripped": "Fix login",
    "state_change_seq": 7,
}
SNAPSHOT = {"agents": [AGENT], "workspaces": [{"workspace_id": "w4", "label": "kernel"}],
            "tabs": [{"tab_id": "w4:t2", "label": "Agent"}]}
PROMPT = ("Would you like to run the following command?\n  echo hello\n"
          "› 1. Yes, proceed (y)\n  2. No, and tell Codex what to do differently (esc)\n"
          "Press enter to confirm or esc to cancel")


def item(**updates):
    result = backend.build_items(copy.deepcopy(SNAPSHOT))[0]
    result.update(updates)
    return result


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.backend = backend.Backend({"session": "default", "terminal_app": ""}, "herdr")

    def test_maps_live_snapshot_and_ignores_shells(self):
        snapshot = copy.deepcopy(SNAPSHOT)
        snapshot["panes"] = [{"pane_id": "w4:p1", "agent_status": "unknown"}]
        result = backend.build_items(snapshot)
        self.assertEqual(len(result), 1)
        self.assertEqual((result[0]["label"], result[0]["sub"], result[0]["pane_id"]),
                         ("kernel", "Fix login", "w4:p2"))

    def test_known_states_and_future_unknown_state(self):
        for state in ["blocked", "working", "done", "idle", "unknown", "future-state"]:
            snapshot = copy.deepcopy(SNAPSHOT)
            snapshot["agents"][0]["agent_status"] = state
            result = backend.build_items(snapshot)[0]
            self.assertEqual(result["state"], state if state in backend.STATES else "unknown")
            self.assertEqual(result["unread"], state == "done")

    def test_identity_follows_terminal_move_but_changes_for_new_occupant(self):
        original = backend.identity(AGENT, "default")
        moved = dict(AGENT, pane_id="w9:p8", workspace_id="w9")
        self.assertEqual(original, backend.identity(moved, "default"))
        for changed in [dict(AGENT, terminal_id="new-terminal"),
                        dict(AGENT, agent="claude"),
                        dict(AGENT, agent_session={"value": "new-conversation"})]:
            self.assertNotEqual(original, backend.identity(changed, "default"))
        self.assertNotEqual(original, backend.identity(AGENT, "personal"))

    def test_missing_identity_is_not_an_actionable_tile(self):
        for key in ["pane_id", "terminal_id", "agent"]:
            snapshot = copy.deepcopy(SNAPSHOT)
            snapshot["agents"][0].pop(key)
            self.assertEqual(backend.build_items(snapshot), [])

    def test_cli_routes_named_session_and_discards_inherited_context(self):
        self.backend.session = "personal"
        response = subprocess.CompletedProcess([], 0, '{"result":{"agents":[]}}', '')
        with patch.dict(backend.os.environ, {"HERDR_SOCKET_PATH": "/wrong/socket", "HERDR_PANE_ID": "wrong"}), \
                patch.object(backend.subprocess, "run", return_value=response) as run:
            self.backend.run(["agent", "list"])
        self.assertEqual(run.call_args.args[0], ["herdr", "--session", "personal", "agent", "list"])
        self.assertFalse(any(key.startswith("HERDR_") for key in run.call_args.kwargs["env"]))

    def test_cli_error_and_malformed_json_fail_closed(self):
        for result in [subprocess.CompletedProcess([], 1, '', 'socket unavailable'),
                       subprocess.CompletedProcess([], 0, 'plain text', ''),
                       subprocess.CompletedProcess([], 0, '{"error":"bad"}', '')]:
            with patch.object(backend.subprocess, "run", return_value=result), \
                    self.assertRaises(backend.HerdrError):
                self.backend.run(["api", "snapshot"])
        with patch.object(backend.subprocess, "run", side_effect=FileNotFoundError), \
                self.assertRaises(backend.HerdrError):
            self.backend.run(["api", "snapshot"])

    def test_read_uses_plain_text_detection_source(self):
        with patch.object(self.backend, "live_agent", return_value=AGENT), \
                patch.object(self.backend, "run", return_value=PROMPT) as run:
            self.assertEqual(self.backend.read_tail(item()), PROMPT)
            run.assert_called_once_with(["agent", "read", "w4:p2", "--source", "detection"], raw=True)

    def test_no_input_when_occupant_changes(self):
        with patch.object(self.backend, "run", return_value={"agent": dict(AGENT, terminal_id="other")}) as run:
            with self.assertRaises(backend.HerdrError):
                self.backend.send_keys(item(), "enter")
            self.assertEqual(run.call_count, 1)

    def test_no_default_target_or_wrong_session_fallback(self):
        with patch.object(self.backend, "run") as run:
            for target in [None, {}, item(session="other")]:
                with self.assertRaises(backend.HerdrError):
                    self.backend.live_agent(target)
            run.assert_not_called()

    def test_changed_approval_blocks_last_input(self):
        with patch.object(self.backend, "live_agent", return_value=dict(AGENT, state_change_seq=8)), \
                patch.object(self.backend, "run") as run:
            with self.assertRaises(backend.HerdrError):
                self.backend.send_keys(item(), "enter", blocked_seq=7)
            run.assert_not_called()

    def test_approval_rechecks_liveness_after_read(self):
        with patch.object(self.backend, "live_agent", side_effect=[AGENT, dict(AGENT, agent_status="working")]), \
                patch.object(self.backend, "read_tail", return_value=PROMPT):
            self.assertIsNone(self.backend.approval_snapshot(item()))

    def test_focus_only_raises_terminal_after_success(self):
        with patch.object(self.backend, "live_agent", return_value=AGENT), \
                patch.object(self.backend, "run", side_effect=backend.HerdrError("gone")), \
                patch.object(self.backend, "raise_terminal") as raise_terminal:
            with self.assertRaises(backend.HerdrError):
                self.backend.focus(item())
            raise_terminal.assert_not_called()

    def test_interrupt_uses_agent_keys_and_refuses_idle(self):
        with patch.object(self.backend, "live_agent", return_value=AGENT), \
                patch.object(self.backend, "send_keys") as send:
            self.backend.interrupt(item())
            send.assert_called_once_with(item(), "esc")
        with patch.object(self.backend, "live_agent", return_value=dict(AGENT, agent_status="idle")), \
                self.assertRaises(backend.HerdrError):
            self.backend.interrupt(item())

    def test_age_tracks_state_changes_not_every_poll(self):
        snapshot = copy.deepcopy(SNAPSHOT)
        with patch.object(self.backend, "snapshot", return_value=snapshot), \
                patch.object(backend.time, "time", side_effect=[100, 200, 300]):
            self.assertEqual(self.backend.fetch_items()[0]["state_since"], 100000)
            self.assertEqual(self.backend.fetch_items()[0]["state_since"], 100000)
            snapshot["agents"][0]["state_change_seq"] += 1
            self.assertEqual(self.backend.fetch_items()[0]["state_since"], 300000)

    def test_diff_opens_returned_review_pane_only(self):
        with tempfile.TemporaryDirectory(prefix="repo ' ") as directory:
            live = dict(AGENT, foreground_cwd=directory)
            result = subprocess.CompletedProcess([], 0, directory + '\n', '')
            created = {"root_pane": {"pane_id": "w4:p9"}, "tab": {"tab_id": "w4:t9"}}
            with patch.object(self.backend, "live_agent", return_value=live), \
                    patch.object(backend.subprocess, "run", return_value=result), \
                    patch.object(self.backend, "run", side_effect=[created, {}, {}]) as run, \
                    patch.object(self.backend, "raise_terminal"):
                self.backend.open_changed(item())
            calls = [call.args[0] for call in run.call_args_list]
            self.assertEqual(calls[1][:3], ["pane", "run", "w4:p9"])
            self.assertIn(directory, backend.shlex.split(calls[1][3]))
            self.assertEqual(calls[2], ["tab", "focus", "w4:t9"])


class ApprovalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.arm_patch = patch.object(core, "ARMED_FILE", Path(self.tmp.name) / "armed")
        self.arm_patch.start()

    def tearDown(self):
        self.arm_patch.stop()
        self.tmp.cleanup()

    def test_current_modal_requires_blocked_and_yes(self):
        self.assertTrue(core.wants_approval(PROMPT, "blocked"))
        self.assertFalse(core.wants_approval(PROMPT, "working"))
        self.assertFalse(core.wants_approval("Would you like to run the following command?", "blocked"))
        self.assertFalse(core.wants_approval(PROMPT + '\nenter to submit answer', "blocked"))
        self.assertFalse(core.wants_approval("Question 1 · enter to submit answer", "blocked"))

    def test_auto_requires_selected_one_time_choice(self):
        self.assertTrue(core.selected_once(PROMPT))
        self.assertFalse(core.selected_once(PROMPT.replace('› 1.', '  1.')))
        self.assertFalse(core.selected_once(PROMPT.replace('Yes, proceed (y)', "Yes, and don't ask again")))
        self.assertFalse(core.selected_once(PROMPT.replace('› 1.', '  1.').replace('  2.', '› 2.')))

    def test_changed_dialog_is_not_answered(self):
        with patch.object(core, "approval_details", return_value=(PROMPT, 8)), \
                patch.object(core._backend, "send_keys") as send:
            self.assertFalse(core.answer_approval(item(), expected=(PROMPT, 7)))
            send.assert_not_called()

    def test_manual_yes_and_no_use_validated_agent_keys(self):
        with patch.object(core, "approval_details", return_value=(PROMPT, 7)), \
                patch.object(core._backend, "send_keys") as send:
            self.assertTrue(core.answer_approval(item()))
            send.assert_called_with(item(), "enter", blocked_seq=7)
            self.assertTrue(core.answer_approval(item(), deny=True))
            send.assert_called_with(item(), "esc", blocked_seq=7)

    def test_expiry_scope_and_session_are_persisted(self):
        self.assertIsNone(core.armed_state(100))
        core.arm_autoapprove(30, 100, only=item()["id"])
        self.assertEqual(core.armed_state(101), (1900, 0, item()["id"]))
        self.assertIsNone(core.armed_state(1900))
        with patch.object(core._backend, "session", "other"):
            self.assertIsNone(core.armed_state(101))
        self.assertEqual(core.ARMED_FILE.stat().st_mode & 0o777, 0o600)
        core.disarm_autoapprove()
        self.assertFalse(core.ARMED_FILE.exists())

    def test_invalid_or_legacy_arm_file_is_disarmed(self):
        for data in ['inf 2', '{}', '[]', '{"session":"default","until":"NaN","index":0}',
                     '{"session":"default","until":null,"index":99}']:
            core.ARMED_FILE.write_text(data)
            self.assertIsNone(core.armed_state(100))

    def test_duration_cycle_and_forever(self):
        idx = -1
        for expected in [1900, 3700, core.FOREVER, None]:
            expiry, scope, idx = core.cycle_autoapprove(idx, 100)
            self.assertEqual(expiry, expected)
        self.assertIsNone(core.armed_state(100))

    def test_agent_auto_tap_enables_forever_and_toggles_off(self):
        target = item()
        core.agent_action({}, "auto", target, 0, Mock())
        armed = core.armed_state(time.time())
        self.assertEqual(armed, (core.FOREVER, core.AUTO_DURATIONS.index(None), target["id"]))
        self.assertEqual(core.action_state("auto", target, "", armed), ("forever", True))
        # Persisted indefinite mode remains valid far beyond any timed window.
        self.assertEqual(core.armed_state(time.time() + 365 * 86400), armed)
        core.agent_action({}, "auto", target, 0, Mock())
        self.assertIsNone(core.armed_state(time.time()))
        self.assertEqual(core.action_state("auto", target, "", None), ("off", True))

    def test_agent_auto_hold_cycles_duration_without_losing_scope(self):
        target = item()
        with patch.object(core.time, "time", return_value=100):
            for expected, label in [(1900, "30m"), (3700, "1h"), (core.FOREVER, "forever")]:
                core.agent_action({}, "auto", target, core.LONG_PRESS_SEC, Mock())
                armed = core.armed_state(100)
                self.assertEqual(armed[0], expected)
                self.assertEqual(armed[2], target["id"])
                self.assertEqual(core.action_state("auto", target, "", armed), (label, True))
            core.agent_action({}, "auto", target, core.LONG_PRESS_SEC, Mock())
            self.assertIsNone(core.armed_state(100))

    def test_auto_key_shows_session_wide_mode_and_other_agent_scope(self):
        target = item()
        self.assertEqual(core.action_state("auto", target, "", (core.FOREVER, 2, None)),
                         ("all forever", True))
        self.assertEqual(core.action_state("auto", target, "", (core.FOREVER, 2, "other-agent")),
                         ("off", True))

    def test_auto_once_per_modal_and_records_only_success(self):
        core.arm_autoapprove(30, time.time())
        sent = {}
        with patch.object(core, "fetch_items", return_value=[item()]), \
                patch.object(core, "approval_details", return_value=(PROMPT, 7)), \
                patch.object(core, "answer_approval", return_value=True) as answer:
            self.assertEqual(len(auto.poll(sent, 100)), 1)
            self.assertEqual(auto.poll(sent, 200), [])
            self.assertEqual(answer.call_count, 1)
        with patch.object(core, "fetch_items", return_value=[item()]), \
                patch.object(core, "approval_details", return_value=(PROMPT, 8)), \
                patch.object(core, "answer_approval", return_value=False):
            self.assertEqual(auto.poll(sent, 300), [])
            self.assertEqual(sent[item()["id"]][1][0], 7)

    def test_auto_disarmed_wrong_scope_and_non_codex_are_ignored(self):
        with patch.object(core, "fetch_items", return_value=[item()]), \
                patch.object(core, "approval_details", return_value=(PROMPT, 7)), \
                patch.object(core, "answer_approval") as answer:
            self.assertEqual(auto.poll({}, 100), [])
            core.arm_autoapprove(30, time.time(), only="some-other-agent")
            self.assertEqual(auto.poll({}, 100), [])
            answer.assert_not_called()
        core.arm_autoapprove(30, time.time())
        for changed in [item(agent_type="claude"), item(agent_session=None), item(state="working")]:
            with patch.object(core, "fetch_items", return_value=[changed]), \
                    patch.object(core, "approval_details") as read:
                self.assertEqual(auto.poll({}, 100), [])
                read.assert_not_called()


class ControllerTests(unittest.TestCase):
    def test_service_paths_follow_the_checkout(self):
        root = Path("/tmp/project with spaces")
        for name in ["streamdeck", "autoapprove", "menubar"]:
            config = service_plist(name, root)
            self.assertEqual(config["Label"], f"com.taek.herdr-{name}")
            self.assertEqual(config["WorkingDirectory"], str(root))
            self.assertTrue(config["ProgramArguments"][0].startswith(str(root)))
            self.assertIn(".local/bin", config["EnvironmentVariables"]["PATH"])
            self.assertNotIn("--always", config["ProgramArguments"])

    def test_pagination_and_urgent_page(self):
        items = [item(id=str(i), state="idle") for i in range(8)]
        items[6]["state"] = "blocked"
        self.assertEqual([len(page) for page in core.paginate(items, 6)], [5, 3])
        self.assertEqual(core.page_of(items, 6, core.urgent_first(items)), 1)
        self.assertEqual(core.paginate([], 6), [[]])

    def test_disabled_actions_do_not_execute(self):
        with patch.object(core, "armed_state", return_value=None):
            run = Mock()
            core.agent_action({"focused_ask": ""}, "approve", item(), 0, run)
            core.agent_action({}, "interrupt", item(state="idle"), 0, run)
            core.agent_action({}, "auto", item(agent_type="claude"), 0, run)
            run.assert_not_called()

    def test_disconnect_drops_all_action_targets(self):
        state = {"focused": item(), "focused_ask": "command", "items": [item()], "slots": [item()]}
        core.disconnected(state, 6)
        self.assertIsNone(state["focused"])
        self.assertEqual(state["slots"], [None] * 6)
        self.assertEqual(state["items"], [])

    def test_disappeared_agent_closes_action_page(self):
        state = {"focused": item(), "focused_at": time.monotonic(), "focused_ask": "command"}
        core.refresh_focus(state, [])
        self.assertIsNone(state["focused"])

    def test_tiles_render_for_all_states_and_key_sizes(self):
        class Deck:
            def __init__(self, size):
                self.size = size
            def key_image_format(self):
                return {"size": self.size, "format": "JPEG", "flip": (False, False), "rotation": 0}
        for size in [(80, 80), (72, 72), (96, 96)]:
            deck = Deck(size)
            for state in backend.STATES:
                self.assertTrue(ui.render_tile(deck, item(state=state)))
            self.assertTrue(ui.render_status(deck, 0, 0, 1, down=True))
            self.assertTrue(ui.render_action(deck, "APPRV", "nothing", enabled=False))

    def test_six_key_action_page_fits_and_has_back(self):
        deck = Mock()
        state = {"items": [item()], "focused": item(), "focused_ask": "command"}
        with patch.object(core, "armed_state", return_value=None), \
                patch.object(core, "render_action", return_value=b'image') as render:
            core.repaint(deck, state, 6, 5)
            self.assertEqual([call.args[1] for call in render.call_args_list],
                             ["FOCUS", "APPRV", "AUTO", "INTR", "DIFFS", "BACK"])

    def test_review_shows_staged_unstaged_and_untracked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q", directory], check=True)
            (root / "tracked.txt").write_text('staged\n')
            subprocess.run(["git", "-C", directory, "add", "tracked.txt"], check=True)
            (root / "tracked.txt").write_text('unstaged\n')
            (root / "untracked.txt").write_text('local\n')
            text = review(root)
            for expected in ["Staged changes", "Unstaged changes", "untracked.txt", "staged", "unstaged"]:
                self.assertIn(expected, text)


if __name__ == "__main__":
    unittest.main()
