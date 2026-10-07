//! Offline regressions: real CLI-shaped fixtures, a fake command runner, no agent or USB touched.
use anyhow::{Result, bail};
use herdr_streamdeck::{
    approval::{self, Arm, Sent},
    backend::{Backend, Fleet, Runner, Spec, review_command},
    controller::{Action, Controls, Effect},
    model::*,
    ui::Renderer,
};
use serde_json::{Value, json};
use std::{
    collections::HashSet,
    fs,
    process::Command,
    sync::{Arc, Mutex},
};

const PROMPT: &str = "Would you like to run the following command?\n  echo hello\n› 1. Yes, proceed (y)\n  2. No, and tell Codex what to do differently (esc)\nPress enter to confirm or esc to cancel";
const QUESTION: &str =
    "Which database should we use?\n› 1. Yes\n  2. No\nPress enter to submit answer";
const PROFILE: &str = r#"[{"id":"saved-mini","label":"Mac Mini Dev","target":"herdr-remote","session":"default","enabled":true,"selected":false}]"#;

fn snapshot() -> Value {
    serde_json::from_str(include_str!("fixtures/snapshot.json")).unwrap()
}
fn remote() -> Source {
    parse_machines(PROFILE).unwrap().remove(0)
}

/// Answers Herdr, SSH, git and open invocations from fixtures and logs every call.
#[derive(Default)]
struct Fake {
    log: Mutex<Vec<Vec<String>>>,
    agent: Mutex<Option<Value>>,
    agents: Mutex<Vec<Value>>,
    tail: Mutex<String>,
    machines: Mutex<String>,
    remote_down: Mutex<bool>,
}
impl Fake {
    fn new() -> Arc<Self> {
        let fake = Self::default();
        *fake.agent.lock().unwrap() = Some(snapshot()["agents"][0].clone());
        *fake.tail.lock().unwrap() = PROMPT.into();
        *fake.machines.lock().unwrap() = "[]".into();
        Arc::new(fake)
    }
    fn calls(&self, needle: &str) -> Vec<Vec<String>> {
        self.log
            .lock()
            .unwrap()
            .iter()
            .filter(|c| c.join(" ").replace(['\'', '"'], "").contains(needle))
            .cloned()
            .collect()
    }
}
impl Runner for Fake {
    fn run(&self, spec: Spec) -> Result<String> {
        self.log.lock().unwrap().push(
            std::iter::once(spec.program.clone())
                .chain(spec.args.clone())
                .collect(),
        );
        let flat = spec.args.join(" ").replace(['\'', '"'], "");
        if spec.program == "/usr/bin/ssh" && *self.remote_down.lock().unwrap() {
            bail!("ssh: connect to host herdr-remote port 22: Operation timed out");
        }
        if spec.program == "git" || (spec.program == "/usr/bin/ssh" && flat.contains("rev-parse")) {
            return Ok("/tmp/project\n".into());
        }
        if spec.program == "/usr/bin/open" {
            return Ok(String::new());
        }
        let result = if flat.contains("machine list") {
            return Ok(self.machines.lock().unwrap().clone());
        } else if flat.contains("api snapshot") {
            let mut snapshot = snapshot();
            let agents = self.agents.lock().unwrap();
            if !agents.is_empty() {
                snapshot["agents"] = json!(*agents);
            }
            json!({"snapshot": snapshot})
        } else if flat.contains("agent get") {
            let agents = self.agents.lock().unwrap();
            let agent = if agents.is_empty() {
                self.agent.lock().unwrap().clone()
            } else {
                let target = flat
                    .split("agent get ")
                    .nth(1)
                    .and_then(|args| args.split_whitespace().next());
                agents
                    .iter()
                    .find(|a| field(a, "pane_id") == target)
                    .cloned()
            };
            match agent {
                Some(agent) => json!({"agent": agent}),
                None => bail!("agent not found"),
            }
        } else if flat.contains("agent read") {
            return Ok(self.tail.lock().unwrap().clone());
        } else if flat.contains("tab create") {
            json!({"root_pane": {"pane_id": "w4:p9"}, "tab": {"tab_id": "w4:t9"}})
        } else if flat.contains("send-keys") || flat.contains("focus") || flat.contains("pane run")
        {
            json!({})
        } else {
            bail!("unexpected command: {flat}")
        };
        Ok(json!({"result": result}).to_string())
    }
}
fn backend(fake: &Arc<Fake>, session: &str, remote_machines: bool) -> Backend {
    let config = Config {
        session: session.into(),
        terminal_app: "Ghostty".into(),
        include_remote_machines: remote_machines,
    };
    Backend {
        source: Source::local(session),
        config: Arc::new(config),
        runner: fake.clone(),
    }
}
fn item() -> Item {
    build_items(&snapshot(), &Source::local("default"))
        .unwrap()
        .remove(0)
}
fn arm_file() -> (tempfile::TempDir, std::path::PathBuf) {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("armed");
    (dir, path)
}

#[test]
fn maps_snapshot_and_ignores_agentless_panes() {
    let items = build_items(&snapshot(), &Source::local("default")).unwrap();
    assert_eq!(items.len(), 1);
    let item = &items[0];
    assert_eq!(
        (
            item.label.as_str(),
            item.sub.as_str(),
            item.pane_id.as_str(),
            item.state
        ),
        ("kernel", "Fix login", "w4:p2", State::Blocked)
    );
    assert!(item.can_auto());
    let mut future = snapshot();
    future["agents"][0]["agent_status"] = json!("paused");
    assert_eq!(
        build_items(&future, &Source::local("default")).unwrap()[0].state,
        State::Unknown
    );
    let mut shell = snapshot();
    shell["agents"][0]["agent"] = json!("");
    assert!(
        build_items(&shell, &Source::local("default"))
            .unwrap()
            .is_empty()
    );
}

#[test]
fn identity_follows_terminal_but_separates_machines_and_occupants() {
    let agent = snapshot()["agents"][0].clone();
    let local = identity(&agent, &Source::local("default")).unwrap();
    let mut moved = agent.clone();
    moved["pane_id"] = json!("w9:p1");
    assert_eq!(
        identity(&moved, &Source::local("default")).unwrap(),
        local,
        "moving panes keeps the conversation identity"
    );
    let mut occupant = agent.clone();
    occupant["agent_session"]["value"] = json!("conversation-2");
    assert_ne!(
        identity(&occupant, &Source::local("default")).unwrap(),
        local
    );
    assert_ne!(
        identity(&agent, &remote()).unwrap(),
        local,
        "identical remote pane must not collide with local"
    );
    assert_ne!(identity(&agent, &Source::local("work")).unwrap(), local);
    // Matches the Python controller's sha256(json.dumps(..., sort_keys=True)) so existing arm files stay valid.
    assert_eq!(
        local, "7497a3509cb1be5789343a4202d58e0fd4b68c46370ae07942371288e16e73c2",
        "must match the Python controller's sha256(json.dumps(sort_keys=True)) so existing arm files stay valid"
    );
}

#[test]
fn catalog_validates_profiles_and_ignores_disabled_machines() {
    assert_eq!(remote().label, "Mac Mini Dev");
    let disabled = PROFILE.replace("\"enabled\":true", "\"enabled\":false");
    assert!(parse_machines(&disabled).unwrap().is_empty());
    for bad in [
        PROFILE.replace("herdr-remote", "-oProxyCommand=evil"),
        PROFILE.replace("herdr-remote", "host name"),
        PROFILE.replace("saved-mini", "local"),
        format!(
            "[{},{}]",
            &PROFILE[1..PROFILE.len() - 1],
            &PROFILE[1..PROFILE.len() - 1]
        ),
    ] {
        assert!(parse_machines(&bad).is_err(), "accepted {bad}");
    }
    assert!(parse_machines("not json").is_err());
}

#[test]
fn cli_routes_sessions_and_wraps_remote_calls_in_batch_ssh() {
    let fake = Fake::new();
    let local = backend(&fake, "work", true)
        .spec(&["api", "snapshot"])
        .unwrap();
    assert_eq!(
        (local.program.as_str(), local.args.clone()),
        (
            "herdr",
            vec![
                "--session".to_string(),
                "work".into(),
                "api".into(),
                "snapshot".into()
            ]
        )
    );
    unsafe { std::env::set_var("HERDR_SESSION", "inherited") };
    let cmd = local.command();
    assert!(
        cmd.get_envs()
            .any(|(key, value)| key == "HERDR_SESSION" && value.is_none()),
        "inherited HERDR_* context must be cleared"
    );
    let remote = backend(&fake, "default", true)
        .for_source(remote())
        .spec(&["agent", "read", "w4:p2", "--source", "detection"])
        .unwrap();
    assert_eq!(remote.program, "/usr/bin/ssh");
    assert!(
        remote.args.contains(&"BatchMode=yes".to_string())
            && remote
                .args
                .contains(&"StrictHostKeyChecking=yes".to_string())
    );
    let position = remote.args.iter().position(|a| a == "--").unwrap();
    assert_eq!(remote.args[position + 1], "herdr-remote");
    // The script is shell-quoted twice (once per hop); compare its unquoted shape.
    let script = remote.args[position + 2].replace(['\'', '"'], "");
    assert!(
        script.starts_with("/bin/sh -c for name in $(env"),
        "{script}"
    );
    assert!(
        script.contains("exec $bin agent read w4:p2 --source detection"),
        "{script}"
    );
    assert!(script.contains("unset $name"));
}

#[test]
fn independent_services_share_ssh_connections_but_not_host_aliases() {
    let control_path = |spec: &Spec| {
        spec.args
            .iter()
            .find(|a| a.starts_with("ControlPath="))
            .unwrap()
            .clone()
    };
    let poll = backend(&Fake::new(), "default", true)
        .for_source(remote())
        .spec(&["api", "snapshot"])
        .unwrap();
    let mut same_host = remote();
    same_host.session = "another-session".into();
    let action = backend(&Fake::new(), "work", true)
        .for_source(same_host)
        .spec(&["agent", "get", "w4:p2"])
        .unwrap();
    assert_eq!(control_path(&poll), control_path(&action));
    assert!(poll.args.contains(&"ControlMaster=auto".into()));
    assert!(poll.args.contains(&"ControlPersist=60".into()));
    assert!(control_path(&poll).ends_with("-%C"));

    let mut other_host = remote();
    other_host.target = Some("other-alias".into());
    let other = backend(&Fake::new(), "default", true)
        .for_source(other_host)
        .spec(&["api", "snapshot"])
        .unwrap();
    assert_ne!(control_path(&poll), control_path(&other));
    let local = backend(&Fake::new(), "default", false)
        .spec(&["api", "snapshot"])
        .unwrap();
    assert!(!local.args.iter().any(|a| a.starts_with("Control")));
}

#[test]
fn errors_and_malformed_json_fail_closed() {
    struct Broken;
    impl Runner for Broken {
        fn run(&self, spec: Spec) -> Result<String> {
            if spec.args.contains(&"snapshot".to_string()) {
                Ok("{not json".into())
            } else {
                bail!("herdr: connection refused")
            }
        }
    }
    let backend = Backend {
        source: Source::local("default"),
        config: Arc::new(Config::default()),
        runner: Arc::new(Broken),
    };
    assert!(backend.items().is_err());
    assert!(backend.live_agent(&item()).is_err());
}

#[test]
fn approval_screens_are_recognized_precisely() {
    assert!(approval::wants_approval(PROMPT));
    assert!(
        !approval::wants_approval(QUESTION),
        "free-text questions are never approvals"
    );
    assert!(approval::selected_once(PROMPT));
    assert!(!approval::selected_once(
        &PROMPT.replace("› 1.", "  1.").replace("  2.", "› 2.")
    ));
    assert!(!approval::selected_once(
        &PROMPT.replace("Yes, proceed (y)", "Yes, and don't ask again")
    ));
    assert_eq!(
        approval::describe(PROMPT),
        "Would you like to run the following command"
    );
    assert_eq!(approval::ask_label(PROMPT), "command");
    assert_eq!(
        approval::ask_label(
            "Would you like to make the following edits?\n› 1. Yes\nPress enter to confirm"
        ),
        "edits"
    );
}

#[test]
fn manual_answers_validate_agent_and_dialog_before_input() {
    let fake = Fake::new();
    let backend = backend(&fake, "default", false);
    let item = item();
    let details = approval::details(&backend, &item)
        .unwrap()
        .expect("blocked codex agent with a prompt");
    assert_eq!(details.seq, 7);
    assert_eq!(fake.calls("agent read w4:p2 --source detection").len(), 1);
    assert!(approval::answer(&backend, &item, true, false, None, || true).unwrap());
    assert_eq!(fake.calls("send-keys w4:p2 esc").len(), 1);
    *fake.tail.lock().unwrap() = PROMPT.replace("echo hello", "rm -rf /");
    assert!(
        !approval::answer(&backend, &item, false, true, Some(&details), || true).unwrap(),
        "a changed dialog is never answered"
    );
    *fake.tail.lock().unwrap() = QUESTION.into();
    assert!(approval::details(&backend, &item).unwrap().is_none());
    *fake.tail.lock().unwrap() = PROMPT.into();
    fake.agent.lock().unwrap().as_mut().unwrap()["agent_session"]["value"] =
        json!("conversation-2");
    assert!(
        approval::answer(&backend, &item, false, false, None, || true).is_err(),
        "a new occupant blocks input"
    );
    assert_eq!(fake.calls("send-keys w4:p2 enter").len(), 0);
}

#[test]
fn focus_interrupt_and_diffs_use_validated_agent_commands() {
    let fake = Fake::new();
    let backend = backend(&fake, "default", false);
    let item = item();
    backend.focus(&item).unwrap();
    assert_eq!(fake.calls("agent focus w4:p2").len(), 1);
    assert_eq!(fake.calls("/usr/bin/open -a Ghostty").len(), 1);
    backend.interrupt(&item).unwrap();
    assert_eq!(
        fake.calls("send-keys w4:p2 esc").len(),
        1,
        "codex is interrupted with escape"
    );
    fake.agent.lock().unwrap().as_mut().unwrap()["agent_status"] = json!("idle");
    assert!(backend.interrupt(&item).is_err());
    fake.agent.lock().unwrap().as_mut().unwrap()["agent_status"] = json!("blocked");
    backend.open_changed(&item).unwrap();
    assert_eq!(
        fake.calls("tab create --workspace w4 --cwd /tmp/project --label Changes --no-focus")
            .len(),
        1
    );
    assert_eq!(fake.calls("pane run w4:p9").len(), 1);
    assert_eq!(fake.calls("tab focus w4:t9").len(), 1);
    let remote_backend = backend.for_source(remote());
    let remote_item = build_items(&snapshot(), &remote()).unwrap().remove(0);
    remote_backend.open_changed(&remote_item).unwrap();
    assert!(
        fake.calls("rev-parse --show-toplevel")
            .iter()
            .any(|c| c[0] == "/usr/bin/ssh"),
        "remote diffs resolve git on the remote host"
    );
    assert!(
        backend.focus(&remote_item).is_err(),
        "a local backend refuses another machine's agent"
    );
}

#[test]
fn arm_file_persists_scope_and_expiry() {
    let (_dir, path) = arm_file();
    let written = Arm::write(&path, "default", 0, None, 1000.0).unwrap();
    assert_eq!(written.until, Some(2800.0));
    assert!(Arm::read(&path, "default", 1500.0).is_some());
    assert!(Arm::read(&path, "default", 3000.0).is_none(), "expired");
    assert!(
        Arm::read(&path, "work", 1500.0).is_none(),
        "another session"
    );
    fs::write(&path, "1700000000").unwrap();
    assert!(
        Arm::read(&path, "default", 0.0).is_none(),
        "legacy arm files are ignored"
    );
    approval::cycle(&path, "default", None, 0.0).unwrap();
    assert_eq!(Arm::read(&path, "default", 0.0).unwrap().index, 0);
    approval::cycle(&path, "default", None, 0.0).unwrap();
    approval::cycle(&path, "default", None, 0.0).unwrap();
    let forever = Arm::read(&path, "default", 0.0).unwrap();
    assert_eq!((forever.index, forever.until), (2, None));
    approval::cycle(&path, "default", None, 0.0).unwrap();
    assert!(!path.exists(), "cycling past forever disarms");
    let local = item();
    let remote_item = build_items(&snapshot(), &remote()).unwrap().remove(0);
    let session_wide = Arm::write(&path, "default", 2, None, 0.0).unwrap();
    assert!(
        session_wide.covers(&local) && !session_wide.covers(&remote_item),
        "session-wide windows never extend to remote agents"
    );
    let scoped = Arm::write(&path, "default", 2, Some(remote_item.id.clone()), 0.0).unwrap();
    assert!(scoped.covers(&remote_item) && !scoped.covers(&local));
    approval::disarm(&path).unwrap();
    approval::disarm(&path).unwrap();
}

#[test]
fn auto_approval_answers_each_modal_once_and_respects_scope() {
    let fake = Fake::new();
    let backend = backend(&fake, "default", false);
    let (_dir, path) = arm_file();
    let mut frame = Frame::default();
    frame.agents.push(item());
    let mut sent = Sent::new();
    let none = HashSet::new();
    approval::poll(&backend, &frame, &path, &mut sent, false, &none);
    assert_eq!(
        fake.calls("send-keys").len(),
        0,
        "disarmed helpers make no input"
    );
    Arm::write(&path, "default", 2, None, 0.0).unwrap();
    approval::poll(&backend, &frame, &path, &mut sent, false, &none);
    approval::poll(&backend, &frame, &path, &mut sent, false, &none);
    assert_eq!(
        fake.calls("send-keys w4:p2 enter").len(),
        1,
        "unchanged modal is answered once"
    );
    let mut remote_frame = Frame::default();
    remote_frame
        .agents
        .push(build_items(&snapshot(), &remote()).unwrap().remove(0));
    let mut remote_sent = Sent::new();
    approval::poll(
        &backend,
        &remote_frame,
        &path,
        &mut remote_sent,
        false,
        &none,
    );
    approval::poll(
        &backend,
        &remote_frame,
        &path,
        &mut remote_sent,
        true,
        &none,
    );
    assert!(
        fake.calls("send-keys")
            .iter()
            .all(|c| c[0] != "/usr/bin/ssh"),
        "session-wide and --always never approve remote agents"
    );
    let mut other = Frame::default();
    other.agents.push(Item {
        agent_type: "claude".into(),
        ..item()
    });
    approval::poll(&backend, &other, &path, &mut Sent::new(), true, &none);
    assert_eq!(
        fake.calls("send-keys").len(),
        1,
        "non-codex agents are ignored"
    );
}

#[test]
fn session_auto_approves_each_codex_pane_in_a_split_tab_without_conversation_ids() {
    let fake = Fake::new();
    let backend = backend(&fake, "default", false);
    let mut focused = snapshot()["agents"][0].clone();
    focused["agent"] = json!("claude");
    focused["agent_session"]["agent"] = json!("claude");
    focused["focused"] = json!(true);
    let mut agents = vec![focused];
    for n in [3, 4] {
        let mut agent = snapshot()["agents"][0].clone();
        agent["pane_id"] = json!(format!("w4:p{n}"));
        agent["terminal_id"] = json!(format!("term_split_{n}"));
        agent["focused"] = json!(false);
        agent.as_object_mut().unwrap().remove("agent_session");
        agents.push(agent);
    }
    *fake.agents.lock().unwrap() = agents;
    let frame = Frame {
        agents: backend.items().unwrap(),
        ..Frame::default()
    };
    assert!(
        frame
            .agents
            .iter()
            .all(|a| a.tab_id == frame.agents[0].tab_id)
    );
    assert_ne!(frame.agents[1].id, frame.agents[2].id);
    let (_dir, path) = arm_file();
    Arm::write(&path, "default", 2, None, 0.0).unwrap();
    let mut sent = Sent::new();
    approval::poll(&backend, &frame, &path, &mut sent, false, &HashSet::new());
    assert_eq!(fake.calls("send-keys w4:p3 enter").len(), 1);
    assert_eq!(fake.calls("send-keys w4:p4 enter").len(), 1);
    assert_eq!(fake.calls("send-keys").len(), 2);
    for pane in ["w4:p3", "w4:p4"] {
        assert_eq!(
            fake.calls(&format!("agent read {pane} --source detection"))
                .len(),
            2
        );
    }
    // Once the retry cooldown expires, each unchanged modal is still deduplicated.
    for (_, _, at) in sent.values_mut() {
        *at = 0;
    }
    approval::poll(&backend, &frame, &path, &mut sent, false, &HashSet::new());
    assert_eq!(fake.calls("send-keys").len(), 2);
}

#[test]
fn missing_conversation_ids_require_local_session_wide_auto() {
    let fake = Fake::new();
    let backend = backend(&fake, "default", false);
    fake.agent.lock().unwrap().as_mut().unwrap()["agent_session"] = Value::Null;
    let mut snapshot = snapshot();
    snapshot["agents"][0] = fake.agent.lock().unwrap().clone().unwrap();
    let local = build_items(&snapshot, &Source::local("default"))
        .unwrap()
        .remove(0);
    let remote_item = build_items(&snapshot, &remote()).unwrap().remove(0);
    let (_dir, path) = arm_file();
    let frame = Frame {
        agents: vec![local.clone(), remote_item.clone()],
        ..Frame::default()
    };
    let none = HashSet::new();

    // Disarmed, conversation-scoped, and --only windows cannot authorize these panes.
    approval::poll(&backend, &frame, &path, &mut Sent::new(), false, &none);
    for target in [&local, &remote_item] {
        let arm = Arm::write(&path, "default", 2, Some(target.id.clone()), 0.0).unwrap();
        assert!(!arm.covers(target));
        approval::poll(&backend, &frame, &path, &mut Sent::new(), false, &none);
        approval::poll(
            &backend,
            &frame,
            &path,
            &mut Sent::new(),
            true,
            &HashSet::from([target.id.clone()]),
        );
    }
    assert!(fake.calls("send-keys").is_empty());

    // --always is local-session-wide, just like the global AUTO switch.
    approval::poll(&backend, &frame, &path, &mut Sent::new(), true, &none);
    assert_eq!(fake.calls("send-keys w4:p2 enter").len(), 1);
    assert!(
        fake.calls("send-keys")
            .iter()
            .all(|c| c[0] != "/usr/bin/ssh")
    );

    // The relaxed metadata requirement does not relax dialog or selection checks.
    Arm::write(&path, "default", 2, None, 0.0).unwrap();
    for tail in [
        QUESTION.to_owned(),
        PROMPT.replace("Yes, proceed (y)", "Yes, and don't ask again"),
    ] {
        *fake.tail.lock().unwrap() = tail;
        approval::poll(&backend, &frame, &path, &mut Sent::new(), false, &none);
    }
    assert_eq!(fake.calls("send-keys").len(), 1);

    // Live terminal/conversation changes invalidate the snapshot before input.
    *fake.tail.lock().unwrap() = PROMPT.into();
    for (name, value) in [
        ("terminal_id", json!("term_replacement")),
        ("agent_session", json!({"value": "new-conversation"})),
    ] {
        let mut changed = snapshot["agents"][0].clone();
        changed[name] = value;
        *fake.agent.lock().unwrap() = Some(changed);
        approval::poll(&backend, &frame, &path, &mut Sent::new(), false, &none);
    }
    assert_eq!(fake.calls("send-keys").len(), 1);
}

#[test]
fn controls_page_focus_and_disable_unavailable_actions() {
    let mut controls = Controls::new(6);
    let mut frame = Frame::default();
    for n in 0..7 {
        frame.agents.push(Item {
            id: format!("agent-{n}"),
            pane_id: format!("w4:p{n}"),
            state: if n == 3 { State::Idle } else { State::Working },
            ..item()
        });
    }
    controls.update(frame.clone(), 0);
    assert_eq!(controls.pages(), 2);
    controls.down(5, 0);
    assert_eq!(controls.up(5, 100), None);
    assert_eq!(controls.page, 1);
    controls.down(5, 0);
    assert_eq!(controls.up(5, 900), Some(Effect::CycleGlobal));
    controls.page = 0;
    controls.down(0, 0);
    assert_eq!(
        controls.up(0, 100),
        Some(Effect::Run(Action::Focus, "agent-0".into())),
        "tap focuses directly"
    );
    controls.down(3, 0);
    assert_eq!(controls.up(3, 800), None);
    assert_eq!(
        controls.focus.as_deref(),
        Some("agent-3"),
        "hold opens the action page"
    );
    controls.down(1, 1000);
    assert_eq!(
        controls.up(1, 1100),
        None,
        "APPRV is disabled unless blocked codex"
    );
    controls.down(3, 1000);
    assert_eq!(
        controls.up(3, 1100),
        None,
        "idle agents cannot be interrupted"
    );
    controls.down(4, 1000);
    assert_eq!(
        controls.up(4, 1100),
        Some(Effect::Run(Action::Diffs, "agent-3".into()))
    );
    controls.down(5, 1000);
    assert_eq!(controls.up(5, 1100), None);
    assert!(controls.focus.is_none(), "BACK returns to the overview");
    controls.down(2, 2000);
    controls.up(2, 2800);
    let mut blocked = frame.clone();
    blocked.agents[2].state = State::Blocked;
    blocked.sort();
    controls.update(blocked, 3000);
    controls.down(1, 3000);
    assert_eq!(
        controls.up(1, 3100),
        Some(Effect::Run(Action::Approve, "agent-2".into()))
    );
    controls.down(1, 3000);
    assert_eq!(
        controls.up(1, 3900),
        Some(Effect::Run(Action::Deny, "agent-2".into()))
    );
    controls.down(2, 3000);
    assert_eq!(
        controls.up(2, 3100),
        Some(Effect::Auto("agent-2".into(), false))
    );
    frame.agents.remove(2);
    controls.update(frame, 4000);
    assert!(
        controls.focus.is_none(),
        "a vanished agent closes its action page"
    );
    controls.update(Frame::default(), 5000);
    assert_eq!((controls.pages(), controls.page), (1, 0));
}

#[test]
fn fleet_keeps_local_tiles_when_remote_is_offline() {
    let fake = Fake::new();
    *fake.machines.lock().unwrap() = PROFILE.into();
    *fake.remote_down.lock().unwrap() = true;
    let mut fleet = Fleet::new(backend(&fake, "default", true));
    let frame = fleet.inspect();
    assert_eq!(frame.agents.len(), 1);
    assert!(frame.agents[0].show_machine && frame.agents[0].machine_label == "Local");
    let mini = frame
        .machines
        .iter()
        .find(|m| m.id == "saved-mini")
        .unwrap();
    assert_eq!(mini.state, Connection::Offline);
    assert!(mini.error.contains("timed out"));
    assert_eq!(frame.online(), 1);
    let remote_item = build_items(&snapshot(), &remote()).unwrap().remove(0);
    assert!(
        fleet.backend.resolve(&remote_item).is_ok(),
        "profile is still saved, so actions revalidate on the remote host"
    );
    *fake.machines.lock().unwrap() = PROFILE.replace("\"enabled\":true", "\"enabled\":false");
    std::thread::sleep(std::time::Duration::from_millis(2100));
    let frame = fleet.inspect();
    assert!(
        frame.machines.iter().all(|m| m.id != "saved-mini"),
        "disabled profiles disappear"
    );
    assert!(
        fleet.backend.resolve(&remote_item).is_err(),
        "stale remote actions are rejected"
    );
    let local_only = Fleet::new(backend(&fake, "default", false)).inspect();
    assert_eq!(local_only.machines.len(), 1);
    assert!(!local_only.agents[0].show_machine);
}

#[test]
fn review_shows_status_staged_unstaged_and_untracked() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path();
    let git = |args: &[&str]| {
        assert!(
            Command::new("git")
                .args([
                    "-c",
                    "commit.gpgsign=false",
                    "-c",
                    "core.hooksPath=/dev/null"
                ])
                .args(args)
                .current_dir(root)
                .env("GIT_AUTHOR_NAME", "t")
                .env("GIT_AUTHOR_EMAIL", "t@t")
                .env("GIT_COMMITTER_NAME", "t")
                .env("GIT_COMMITTER_EMAIL", "t@t")
                .output()
                .unwrap()
                .status
                .success()
        )
    };
    git(&["init", "-q"]);
    fs::write(root.join("a.txt"), "one\n").unwrap();
    git(&["add", "a.txt"]);
    git(&["commit", "-q", "-m", "init"]);
    fs::write(root.join("a.txt"), "staged\n").unwrap();
    git(&["add", "a.txt"]);
    fs::write(root.join("a.txt"), "unstaged\n").unwrap();
    fs::write(root.join("new.txt"), "x").unwrap();
    let output = Command::new("/bin/sh")
        .arg("-c")
        .arg(review_command(root.to_str().unwrap()))
        .env("LESS", "")
        .output()
        .unwrap();
    let text = regex::Regex::new("\x1b\\[[0-9;]*m")
        .unwrap()
        .replace_all(&String::from_utf8_lossy(&output.stdout), "")
        .into_owned();
    assert!(
        text.contains("?? new.txt") && text.contains("+staged") && text.contains("+unstaged"),
        "{text}"
    );
}

#[test]
fn tiles_render_for_every_state_and_key_size() {
    for (w, h) in [(72, 72), (80, 80), (96, 96), (120, 120)] {
        let renderer = Renderer::new(w, h).unwrap();
        for state in [
            State::Blocked,
            State::Working,
            State::Done,
            State::Idle,
            State::Unknown,
        ] {
            let tile = renderer.tile(
                Some(&Item {
                    state,
                    show_machine: true,
                    state_since: 1,
                    ..item()
                }),
                61_000,
                [0; 3],
            );
            assert_eq!(tile.dimensions(), (w, h));
        }
        assert_eq!(
            renderer
                .action("APPRV", "command", herdr_streamdeck::ui::AMBER, false)
                .dimensions(),
            (w, h)
        );
        let mut frame = Frame::default();
        frame.agents.push(item());
        frame.machines.push(MachineStatus {
            id: "mini".into(),
            label: "Mac Mini Dev".into(),
            state: Connection::Offline,
            error: "down".into(),
        });
        let status = renderer.status(
            &frame,
            1,
            3,
            Some(&Arm {
                session: "default".into(),
                until: None,
                index: 2,
                only: None,
            }),
            0,
        );
        assert_eq!(status.dimensions(), (w, h));
        assert_ne!(status, renderer.blank([40, 42, 50]));
    }
}
