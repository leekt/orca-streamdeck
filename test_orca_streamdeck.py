"""Offline checks for the pieces that don't touch hardware. Run:
`./.venv/bin/python test_orca_streamdeck.py`."""
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


def test_group_color_stable_and_distinct():
    assert m.group_color("grp-1") == m.group_color("grp-1")   # deterministic
    assert m.group_color("grp-1") != m.group_color("grp-2")   # distinct
    assert m.group_color(None) == (90, 90, 90)                # ungrouped fallback


def test_paginate_grouped_never_mixes_groups():
    items = [{"group": "a"}, {"group": "a"}, {"group": "b"}]
    # 3 keys -> 2 tiles/page; group a fills a page, group b starts a fresh one
    assert m.paginate_grouped(items, 3) == [
        [{"group": "a"}, {"group": "a"}], [{"group": "b"}]]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
    print("ok")
