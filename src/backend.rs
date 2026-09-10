use crate::{model::*, process};
use anyhow::{Context, Result, ensure};
use serde_json::Value;
use std::{
    collections::{HashMap, HashSet},
    process::Command,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
        mpsc::{self, Receiver, SyncSender},
    },
    thread::{self, JoinHandle},
    time::{Duration, Instant},
};

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Spec {
    pub program: String,
    pub args: Vec<String>,
}
impl Spec {
    pub fn new(program: &str, args: Vec<String>) -> Self {
        Self {
            program: program.into(),
            args,
        }
    }
    pub fn command(&self) -> Command {
        let mut c = Command::new(&self.program);
        c.args(&self.args);
        process::clean_context(&mut c);
        c
    }
}
pub trait Runner: Send + Sync {
    fn run(&self, spec: Spec) -> Result<String>;
}
pub struct SystemRunner;
impl Runner for SystemRunner {
    fn run(&self, spec: Spec) -> Result<String> {
        process::text(spec.command())
    }
}

#[derive(Clone)]
pub struct Backend {
    pub config: Arc<Config>,
    pub source: Source,
    pub runner: Arc<dyn Runner>,
}
impl Backend {
    pub fn local(config: Config) -> Self {
        Self {
            source: Source::local(&config.session),
            config: Arc::new(config),
            runner: Arc::new(SystemRunner),
        }
    }
    pub fn for_source(&self, source: Source) -> Self {
        Self {
            source,
            ..self.clone()
        }
    }
    pub fn ssh_spec(&self, script: &str) -> Result<Spec> {
        let target = self.source.target.as_ref().context("Missing SSH target")?;
        Ok(Spec::new(
            "/usr/bin/ssh",
            [
                "-T",
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                "ConnectTimeout=3",
                "-o",
                "ServerAliveInterval=3",
                "-o",
                "ServerAliveCountMax=1",
                "--",
            ]
            .iter()
            .map(|s| s.to_string())
            .chain([
                target.clone(),
                process::shell_join(&["/bin/sh".into(), "-c".into(), script.into()]),
            ])
            .collect(),
        ))
    }
    pub fn spec(&self, args: &[&str]) -> Result<Spec> {
        let mut full = Vec::new();
        if self.source.session != "default" {
            full.extend(["--session".into(), self.source.session.clone()]);
        }
        full.extend(args.iter().map(|s| s.to_string()));
        if !self.source.remote() {
            return Ok(Spec::new("herdr", full));
        }
        let script = format!(
            r#"for name in $(env | sed -n 's/^\(HERDR_[A-Za-z0-9_]*\)=.*/\1/p'); do unset "$name"; done
for bin in "$(command -v herdr)" "$HOME/.local/bin/herdr" /opt/homebrew/bin/herdr /usr/local/bin/herdr "$HOME/.nix-profile/bin/herdr"; do
    if [ -n "$bin" ] && [ -x "$bin" ]; then exec "$bin" {}; fi
done
printf '%s\n' 'Herdr executable not found on remote machine' >&2
exit 127"#,
            process::shell_join(&full)
        );
        self.ssh_spec(&script)
    }
    pub fn raw(&self, args: &[&str]) -> Result<String> {
        self.runner.run(self.spec(args)?)
    }
    pub fn run(&self, args: &[&str]) -> Result<Value> {
        let text = self.raw(args)?;
        let response: Value = serde_json::from_str(&text).context("Invalid Herdr JSON response")?;
        let result = response
            .get("result")
            .filter(|v| v.is_object())
            .context("Herdr response has no result object")?;
        Ok(result.clone())
    }
    pub fn saved_machines(&self) -> Result<Vec<Source>> {
        if !self.config.include_remote_machines {
            return Ok(Vec::new());
        }
        parse_machines(&self.raw(&["machine", "list", "--json"])?)
    }
    pub fn items(&self) -> Result<Vec<Item>> {
        let response = self.run(&["api", "snapshot"])?;
        build_items(
            response.get("snapshot").context("Missing Herdr snapshot")?,
            &self.source,
        )
    }
    pub fn resolve(&self, item: &Item) -> Result<Self> {
        let source = item.source();
        if !source.remote() {
            ensure!(
                source.same_target(&Source::local(&self.config.session)),
                "Agent belongs to another local session"
            );
        } else {
            ensure!(
                self.saved_machines()?
                    .iter()
                    .any(|s| s.same_target(&source)),
                "Remote machine changed, was disabled, or was removed"
            );
        }
        Ok(self.for_source(source))
    }
    pub fn live_agent(&self, item: &Item) -> Result<Value> {
        ensure!(
            self.source.same_target(&item.source()) && !item.pane_id.is_empty(),
            "Agent belongs to another machine or session"
        );
        let response = self.run(&["agent", "get", &item.pane_id])?;
        let agent = response.get("agent").context("Missing agent")?;
        ensure!(
            identity(agent, &self.source).as_deref() == Some(&item.id),
            "Agent changed or exited; select its current tile"
        );
        Ok(agent.clone())
    }
    pub fn read_tail(&self, item: &Item) -> Result<String> {
        self.live_agent(item)?;
        self.raw(&["agent", "read", &item.pane_id, "--source", "detection"])
    }
    pub fn send_keys(&self, item: &Item, key: &str, blocked_seq: Option<u64>) -> Result<()> {
        let agent = self.live_agent(item)?;
        if let Some(expected) = blocked_seq {
            ensure!(
                agent_state(&agent) == State::Blocked && seq(&agent) == expected,
                "Approval changed before input could be sent"
            );
        }
        self.run(&["agent", "send-keys", &item.pane_id, key])?;
        Ok(())
    }
    fn raise_terminal(&self) -> Result<()> {
        if cfg!(target_os = "macos") && !self.config.terminal_app.is_empty() {
            self.runner.run(Spec::new(
                "/usr/bin/open",
                vec!["-a".into(), self.config.terminal_app.clone()],
            ))?;
        }
        Ok(())
    }
    pub fn focus(&self, item: &Item) -> Result<()> {
        self.live_agent(item)?;
        self.run(&["agent", "focus", &item.pane_id])?;
        self.raise_terminal()
    }
    pub fn interrupt(&self, item: &Item) -> Result<()> {
        ensure!(
            agent_state(&self.live_agent(item)?).active(),
            "Agent is no longer working or blocked"
        );
        self.send_keys(
            item,
            if item.agent_type == "codex" {
                "esc"
            } else {
                "ctrl+c"
            },
            None,
        )
    }
    pub fn open_changed(&self, item: &Item) -> Result<()> {
        let agent = self.live_agent(item)?;
        let cwd = field(&agent, "foreground_cwd")
            .or_else(|| field(&agent, "cwd"))
            .context("Agent has no working directory")?;
        let args: Vec<String> = ["git", "-C", cwd, "rev-parse", "--show-toplevel"]
            .iter()
            .map(|s| s.to_string())
            .collect();
        let spec = if self.source.remote() {
            self.ssh_spec(&process::shell_join(&args))?
        } else {
            Spec::new("git", args[1..].to_vec())
        };
        let root = self.runner.run(spec)?;
        let root = root.trim_end_matches('\n');
        ensure!(!root.is_empty(), "Git returned no checkout path");
        let workspace = field(&agent, "workspace_id").context("Agent has no workspace")?;
        let created = self.run(&[
            "tab",
            "create",
            "--workspace",
            workspace,
            "--cwd",
            root,
            "--label",
            "Changes",
            "--no-focus",
        ])?;
        let pane = created
            .get("root_pane")
            .and_then(|v| field(v, "pane_id"))
            .context("Missing new review pane")?;
        let tab = created
            .get("tab")
            .and_then(|v| field(v, "tab_id"))
            .context("Missing new review tab")?;
        self.run(&["pane", "run", pane, &review_command(root)])?;
        self.run(&["tab", "focus", tab])?;
        self.raise_terminal()
    }
}

pub fn review_command(root: &str) -> String {
    let script = r#"cd "$1" || exit
{
    printf '\nStatus (includes untracked files)\n'
    git -c color.ui=always status --short
    printf '\nStaged changes\n'
    git -c color.ui=always diff --cached --no-ext-diff --no-textconv
    printf '\nUnstaged changes\n'
    git -c color.ui=always diff --no-ext-diff --no-textconv
} | less -R"#;
    process::shell_join(&[
        "/bin/sh".into(),
        "-c".into(),
        script.into(),
        "herdr-changes".into(),
        root.into(),
    ])
}

struct Poll {
    backend: Backend,
    job: Option<JoinHandle<Result<Vec<Item>>>>,
    items: Vec<Item>,
    state: Connection,
    error: String,
    next: Instant,
    observed: HashMap<String, (State, u64, u64)>,
}
impl Poll {
    fn new(backend: Backend) -> Self {
        Self {
            backend,
            job: None,
            items: Vec::new(),
            state: Connection::Connecting,
            error: String::new(),
            next: Instant::now(),
            observed: HashMap::new(),
        }
    }
    fn tick(&mut self) {
        if self.job.as_ref().is_some_and(|job| job.is_finished()) {
            match self
                .job
                .take()
                .unwrap()
                .join()
                .unwrap_or_else(|_| Err(anyhow::anyhow!("Snapshot worker failed")))
            {
                Ok(mut items) => {
                    let now = now_ms();
                    let mut observed = HashMap::new();
                    for item in &mut items {
                        item.state_since = self
                            .observed
                            .get(&item.id)
                            .filter(|(s, seq, _)| *s == item.state && *seq == item.state_change_seq)
                            .map(|(_, _, since)| *since)
                            .unwrap_or(now);
                        observed.insert(
                            item.id.clone(),
                            (item.state, item.state_change_seq, item.state_since),
                        );
                    }
                    self.observed = observed;
                    self.items = items;
                    self.state = Connection::Online;
                    self.error.clear();
                }
                Err(error) => {
                    self.items.clear();
                    self.state = Connection::Offline;
                    self.error = format!("{error:#}");
                }
            }
            self.next = Instant::now()
                + Duration::from_secs(if self.state == Connection::Offline {
                    4
                } else {
                    2
                });
        }
        if self.job.is_none() && Instant::now() >= self.next {
            let backend = self.backend.clone();
            self.job = Some(thread::spawn(move || backend.items()));
        }
    }
}
pub struct Fleet {
    pub backend: Backend,
    polls: HashMap<String, Poll>,
    catalog: Vec<Source>,
    catalog_error: Option<String>,
    next_catalog: Instant,
}
impl Fleet {
    pub fn new(backend: Backend) -> Self {
        Self {
            backend,
            polls: HashMap::new(),
            catalog: Vec::new(),
            catalog_error: None,
            next_catalog: Instant::now(),
        }
    }
    pub fn tick(&mut self) -> Frame {
        if Instant::now() >= self.next_catalog {
            match self.backend.saved_machines() {
                Ok(machines) => {
                    self.catalog = machines;
                    self.catalog_error = None;
                }
                Err(error) => {
                    self.catalog.clear();
                    self.catalog_error = Some(format!("{error:#}"));
                }
            }
            self.next_catalog = Instant::now() + Duration::from_secs(2);
        }
        let sources: Vec<_> = std::iter::once(Source::local(&self.backend.config.session))
            .chain(self.catalog.iter().cloned())
            .collect();
        let ids: HashSet<_> = sources.iter().map(|s| s.id.as_str()).collect();
        self.polls.retain(|id, _| ids.contains(id.as_str()));
        let mut frame = Frame::default();
        for source in sources {
            let poll = self
                .polls
                .entry(source.id.clone())
                .or_insert_with(|| Poll::new(self.backend.for_source(source.clone())));
            if !poll.backend.source.same_target(&source) {
                *poll = Poll::new(self.backend.for_source(source.clone()));
            }
            poll.tick();
            frame
                .agents
                .extend(poll.items.iter().cloned().map(|mut item| {
                    item.machine_label = source.label.clone();
                    item.show_machine = !self.catalog.is_empty();
                    item
                }));
            frame.machines.push(MachineStatus {
                id: source.id,
                label: source.label,
                state: poll.state.clone(),
                error: poll.error.clone(),
            });
        }
        if let Some(error) = &self.catalog_error {
            frame.machines.push(MachineStatus {
                id: "catalog".into(),
                label: "Saved machines".into(),
                state: Connection::Offline,
                error: error.clone(),
            });
        }
        frame.sort();
        frame
    }
    pub fn inspect(&mut self) -> Frame {
        loop {
            let frame = self.tick();
            if frame
                .machines
                .iter()
                .all(|m| m.state != Connection::Connecting)
            {
                return frame;
            }
            thread::sleep(Duration::from_millis(25));
        }
    }
}
pub struct Monitor {
    pub frames: Receiver<Frame>,
    stop: Arc<AtomicBool>,
}
impl Monitor {
    pub fn start(backend: Backend) -> Self {
        let stop = Arc::new(AtomicBool::new(false));
        let flag = stop.clone();
        let (sender, frames) = mpsc::sync_channel(1);
        thread::spawn(move || monitor_loop(backend, flag, sender));
        Self { frames, stop }
    }
}
impl Drop for Monitor {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
    }
}
fn monitor_loop(backend: Backend, stop: Arc<AtomicBool>, sender: SyncSender<Frame>) {
    let mut fleet = Fleet::new(backend);
    let mut connections = Vec::new();
    while !stop.load(Ordering::Relaxed) {
        let frame = fleet.tick();
        if frame.machines != connections {
            for machine in &frame.machines {
                eprintln!(
                    "Herdr {}: {}{}",
                    machine.label,
                    machine.state.as_str(),
                    if machine.error.is_empty() {
                        String::new()
                    } else {
                        format!(" — {}", machine.error)
                    }
                );
            }
            connections.clone_from(&frame.machines);
        }
        if matches!(
            sender.try_send(frame),
            Err(mpsc::TrySendError::Disconnected(_))
        ) {
            break;
        }
        thread::sleep(Duration::from_millis(100));
    }
}
