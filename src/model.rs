use anyhow::{Context, Result, ensure};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::{
    collections::{BTreeMap, HashMap, HashSet},
    fs,
    path::{Path, PathBuf},
    time::{SystemTime, UNIX_EPOCH},
};

pub fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as u64
}
pub fn home() -> Result<PathBuf> {
    std::env::var_os("HOME")
        .map(PathBuf::from)
        .context("HOME is not set")
}
pub fn config_path() -> Result<PathBuf> {
    Ok(home()?.join(".config/herdr-streamdeck/config.json"))
}
pub fn armed_path() -> Result<PathBuf> {
    Ok(home()?.join(".herdr-streamdeck-armed"))
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(default)]
pub struct Config {
    pub session: String,
    pub terminal_app: String,
    pub include_remote_machines: bool,
}
impl Default for Config {
    fn default() -> Self {
        Self {
            session: "default".into(),
            terminal_app: "Ghostty".into(),
            include_remote_machines: true,
        }
    }
}
impl Config {
    pub fn load(path: &Path) -> Result<Self> {
        let config: Self = if path.exists() {
            serde_json::from_slice(&fs::read(path)?)
                .with_context(|| format!("Invalid config: {}", path.display()))?
        } else {
            Self::default()
        };
        ensure!(!config.session.is_empty(), "Session cannot be empty");
        Ok(config)
    }
}

#[derive(Clone, Copy, Debug, Default, Deserialize, Serialize, PartialEq, Eq, PartialOrd, Ord)]
#[serde(rename_all = "lowercase")]
pub enum State {
    Blocked,
    Working,
    Done,
    Idle,
    #[default]
    #[serde(other)]
    Unknown,
}
impl State {
    pub fn active(self) -> bool {
        matches!(self, Self::Working | Self::Blocked)
    }
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Blocked => "blocked",
            Self::Working => "working",
            Self::Done => "done",
            Self::Idle => "idle",
            Self::Unknown => "unknown",
        }
    }
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
pub struct Source {
    pub id: String,
    pub label: String,
    pub target: Option<String>,
    pub session: String,
}
impl Source {
    pub fn local(session: &str) -> Self {
        Self {
            id: "local".into(),
            label: "Local".into(),
            target: None,
            session: session.into(),
        }
    }
    pub fn same_target(&self, other: &Self) -> bool {
        self.id == other.id && self.target == other.target && self.session == other.session
    }
    pub fn remote(&self) -> bool {
        self.id != "local"
    }
}
#[derive(Deserialize)]
struct Profile {
    id: String,
    label: String,
    target: String,
    session: String,
    enabled: bool,
}
pub fn parse_machines(text: &str) -> Result<Vec<Source>> {
    let records: Vec<Profile> = serde_json::from_str(text)
        .context("Invalid saved machine catalog (Herdr 0.9.0+ required)")?;
    let mut seen = HashSet::new();
    let mut sources = Vec::new();
    for p in records {
        ensure!(
            !p.id.is_empty() && p.id != "local" && seen.insert(p.id.clone()),
            "Duplicate or invalid machine ID"
        );
        ensure!(
            !p.label.is_empty()
                && !p.session.is_empty()
                && !p.target.is_empty()
                && !p.target.starts_with('-')
                && !p.target.chars().any(char::is_whitespace),
            "Invalid machine profile"
        );
        if p.enabled {
            sources.push(Source {
                id: p.id,
                label: p.label,
                target: Some(p.target),
                session: p.session,
            });
        }
    }
    Ok(sources)
}

// Match Python's json.dumps(sort_keys=True, ensure_ascii=True) to keep existing
// conversation identities and scoped arm files valid during the migration.
fn python_json(value: &Value) -> String {
    match value {
        Value::Array(values) => format!(
            "[{}]",
            values
                .iter()
                .map(python_json)
                .collect::<Vec<_>>()
                .join(", ")
        ),
        Value::Object(values) => {
            let sorted: BTreeMap<_, _> = values.iter().collect();
            format!(
                "{{{}}}",
                sorted
                    .iter()
                    .map(|(k, v)| format!(
                        "{}: {}",
                        python_json(&Value::String((*k).clone())),
                        python_json(v)
                    ))
                    .collect::<Vec<_>>()
                    .join(", ")
            )
        }
        Value::String(_) => serde_json::to_string(value)
            .unwrap()
            .chars()
            .map(|c| {
                if c as u32 >= 127 {
                    c.encode_utf16(&mut [0; 2])
                        .iter()
                        .map(|unit| format!("\\u{unit:04x}"))
                        .collect()
                } else {
                    c.to_string()
                }
            })
            .collect(),
        _ => value.to_string(),
    }
}
pub fn field<'a>(value: &'a Value, name: &str) -> Option<&'a str> {
    value
        .get(name)
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
}
pub fn seq(agent: &Value) -> u64 {
    agent
        .get("state_change_seq")
        .and_then(Value::as_u64)
        .unwrap_or(0)
}
pub fn agent_state(agent: &Value) -> State {
    serde_json::from_value(agent.get("agent_status").cloned().unwrap_or(Value::Null))
        .unwrap_or_default()
}
pub fn identity(agent: &Value, source: &Source) -> Option<String> {
    field(agent, "pane_id")?;
    let mut values = vec![
        Value::String(source.session.clone()),
        Value::String(field(agent, "terminal_id")?.into()),
        Value::String(field(agent, "agent")?.into()),
        agent.get("agent_session").cloned().unwrap_or(Value::Null),
    ];
    if source.remote() {
        values.push(source.id.clone().into());
        values.push(source.target.clone()?.into());
    }
    Some(format!(
        "{:x}",
        Sha256::digest(python_json(&Value::Array(values)).as_bytes())
    ))
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct Item {
    pub id: String,
    pub session: String,
    pub pane_id: String,
    pub machine_id: String,
    pub machine_label: String,
    pub machine_target: Option<String>,
    pub terminal_id: String,
    pub agent_session: Option<Value>,
    pub workspace_id: Option<String>,
    pub tab_id: Option<String>,
    pub label: String,
    pub cwd: Option<String>,
    pub sub: String,
    pub agent_type: String,
    pub state: State,
    pub state_change_seq: u64,
    pub state_since: u64,
    pub unread: bool,
    pub show_machine: bool,
}
impl Item {
    pub fn source(&self) -> Source {
        Source {
            id: self.machine_id.clone(),
            label: self.machine_label.clone(),
            target: self.machine_target.clone(),
            session: self.session.clone(),
        }
    }
    pub fn can_auto(&self) -> bool {
        self.agent_type == "codex" && self.agent_session.as_ref().is_some_and(|s| !s.is_null())
    }
}
pub fn build_items(snapshot: &Value, source: &Source) -> Result<Vec<Item>> {
    let agents = snapshot
        .get("agents")
        .and_then(Value::as_array)
        .context("Herdr snapshot has no agent list")?;
    let lookup = |kind: &str, id: &str| -> HashMap<&str, &Value> {
        snapshot
            .get(kind)
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
            .filter_map(|v| field(v, id).map(|id| (id, v)))
            .collect()
    };
    let workspaces = lookup("workspaces", "workspace_id");
    let tabs = lookup("tabs", "tab_id");
    let mut items = Vec::new();
    for a in agents {
        let Some(id) = identity(a, source) else {
            continue;
        };
        let workspace = field(a, "workspace_id")
            .and_then(|id| workspaces.get(id))
            .copied()
            .unwrap_or(&Value::Null);
        let tab = field(a, "tab_id")
            .and_then(|id| tabs.get(id))
            .copied()
            .unwrap_or(&Value::Null);
        let cwd = field(a, "foreground_cwd").or_else(|| field(a, "cwd"));
        let label = field(workspace, "label")
            .or_else(|| cwd.and_then(|p| Path::new(p).file_name()?.to_str()))
            .unwrap_or("workspace");
        let kind = field(a, "agent").unwrap();
        let sub = field(a, "title")
            .or_else(|| field(a, "name"))
            .or_else(|| field(a, "terminal_title_stripped"))
            .or_else(|| field(tab, "label"))
            .unwrap_or(kind);
        let state = agent_state(a);
        items.push(Item {
            id,
            session: source.session.clone(),
            pane_id: field(a, "pane_id").unwrap().into(),
            machine_id: source.id.clone(),
            machine_label: source.label.clone(),
            machine_target: source.target.clone(),
            terminal_id: field(a, "terminal_id").unwrap().into(),
            agent_session: a.get("agent_session").filter(|v| !v.is_null()).cloned(),
            workspace_id: field(a, "workspace_id").map(str::to_owned),
            tab_id: field(a, "tab_id").map(str::to_owned),
            label: label.into(),
            cwd: cwd.map(str::to_owned),
            sub: sub.into(),
            agent_type: kind.into(),
            state,
            state_change_seq: seq(a),
            state_since: 0,
            unread: state == State::Done,
            show_machine: false,
        });
    }
    Ok(items)
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum Connection {
    Connecting,
    Online,
    Offline,
}
impl Connection {
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::Connecting => "connecting",
            Self::Online => "online",
            Self::Offline => "offline",
        }
    }
}
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
pub struct MachineStatus {
    pub id: String,
    pub label: String,
    pub state: Connection,
    pub error: String,
}
#[derive(Clone, Debug, Default, Serialize, Deserialize, PartialEq)]
pub struct Frame {
    pub machines: Vec<MachineStatus>,
    pub agents: Vec<Item>,
}
impl Frame {
    pub fn blocked(&self) -> usize {
        self.agents
            .iter()
            .filter(|a| a.state == State::Blocked)
            .count()
    }
    pub fn online(&self) -> usize {
        self.machines
            .iter()
            .filter(|m| m.state == Connection::Online)
            .count()
    }
    pub fn sort(&mut self) {
        self.agents
            .sort_by(|a, b| (a.state, a.state_since, &a.id).cmp(&(b.state, b.state_since, &b.id)));
    }
}
