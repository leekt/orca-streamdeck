use crate::{backend::Backend, model::*};
use anyhow::{Context, Result};
use regex::Regex;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    collections::{HashMap, HashSet},
    fs,
    io::Write,
    path::Path,
    sync::OnceLock,
};

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct Arm {
    pub session: String,
    pub until: Option<f64>,
    pub index: usize,
    pub only: Option<String>,
}
impl Arm {
    pub fn read(path: &Path, session: &str, now: f64) -> Option<Self> {
        let arm: Self = serde_json::from_slice(&fs::read(path).ok()?).ok()?;
        if arm.session != session
            || arm.index > 2
            || arm.only.as_ref().is_some_and(String::is_empty)
            || arm
                .until
                .is_some_and(|until| !until.is_finite() || until <= now)
        {
            return None;
        }
        Some(arm)
    }
    pub fn write(
        path: &Path,
        session: &str,
        index: usize,
        only: Option<String>,
        now: f64,
    ) -> Result<Self> {
        anyhow::ensure!(index <= 2, "Invalid approval duration");
        let until = match index {
            0 => Some(now + 1800.0),
            1 => Some(now + 3600.0),
            _ => None,
        };
        let arm = Self {
            session: session.into(),
            until,
            index,
            only,
        };
        let mut temporary =
            tempfile::NamedTempFile::new_in(path.parent().context("Invalid arm path")?)?;
        serde_json::to_writer(&mut temporary, &arm)?;
        temporary.flush()?;
        temporary.as_file().sync_all()?;
        temporary.persist(path).map_err(|error| error.error)?;
        Ok(arm)
    }
    pub fn covers(&self, item: &Item) -> bool {
        match &self.only {
            Some(id) => item.can_auto() && id == &item.id,
            None => item.machine_id == "local",
        }
    }
    pub fn badge(&self, now: f64) -> String {
        match self.until {
            None => "AUTO ON".into(),
            Some(until) => format!(
                "AUTO {}",
                crate::ui::age(((until - now).max(0.0) * 1000.0) as u64)
            ),
        }
    }
}
pub fn disarm(path: &Path) -> Result<()> {
    match fs::remove_file(path) {
        Ok(()) => Ok(()),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(e) => Err(e.into()),
    }
}
pub fn cycle(path: &Path, session: &str, only: Option<String>, now: f64) -> Result<()> {
    let old = Arm::read(path, session, now).filter(|arm| arm.only == only);
    match old {
        Some(Arm { index: 2, .. }) => disarm(path),
        old => {
            Arm::write(path, session, old.map_or(0, |a| a.index + 1), only, now)?;
            Ok(())
        }
    }
}
const MARKERS: [&str; 6] = [
    "Press enter to confirm",
    "Would you like to run the following command?",
    "Would you like to make the following edits?",
    "Would you like to grant these permissions?",
    "Do you want to approve network access to",
    "needs your approval.",
];
fn squash(s: &str) -> String {
    s.chars()
        .filter(|c| !c.is_whitespace())
        .flat_map(char::to_lowercase)
        .collect()
}
pub fn wants_approval(tail: &str) -> bool {
    let flat = squash(tail);
    flat.contains("1.yes")
        && !flat.contains("entertosubmitanswer")
        && MARKERS.iter().any(|marker| flat.contains(&squash(marker)))
}
pub fn selected_once(tail: &str) -> bool {
    static PATTERN: OnceLock<Regex> = OnceLock::new();
    let re = PATTERN.get_or_init(|| Regex::new(r"(?m)^[\t ]*[›❯>][\t ]*1[.)][\t ]*(.+)$").unwrap());
    re.captures(tail).is_some_and(|c| {
        matches!(
            c[1].split_whitespace()
                .collect::<Vec<_>>()
                .join(" ")
                .to_lowercase()
                .as_str(),
            "yes" | "yes (y)" | "yes, proceed" | "yes, proceed (y)"
        )
    })
}
pub fn describe(tail: &str) -> String {
    MARKERS[1..]
        .iter()
        .find(|marker| squash(tail).contains(&squash(marker)))
        .unwrap_or(&"command approval")
        .trim_end_matches(['?', '.'])
        .to_string()
}
pub fn ask_label(tail: &str) -> &'static str {
    let description = describe(tail).to_lowercase();
    for (word, label) in [
        ("command", "command"),
        ("edits", "edits"),
        ("permissions", "perms"),
        ("network", "network"),
    ] {
        if description.contains(word) {
            return label;
        }
    }
    "tool"
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Approval {
    pub tail: String,
    pub seq: u64,
}
pub fn details(backend: &Backend, item: &Item) -> Result<Option<Approval>> {
    let before = backend.live_agent(item)?;
    if field(&before, "agent") != Some("codex") || agent_state(&before) != State::Blocked {
        return Ok(None);
    }
    let tail = backend.read_tail(item)?;
    let after = backend.live_agent(item)?;
    if agent_state(&after) != State::Blocked
        || seq(&before) != seq(&after)
        || !wants_approval(&tail)
    {
        return Ok(None);
    }
    Ok(Some(Approval {
        tail,
        seq: seq(&after),
    }))
}
pub fn answer(
    backend: &Backend,
    item: &Item,
    deny: bool,
    automatic: bool,
    expected: Option<&Approval>,
    guard: impl Fn() -> bool,
) -> Result<bool> {
    let Some(current) = details(backend, item)? else {
        return Ok(false);
    };
    if expected.is_some_and(|expected| expected != &current)
        || (automatic && !selected_once(&current.tail))
        || !guard()
    {
        return Ok(false);
    }
    backend.send_keys(item, if deny { "esc" } else { "enter" }, Some(current.seq))?;
    Ok(true)
}
pub type Sent = HashMap<String, (u64, String, u64)>;
pub fn poll(
    backend: &Backend,
    frame: &Frame,
    arm_path: &Path,
    sent: &mut Sent,
    always: bool,
    only: &HashSet<String>,
) {
    let ids: HashSet<_> = frame.agents.iter().map(|a| &a.id).collect();
    sent.retain(|id, _| ids.contains(id));
    for item in &frame.agents {
        // Herdr may recognize Codex in a newly split pane before it has a
        // conversation ID. A local session-wide window covers that pane;
        // conversation-scoped windows still require the conversation identity.
        if item.state != State::Blocked
            || item.agent_type != "codex"
            || (!only.is_empty() && (!item.can_auto() || !only.contains(&item.id)))
        {
            continue;
        }
        let guard = || {
            if always {
                !item.source().remote() || only.contains(&item.id)
            } else {
                Arm::read(arm_path, &backend.config.session, now_ms() as f64 / 1000.0)
                    .is_some_and(|arm| arm.covers(item))
            }
        };
        if !guard()
            || sent
                .get(&item.id)
                .is_some_and(|(_, _, at)| now_ms().saturating_sub(*at) < 4000)
        {
            continue;
        }
        let result = (|| -> Result<()> {
            let target = backend.resolve(item)?;
            let Some(details) = details(&target, item)? else {
                return Ok(());
            };
            if !selected_once(&details.tail) {
                return Ok(());
            }
            let digest = format!("{:x}", Sha256::digest(details.tail.as_bytes()));
            if sent
                .get(&item.id)
                .is_some_and(|(seq, hash, _)| *seq == details.seq && hash == &digest)
            {
                return Ok(());
            }
            if answer(&target, item, false, true, Some(&details), guard)? {
                sent.insert(item.id.clone(), (details.seq, digest, now_ms()));
                eprintln!(
                    "Approved {} · {} ({}): {}",
                    item.machine_label,
                    item.label,
                    item.pane_id,
                    describe(&details.tail)
                );
            }
            Ok(())
        })();
        if let Err(error) = result {
            eprintln!("Skipped {} · {}: {error:#}", item.machine_label, item.label);
        }
    }
}
