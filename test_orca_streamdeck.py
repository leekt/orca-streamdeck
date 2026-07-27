"""Offline checks for the pieces that don't touch hardware. Run:
`./.venv/bin/python test_orca_streamdeck.py`."""
import orca_autoapprove as auto
import orca_streamdeck as m


def test_needs_human_set():
    assert m.NEEDS_HUMAN == {"permission", "waiting"}


def test_urgency_stopped_needs_input_working_done_idle():
    r = lambda s: m.STATUS.get(s, m.DEFAULT)[0]
    assert r("interrupted") < r("permission") < r("waiting") < r("working") \
        < r("done") < r("active")
    assert m.DEFAULT[0] > r("active")  # unknown state sorts last


def test_interrupted_agent_becomes_stopped_state():
    wts = [{"worktreeId": "W", "repo": "x", "displayName": "m", "agents": [
        {"paneKey": "T1:L1", "state": "working", "interrupted": True}]}]
    assert m.build_items(wts, TERMS)[0]["state"] == "interrupted"


def test_text_color_contrast():
    assert m.text_color((210, 214, 222)) == (20, 20, 20)   # light idle -> dark text
    assert m.text_color((30, 90, 190)) == (255, 255, 255)  # dark blue -> white text


def test_paginate_always_reserves_last_key():
    pages = m.paginate(list(range(6)), 6)  # 6 keys -> 5 tiles + status
    assert pages == [[0, 1, 2, 3, 4], [5]]
    assert m.paginate([], 6) == [[]]      # empty still yields one page


TERMS = [
    {"tabId": "T1", "leafId": "L1", "handle": "h1",
     "worktreeId": "W", "lastOutputAt": 10},
    {"tabId": "T2", "leafId": "L2", "handle": "h2",
     "worktreeId": "W", "lastOutputAt": 99},
]


def test_build_items_maps_agent_panekey_to_handle():
    wts = [{"worktreeId": "W", "repo": "kernel", "displayName": "dev", "agents": [
        {"paneKey": "T1:L1", "state": "working"},
        {"paneKey": "T2:L2", "state": "permission"},
    ]}]
    items = m.build_items(wts, TERMS)
    # permission sorts first; each agent gets its own terminal handle
    assert items[0]["state"] == "permission" and items[0]["handle"] == "h2"
    assert items[1]["state"] == "working" and items[1]["handle"] == "h1"


def test_build_items_handles_null_last_output_at():
    # a terminal with lastOutputAt=None must not crash the recency comparison
    terms = [{"tabId": "T", "leafId": "L", "handle": "h",
              "worktreeId": "W", "lastOutputAt": None},
             {"tabId": "T2", "leafId": "L2", "handle": "h2",
              "worktreeId": "W", "lastOutputAt": 5}]
    wts = [{"worktreeId": "W", "repo": "x", "displayName": "m",
            "status": "active", "agents": []}]
    assert m.build_items(wts, terms)[0]["handle"] == "h2"


def test_build_items_agentless_worktree_uses_recent_terminal():
    wts = [{"worktreeId": "W", "repo": "x", "displayName": "m",
            "status": "active", "agents": []}]
    items = m.build_items(wts, TERMS)
    assert len(items) == 1 and items[0]["handle"] == "h2"  # most recent (lastOutputAt=99)


def test_build_items_attaches_group_from_repo_id():
    wts = [{"worktreeId": "W", "repoId": "R", "repo": "x", "displayName": "m",
            "agents": [{"paneKey": "T1:L1", "state": "working"}]}]
    items = m.build_items(wts, TERMS, {"R": "grp-1"})
    assert items[0]["group"] == "grp-1"


def test_orca_cmd_targets_a_machine():
    assert m._orca_cmd(["worktree", "ps"], None) == ["orca", "worktree", "ps"]
    assert m._orca_cmd(["worktree", "ps"], "mac mini") == \
        ["orca", "worktree", "ps", "--environment", "mac mini"]


def test_send_to_terminal_refuses_an_empty_handle():
    """An empty --terminal makes the CLI target 'the active terminal' instead of
    erroring — that's how a lost handle closed a live tab on another machine."""
    calls = []
    m.subprocess.run = lambda cmd, **k: calls.append(cmd)
    try:
        assert m.send_to_terminal(["terminal", "send"], "", "mac mini") is False
        assert m.send_to_terminal(["terminal", "send"], None) is False
        assert calls == []                       # nothing was sent anywhere
        assert m.send_to_terminal(["terminal", "send"], "h", "mac mini") is True
        assert calls == [["orca", "terminal", "send", "--terminal", "h",
                          "--environment", "mac mini"]]
    finally:
        m.subprocess.run = _REAL_RUN


def test_interrupt_and_focus_never_fire_without_a_handle():
    calls, opened = [], []
    m.subprocess.run = lambda cmd, **k: calls.append(cmd)
    m.subprocess.Popen = lambda cmd: opened.append(cmd)
    try:
        m.interrupt_terminal(None, "mac mini")
        m.focus_terminal(None, "mac mini")
        assert calls == [] and opened == []      # remote: nothing at all
        m.focus_terminal(None)                   # local still raises the app
        assert calls == [] and opened == [["open", "-a", "Orca"]]
    finally:
        m.subprocess.run, m.subprocess.Popen = _REAL_RUN, _REAL_POPEN


def test_build_items_tags_the_machine():
    wts = [{"worktreeId": "W", "repo": "x", "agents": []}]
    assert m.build_items(wts, TERMS)[0]["env"] is None
    assert m.build_items(wts, TERMS, None, "mac mini")[0]["env"] == "mac mini"


def test_fetch_items_merges_machines_and_survives_a_sleeping_one():
    local = [{"id": "L", "state": "done", "pinned": False, "last_output": 5}]
    remote = [{"id": "R", "state": "permission", "pinned": False, "last_output": 9}]
    m.fetch_environments = lambda: ("mac mini",)
    try:
        m.fetch_env_items = lambda env=None: remote if env else local
        # merged and re-sorted across machines: the remote's blocked agent leads
        assert [it["id"] for it in m.fetch_items()] == ["R", "L"]

        m.fetch_env_items = lambda env=None: None if env else local   # remote asleep
        assert [it["id"] for it in m.fetch_items()] == ["L"]          # local carries on

        m.fetch_env_items = lambda env=None: remote if env else None  # local Orca down
        assert m.fetch_items() is None          # that's the pane being down
    finally:
        m.fetch_environments, m.fetch_env_items = _REAL_ENVS, _REAL_ENV_ITEMS


def test_icon_differs_for_the_same_repo_on_another_machine():
    here = m.icon_image({"repo": "deployer"}, 40)
    there = m.icon_image({"repo": "deployer", "env": "mac mini"}, 40)
    assert here.tobytes() != there.tobytes()


def test_group_color_stable_and_distinct():
    assert m.group_color("grp-1") == m.group_color("grp-1")   # deterministic
    assert m.group_color("grp-1") != m.group_color("grp-2")   # distinct
    assert m.group_color(None) == (90, 90, 90)                # ungrouped fallback


def test_icon_image_generates_identicon_per_repo():
    a1 = m.icon_image({"repo": "kernel"}, 40)
    a2 = m.icon_image({"repo": "kernel"}, 40)
    b = m.icon_image({"repo": "deployer"}, 40)
    assert a1.size == (40, 40) and a1.mode == "RGBA"
    assert a1.tobytes() == a2.tobytes()   # deterministic per repo
    assert a1.tobytes() != b.tobytes()    # distinct across repos


def test_paginate_grouped_never_mixes_groups():
    items = [{"group": "a"}, {"group": "a"}, {"group": "b"}]
    # 3 keys -> 2 tiles/page; group a fills a page, group b starts a fresh one
    assert m.paginate_grouped(items, 3) == [
        [{"group": "a"}, {"group": "a"}], [{"group": "b"}]]


def test_age_label_scales_and_tolerates_a_silent_terminal():
    now = 1_000_000_000
    assert m.age_label(now - 45_000, now) == "45s"
    assert m.age_label(now - 12 * 60_000, now) == "12m"
    assert m.age_label(now - 4 * 3_600_000, now) == "4h"
    assert m.age_label(now - 2 * 86_400_000, now) == "2d"
    assert m.age_label(now + 5_000, now) == "0s"   # clock skew, not a negative age
    assert m.age_label(None, now) == ""            # never output
    # the caller passes time.time()*1000, a float — no "2.0h" on the tile
    assert m.age_label(now - 7_200_000, now + 0.5) == "2h"


def test_build_items_carries_pin_unread_pr_and_age():
    wts = [{"worktreeId": "W", "repo": "x", "displayName": "m", "isPinned": True,
            "unread": True, "linkedPR": {"number": 104, "state": "open"},
            "agents": [{"paneKey": "T1:L1", "state": "working"}]}]
    it = m.build_items(wts, TERMS)[0]
    assert (it["pinned"], it["unread"], it["pr"]) == (True, True, 104)
    assert it["last_output"] == 10                 # from its own terminal, not the worktree
    # absent keys must not explode, and no PR must read as None rather than {}
    bare = m.build_items([{"worktreeId": "W", "repo": "x", "agents": []}], TERMS)[0]
    assert (bare["pinned"], bare["unread"], bare["pr"]) == (False, False, None)


def test_build_items_sorts_by_urgency_then_pin_then_oldest():
    def wt(wid, state, pinned=False):
        return {"worktreeId": wid, "repo": wid, "status": state,
                "isPinned": pinned, "agents": []}
    terms = [{"tabId": "t", "leafId": "l", "handle": h, "worktreeId": w,
              "lastOutputAt": lo}
             for h, w, lo in [("h1", "fresh", 900), ("h2", "stale", 100),
                              ("h3", "pinned", 950), ("h4", "urgent", 999)]]
    items = m.build_items([wt("fresh", "done"), wt("stale", "done"),
                           wt("pinned", "done", pinned=True),
                           wt("urgent", "permission")], terms)
    # urgency wins over a pin; then the pin; then the longest-ignored
    assert [it["id"] for it in items] == ["urgent", "pinned", "stale", "fresh"]


def test_build_items_carries_agent_type():
    wts = [{"worktreeId": "W", "repo": "x", "displayName": "m", "agents": [
        {"paneKey": "T1:L1", "state": "waiting", "agentType": "codex"}]}]
    assert m.build_items(wts, TERMS)[0]["agent_type"] == "codex"
    wts = [{"worktreeId": "W", "repo": "x", "displayName": "m", "agents": []}]
    assert m.build_items(wts, TERMS)[0]["agent_type"] is None  # agentless worktree


# A real codex exec modal preview, with the space-eaten redraw Orca returns, and
# the tab title Orca sets alongside it.
PROMPT = ("head -c 500 /private/tmp/orchestra-api.json› 1. Yes, proceed (y)2.No,and"
          "tellCodexwhattododifferently(esc)Press enter to confirm or esc to cancel•••")
BLOCKED_TITLE = "[ ! ] Action Required | orchestra-web"
BUSY_TITLE = "⠂ Explain this codebase"


def test_wants_approval_detects_every_modal_kind():
    assert auto.wants_approval(PROMPT, BLOCKED_TITLE)           # via the footer
    for header in ["Would you like to run the following command?",
                   "Would you like to make the following edits?",
                   "Would you like to grant these permissions?",
                   'Do you want to approve network access to "api.github.com"?',
                   "shell needs your approval."]:
        assert auto.wants_approval(header, BLOCKED_TITLE), header
        # ...and still when the redraw eats the spaces
        assert auto.wants_approval(header.replace(" ", ""), BLOCKED_TITLE), header


def test_wants_approval_needs_both_signals():
    # modal text in the tail but Orca no longer flags the tab -> already answered,
    # the text just lingers in the transcript. Don't press.
    assert not auto.wants_approval(PROMPT, BUSY_TITLE)
    # flagged but no modal -> the agent finished and wants a prompt, not a yes
    assert not auto.wants_approval("⏵⏵ auto mode on", BLOCKED_TITLE)
    assert not auto.wants_approval("", "")


def test_wants_approval_finds_a_modal_that_isnt_the_last_thing_printed():
    # codex keeps printing after drawing the modal, so it lands mid-tail
    tail = PROMPT + "\n• Ran npm ci\n  └ npm warn deprecated\nRun `npm audit`."
    assert auto.wants_approval(tail, BLOCKED_TITLE)


def test_wants_approval_ignores_plain_output_and_the_question_widget():
    assert not auto.wants_approval("└ []•Working(1m 46s • esc to interrupt)", BUSY_TITLE)
    # codex asking the user a free-text question is not an approval — Enter there
    # would submit a blank answer.
    assert not auto.wants_approval("Question 1 · enter to submit answer", BLOCKED_TITLE)


def test_describe_names_the_modal_and_what_it_asked_about():
    tail = ("• Explored\nhead -c 500 /private/tmp/orchestra-api.json› 1. Yes, proceed"
            " (y)2.No,andtellCodex(esc)Press enter to confirm")
    # no header on an exec modal — the command precedes the option list
    assert "head -c 500 /private/tmp/orchestra-api.json" in auto.describe(tail)
    assert auto.describe("Would you like to run the following command? › 1. Yes") \
        == "Would you like to run the following command"
    # header modals name themselves even when the redraw ate the spaces
    assert auto.describe("Wouldyouliketomakethefollowingedits?") \
        == "Would you like to make the following edits"
    assert auto.describe("no modal here") == "approval modal"


def _handles(approved):
    return [h for h, _ in approved]


def test_poll_presses_enter_only_for_blocked_codex_and_rate_limits_retries():
    sent, tails, reads = [], {"c": PROMPT, "k": PROMPT}, []
    items = [
        {"agent_type": "codex", "handle": "c", "title": BLOCKED_TITLE, "repo": "web"},
        {"agent_type": "claude", "handle": "k", "title": BLOCKED_TITLE, "repo": "web"},
        {"agent_type": "codex", "handle": None, "title": BLOCKED_TITLE, "repo": "web"},
        {"agent_type": "codex", "handle": "d", "title": BUSY_TITLE, "repo": "web"},
    ]
    auto.core.fetch_items = lambda: items
    auto.read_tail = lambda h, env=None: reads.append(h) or tails[h]
    auto.approve = lambda h, env=None: sent.append(h)

    at = {}
    try:
        first = auto.poll(at, 100.0)
        assert _handles(first) == ["c"] and sent == ["c"]        # claude never touched
        assert first[0][1].startswith("web: ")                   # log line names the repo
        assert reads == ["c"]        # unflagged terminals are never even read
        assert auto.poll(at, 101.0) == [] and sent == ["c"]      # inside the retry window
        # still flagged with a modal after the window -> codex asked again, press again
        assert _handles(auto.poll(at, 100.0 + auto.RETRY_SECONDS)) == ["c"]
        items[0]["title"] = BUSY_TITLE
        assert auto.poll(at, 200.0) == [] and sent == ["c", "c"]  # unblocked -> quiet
    finally:
        m.fetch_items = _REAL_FETCH_ITEMS


def test_poll_honours_the_repo_allowlist():
    auto.core.fetch_items = lambda: [
        {"agent_type": "codex", "handle": "c", "title": BLOCKED_TITLE, "repo": "web"},
        {"agent_type": "codex", "handle": "x", "title": BLOCKED_TITLE, "repo": "prod"},
    ]
    auto.read_tail = lambda h, env=None: PROMPT
    sent = []
    auto.approve = lambda h, env=None: sent.append(h)
    auto.REPOS = {"web"}
    try:
        assert _handles(auto.poll({}, 100.0)) == ["c"]   # prod is off limits
    finally:
        auto.REPOS = None
        m.fetch_items = _REAL_FETCH_ITEMS


_REAL_POPEN, _REAL_RUN = m.subprocess.Popen, m.subprocess.run
_REAL_ENVS, _REAL_ENV_ITEMS = m.fetch_environments, m.fetch_env_items
_REAL_READ_TAIL = m.read_tail
_REAL_FETCH_ITEMS = m.fetch_items
_REAL_ARMED_FILE = m.ARMED_FILE
_TMP_ARMED = m.pathlib.Path(__file__).with_name(".armed-test")


class FakeProc:
    def __init__(self, alive=True):
        self.alive, self.terminated = alive, False

    def poll(self):
        return None if self.alive else 0

    def terminate(self):
        self.terminated, self.alive = True, False


def _fake_procs():
    """Patch out process spawning; returns the list every Popen lands in."""
    spawned = []
    m.subprocess.Popen = lambda cmd: spawned.append(cmd) or FakeProc()
    m.subprocess.run = lambda *a, **k: None         # the pkill of any stray daemon
    m.ARMED_FILE = _TMP_ARMED
    return spawned


def _restore():
    m.subprocess.Popen, m.subprocess.run = _REAL_POPEN, _REAL_RUN
    m.ARMED_FILE.unlink(missing_ok=True)
    m.ARMED_FILE = _REAL_ARMED_FILE


def test_arm_autoapprove_persists_a_deadline_that_expires():
    spawned = _fake_procs()
    try:
        p, until = m.arm_autoapprove(None, 30, 1000.0, 0)
        assert p.alive and spawned[0] == m.AUTO_APPROVE_CMD
        assert until == 1000.0 + 30 * 60
        assert m.armed_state(1000.0) == (until, 0)  # survives a deck crash...
        assert m.armed_state(until + 1) is None     # ...but not past its window

        assert m.disarm_autoapprove(p) == (None, None) and p.terminated
        assert m.armed_state(2000.0) is None        # disarming clears the file
    finally:
        _restore()


def test_arm_autoapprove_forever_never_expires():
    _fake_procs()
    try:
        _, until = m.arm_autoapprove(None, None, 1000.0, 2)
        assert until == m.FOREVER
        # a year later it's still armed, and the index round-trips through the file
        assert m.armed_state(1000.0 + 86400 * 365) == (m.FOREVER, 2)
    finally:
        _restore()


def test_cycle_autoapprove_walks_off_30m_1h_forever_off():
    _fake_procs()
    try:
        proc, idx, seen = None, -1, []
        for _ in range(len(m.AUTO_DURATIONS) + 1):
            proc, until, idx = m.cycle_autoapprove(proc, idx, 1000.0)
            seen.append((idx, until))
        assert [i for i, _ in seen] == [0, 1, 2, -1]        # wraps back to off
        assert [u for _, u in seen] == [1000.0 + 1800, 1000.0 + 3600, m.FOREVER, None]
    finally:
        _restore()


def test_armed_state_ignores_a_missing_or_junk_file():
    m.ARMED_FILE = _TMP_ARMED
    try:
        m.ARMED_FILE.unlink(missing_ok=True)
        assert m.armed_state(0) is None             # never armed
        for junk in ("not-a-number", "123", "abc 0", "1e9 x"):
            m.ARMED_FILE.write_text(junk)           # corrupt -> off, not a crash
            assert m.armed_state(0) is None, junk
    finally:
        m.ARMED_FILE.unlink(missing_ok=True)
        m.ARMED_FILE = _REAL_ARMED_FILE


def test_auto_badge_counts_the_window_down():
    now = 1000.0
    assert m.auto_badge(None, now) == ""                    # off
    assert m.auto_badge(m.FOREVER, now) == "AUTO ON"        # no countdown to show
    assert m.auto_badge(now + 24 * 60, now) == "AUTO 24m"
    assert m.auto_badge(now + 90 * 60, now) == "AUTO 1h"


def test_duration_label_reads_naturally():
    assert [m.duration_label(d) for d in m.AUTO_DURATIONS] == \
        ["30 min", "1 hour", "Forever"]


def _agent(id="A", handle="h", state="waiting", **kw):
    base = {"id": id, "handle": handle, "state": state, "repo": "web",
            "agent_type": "codex", "title": BLOCKED_TITLE, "pinned": False,
            "last_output": 1, "worktree_id": "R::/p", "env": None}
    return {**base, **kw}


def test_ask_label_fits_a_key():
    assert m.ask_label("Would you like to run the following command — gh pr") == "command"
    assert m.ask_label("Would you like to make the following edits") == "edits"
    assert m.ask_label("Would you like to grant these permissions") == "perms"
    assert m.ask_label("Do you want to approve network access to") == "network"
    assert m.ask_label("gh pr list") == "approve?"     # exec modal, no header


def test_action_state_disables_what_cannot_run():
    it = _agent()
    assert m.action_state("approve", it, "edits", None) == ("edits", True)
    assert m.action_state("approve", it, "", None) == ("nothing", False)  # no modal
    assert m.action_state("auto", it, "", "h") == ("on", True)            # scoped here
    assert m.action_state("auto", it, "", None) == ("this one", True)
    assert m.action_state("auto", _agent(agent_type="claude"), "", None)[1] is False
    assert m.action_state("interrupt", _agent(handle=None), "", None)[1] is False
    assert m.action_state("diffs", _agent(worktree_id=None), "", None)[1] is False


def test_urgent_first_and_page_of_find_the_thing_that_needs_you():
    calm = [_agent(id=str(i), state="done") for i in range(7)]
    assert m.urgent_first(calm) is None                 # nothing to jump to
    urgent = _agent(id="X", state="permission")
    items = sorted(calm + [urgent], key=m.urgency_key)
    assert m.urgent_first(items)["id"] == "X"
    # a stopped agent outranks one merely waiting
    stopped = _agent(id="S", state="interrupted")
    assert m.urgent_first(sorted(items + [stopped], key=m.urgency_key))["id"] == "S"
    # and we can find which page it sits on (6 keys -> 5 tiles per page)
    tail_urgent = [_agent(id=str(i), state="done") for i in range(5)] + [urgent]
    assert m.page_of(tail_urgent, 6, urgent) == 1
    assert m.page_of(tail_urgent, 6, None) == 0


def test_refresh_focus_rebinds_drops_and_expires():
    m.read_tail = lambda h, env=None: "Would you like to make the following edits?"
    try:
        st = {"focused": _agent(state="waiting"), "focused_at": m.time.monotonic(),
              "focused_ask": ""}
        m.refresh_focus(st, [_agent(state="permission")])
        assert st["focused"]["state"] == "permission"   # re-bound to fresh data
        assert st["focused_ask"] == "edits"             # and what it's asking

        m.refresh_focus(st, [])                         # agent vanished
        assert st["focused"] is None and st["focused_ask"] == ""

        st = {"focused": _agent(), "focused_ask": "edits",
              "focused_at": m.time.monotonic() - m.AGENT_PAGE_IDLE - 1}
        m.refresh_focus(st, [_agent()])                 # idle too long
        assert st["focused"] is None
    finally:
        m.read_tail = _REAL_READ_TAIL


def test_agent_action_routes_taps_and_holds():
    calls = []
    run = lambda fn, *a: calls.append((fn.__name__, a))
    it = _agent(env="mac mini")
    st = {"auto": None, "auto_until": None, "auto_only": None}

    assert m.agent_action(st, "focus", it, 0, run) == {"focused": None}
    assert calls[-1] == ("focus_terminal", ("h", "mac mini"))
    m.agent_action(st, "interrupt", it, 0, run)
    assert calls[-1] == ("interrupt_terminal", ("h", "mac mini"))
    m.agent_action(st, "approve", it, 0, run)                    # tap = yes
    assert calls[-1][0] == "send_to_terminal_enter"
    m.agent_action(st, "approve", it, m.LONG_PRESS_SEC, run)     # hold = no
    assert calls[-1] == ("deny_terminal", ("h", "mac mini"))
    m.agent_action(st, "diffs", it, 0, run)
    assert calls[-1] == ("open_changed", ("R::/p", "mac mini"))


def test_agent_action_auto_scopes_to_one_handle_and_toggles_off():
    spawned = _fake_procs()
    try:
        st = {"auto": None, "auto_until": None, "auto_only": None}
        st.update(m.agent_action(st, "auto", _agent(), 0, lambda *a: None))
        assert st["auto_only"] == "h" and st["auto_idx"] == -1
        assert spawned[0][-2:] == ["--only", "h"]      # daemon told to trust one
        assert not m.ARMED_FILE.exists()               # scoped arming isn't resumed

        st.update(m.agent_action(st, "auto", _agent(), 0, lambda *a: None))
        assert st["auto_only"] is None and st["auto"] is None   # pressing again stops
    finally:
        _restore()


def test_autoapprove_only_flag_narrows_to_one_terminal():
    auto.core.fetch_items = lambda: [
        _agent(id="1", handle="mine"), _agent(id="2", handle="other")]
    auto.read_tail = lambda h, env=None: PROMPT
    sent = []
    auto.approve = lambda h, env=None: sent.append(h)
    auto.ONLY = {"mine"}
    try:
        assert _handles(auto.poll({}, 100.0)) == ["mine"]
    finally:
        auto.ONLY = set()
        m.fetch_items = _REAL_FETCH_ITEMS


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ok")
