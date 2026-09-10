use crate::{
    approval::{self, Approval, Arm},
    backend::{Backend, Monitor},
    model::*,
    ui::{self, Renderer},
};
use anyhow::{Result, ensure};
use elgato_streamdeck::{DeviceStateUpdate, StreamDeck};
use image::{DynamicImage, RgbImage};
use std::{
    collections::{HashMap, HashSet},
    path::Path,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
        mpsc::{self, Receiver, SyncSender},
    },
    thread,
    time::{Duration, Instant},
};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Action {
    Focus,
    Inspect,
    Approve,
    Deny,
    Interrupt,
    Diffs,
}
pub struct Reply {
    pub id: String,
    pub seq: u64,
    pub action: Action,
    pub result: Result<Option<Approval>>,
}
pub struct Actions {
    sender: SyncSender<(Action, Item)>,
    pub replies: Receiver<Reply>,
}
impl Actions {
    pub fn start(backend: Backend) -> Self {
        let (sender, jobs) = mpsc::sync_channel::<(Action, Item)>(1);
        let (results, replies) = mpsc::channel();
        thread::spawn(move || {
            while let Ok((action, item)) = jobs.recv() {
                let result = (|| -> Result<Option<Approval>> {
                    let target = backend.resolve(&item)?;
                    match action {
                        Action::Focus => target.focus(&item)?,
                        Action::Interrupt => target.interrupt(&item)?,
                        Action::Diffs => target.open_changed(&item)?,
                        Action::Inspect => return approval::details(&target, &item),
                        Action::Approve | Action::Deny => {
                            ensure!(
                                approval::answer(
                                    &target,
                                    &item,
                                    action == Action::Deny,
                                    false,
                                    None,
                                    || true
                                )?,
                                "Approval changed or is no longer available"
                            );
                        }
                    }
                    Ok(None)
                })();
                if results
                    .send(Reply {
                        id: item.id,
                        seq: item.state_change_seq,
                        action,
                        result,
                    })
                    .is_err()
                {
                    break;
                }
            }
        });
        Self { sender, replies }
    }
    pub fn submit(&self, action: Action, item: &Item) -> bool {
        self.sender.try_send((action, item.clone())).is_ok()
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
enum Binding {
    Agent(String),
    Status,
    Control(String, usize),
    Empty,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Effect {
    Run(Action, String),
    CycleGlobal,
    Auto(String, bool),
}
pub struct Controls {
    pub frame: Frame,
    pub page: usize,
    pub focus: Option<String>,
    pub keys: usize,
    pressed: HashMap<usize, (Binding, u64)>,
    touched: u64,
    seen: HashMap<String, u64>,
}
impl Controls {
    pub fn new(keys: usize) -> Self {
        assert!(keys >= 6);
        Self {
            frame: Frame::default(),
            page: 0,
            focus: None,
            keys,
            pressed: HashMap::new(),
            touched: 0,
            seen: HashMap::new(),
        }
    }
    pub fn pages(&self) -> usize {
        self.frame.agents.len().div_ceil(self.keys - 1).max(1)
    }
    pub fn focused(&self) -> Option<&Item> {
        self.focus
            .as_ref()
            .and_then(|id| self.frame.agents.iter().find(|a| &a.id == id))
    }
    pub fn update(&mut self, mut frame: Frame, now: u64) {
        if self.frame.blocked() == 0 && frame.blocked() > 0 {
            self.page = 0;
        }
        let ids: HashSet<_> = frame.agents.iter().map(|a| a.id.clone()).collect();
        self.seen.retain(|id, _| ids.contains(id));
        for item in &mut frame.agents {
            item.unread = item.state == State::Done
                && self.seen.get(&item.id) != Some(&item.state_change_seq);
        }
        self.frame = frame;
        self.page = self.page.min(self.pages() - 1);
        if self.focused().is_none() || now.saturating_sub(self.touched) >= 30000 {
            self.focus = None;
        }
    }
    fn binding(&self, key: usize) -> Binding {
        if key >= self.keys {
            return Binding::Empty;
        }
        if let Some(item) = self.focused() {
            return Binding::Control(item.id.clone(), key);
        }
        if key == self.keys - 1 {
            Binding::Status
        } else {
            self.frame
                .agents
                .get(self.page * (self.keys - 1) + key)
                .map(|a| Binding::Agent(a.id.clone()))
                .unwrap_or(Binding::Empty)
        }
    }
    pub fn down(&mut self, key: usize, now: u64) {
        self.pressed.insert(key, (self.binding(key), now));
        self.touched = now;
    }
    pub fn up(&mut self, key: usize, now: u64) -> Option<Effect> {
        let (binding, at) = self.pressed.remove(&key)?;
        self.touched = now;
        let hold = now.saturating_sub(at) >= 700;
        match binding {
            Binding::Status => {
                if self.focus.is_some() {
                    return None;
                }
                if hold {
                    Some(Effect::CycleGlobal)
                } else {
                    self.page = (self.page + 1) % self.pages();
                    None
                }
            }
            Binding::Agent(id) => {
                let item = self.frame.agents.iter_mut().find(|a| a.id == id)?;
                self.seen.insert(id.clone(), item.state_change_seq);
                item.unread = false;
                if hold {
                    self.focus = Some(id);
                    None
                } else {
                    Some(Effect::Run(Action::Focus, id))
                }
            }
            Binding::Control(id, key) => {
                if self.focus.as_ref() != Some(&id) {
                    return None;
                }
                let item = self.frame.agents.iter().find(|a| a.id == id)?;
                match key {
                    0 => Some(Effect::Run(Action::Focus, id)),
                    1 if item.agent_type == "codex" && item.state == State::Blocked => Some(
                        Effect::Run(if hold { Action::Deny } else { Action::Approve }, id),
                    ),
                    2 if item.can_auto() => Some(Effect::Auto(id, hold)),
                    3 if item.state.active() => Some(Effect::Run(Action::Interrupt, id)),
                    4 if item.cwd.is_some() => Some(Effect::Run(Action::Diffs, id)),
                    k if k == 5 || k == self.keys - 1 => {
                        self.focus = None;
                        None
                    }
                    _ => None,
                }
            }
            Binding::Empty => None,
        }
    }
}

pub fn apply_effect(
    effect: Effect,
    controls: &Controls,
    actions: &Actions,
    path: &Path,
    session: &str,
) -> Result<()> {
    let now = now_ms() as f64 / 1000.0;
    match effect {
        Effect::Run(action, id) => {
            if let Some(item) = controls.frame.agents.iter().find(|a| a.id == id) {
                ensure!(
                    actions.submit(action, item),
                    "Another deck action is still pending"
                );
            }
        }
        Effect::CycleGlobal => approval::cycle(path, session, None, now)?,
        Effect::Auto(id, cycle) => {
            if cycle {
                approval::cycle(path, session, Some(id), now)?;
            } else if Arm::read(path, session, now)
                .is_some_and(|arm| arm.only.as_ref() == Some(&id))
            {
                approval::disarm(path)?;
            } else {
                Arm::write(path, session, 2, Some(id), now)?;
            }
        }
    }
    Ok(())
}
fn render(
    controls: &Controls,
    renderer: &Renderer,
    arm: Option<&Arm>,
    ask: Option<&Approval>,
    now: u64,
) -> Vec<RgbImage> {
    if let Some(item) = controls.focused() {
        let active = arm.filter(|a| a.covers(item));
        let badge = active
            .map(|a| {
                if a.only.is_none() {
                    "all".into()
                } else {
                    a.until
                        .map(|until| {
                            ui::age(((until - now as f64 / 1000.0).max(0.0) * 1000.0) as u64)
                        })
                        .unwrap_or("forever".into())
                }
            })
            .unwrap_or("off".into());
        let mut images = vec![
            renderer.action("FOCUS", &item.machine_label, [30, 90, 190], true),
            renderer.action(
                "APPRV",
                ask.map(|a| approval::ask_label(&a.tail))
                    .unwrap_or("unavailable"),
                ui::AMBER,
                ask.is_some(),
            ),
            renderer.action("AUTO", &badge, ui::AMBER, item.can_auto()),
            renderer.action(
                "INTR",
                item.state.as_str(),
                [180, 50, 50],
                item.state.active(),
            ),
            renderer.action("DIFFS", "changes", ui::GRAY, item.cwd.is_some()),
            renderer.action("BACK", &item.label, ui::GRAY, true),
        ];
        images.resize_with(controls.keys, || renderer.blank([0; 3]));
        if controls.keys > 6 {
            images[controls.keys - 1] = renderer.action("BACK", "overview", ui::GRAY, true);
        }
        images
    } else {
        let wash = controls
            .frame
            .agents
            .first()
            .map(|a| ui::color(a.state))
            .unwrap_or([0; 3]);
        let mut images: Vec<_> = (0..controls.keys - 1)
            .map(|i| {
                renderer.tile(
                    controls
                        .frame
                        .agents
                        .get(controls.page * (controls.keys - 1) + i),
                    now,
                    wash,
                )
            })
            .collect();
        images.push(renderer.status(&controls.frame, controls.page, controls.pages(), arm, now));
        images
    }
}

pub fn run(backend: Backend, stop: Arc<AtomicBool>) -> Result<()> {
    let monitor = Monitor::start(backend.clone());
    let actions = Actions::start(backend.clone());
    let path = armed_path()?;
    while !stop.load(Ordering::Relaxed) {
        if let Err(error) = connected(&backend, &monitor, &actions, &path, &stop) {
            eprintln!("Stream Deck disconnected: {error:#}; retrying in 2s");
        }
        for _ in 0..20 {
            if stop.load(Ordering::Relaxed) {
                break;
            }
            thread::sleep(Duration::from_millis(100));
        }
    }
    Ok(())
}
fn connected(
    backend: &Backend,
    monitor: &Monitor,
    actions: &Actions,
    path: &Path,
    stop: &AtomicBool,
) -> Result<()> {
    let api = elgato_streamdeck::new_hidapi()?;
    let (kind, serial) = elgato_streamdeck::list_devices(&api)
        .into_iter()
        .find(|(k, _)| k.is_visual() && k.key_count() >= 6)
        .ok_or_else(|| anyhow::anyhow!("No supported Stream Deck found"))?;
    #[allow(clippy::arc_with_non_send_sync)] // get_reader() requires Arc
    let deck = Arc::new(StreamDeck::connect(&api, kind, &serial)?);
    deck.reset()?;
    deck.set_brightness(100)?;
    let reader = deck.get_reader();
    let (w, h) = kind.key_image_format().size;
    let renderer = Renderer::new(w as u32, h as u32)?;
    let mut controls = Controls::new(kind.key_count() as usize);
    let mut previous: Vec<RgbImage> = Vec::new();
    let mut brightness = 100;
    let mut draw_at = Instant::now();
    let mut ask: Option<(String, u64, Approval)> = None;
    let mut inspect_pending = false;
    let mut inspect_at = Instant::now();
    eprintln!(
        "Connected: {kind:?} ({} keys), Rust {}, Herdr session {}",
        controls.keys,
        env!("CARGO_PKG_VERSION"),
        backend.config.session
    );
    while !stop.load(Ordering::Relaxed) {
        while let Ok(frame) = monitor.frames.try_recv() {
            controls.update(frame, now_ms());
        }
        while let Ok(reply) = actions.replies.try_recv() {
            if reply.action == Action::Inspect {
                inspect_pending = false;
                inspect_at = Instant::now() + Duration::from_secs(2);
                ask = reply
                    .result
                    .ok()
                    .flatten()
                    .map(|a| (reply.id, reply.seq, a));
            } else if let Err(error) = reply.result {
                eprintln!("Deck action failed: {error:#}");
            }
        }
        if controls.focused().is_none() || now_ms().saturating_sub(controls.touched) >= 30000 {
            controls.focus = None;
            ask = None;
        }
        if let Some(item) = controls.focused() {
            if ask.as_ref().is_some_and(|(id, seq, _)| {
                id != &item.id || *seq != item.state_change_seq || item.state != State::Blocked
            }) {
                ask = None;
            }
            if !inspect_pending
                && Instant::now() >= inspect_at
                && item.agent_type == "codex"
                && item.state == State::Blocked
            {
                inspect_pending = actions.submit(Action::Inspect, item);
            }
        }
        for update in reader.read(Some(Duration::from_millis(25)))? {
            match update {
                DeviceStateUpdate::ButtonDown(key) => controls.down(key as usize, now_ms()),
                DeviceStateUpdate::ButtonUp(key) => {
                    if let Some(effect) = controls.up(key as usize, now_ms()) {
                        if let Err(error) =
                            apply_effect(effect, &controls, actions, path, &backend.config.session)
                        {
                            eprintln!("Deck action failed: {error:#}");
                        }
                    }
                    draw_at = Instant::now();
                }
                _ => {}
            }
        }
        if Instant::now() >= draw_at {
            let now = now_ms();
            let arm = Arm::read(path, &backend.config.session, now as f64 / 1000.0);
            let current_ask = ask
                .as_ref()
                .filter(|(id, seq, _)| {
                    controls.focused().is_some_and(|a| {
                        &a.id == id && a.state_change_seq == *seq && a.state == State::Blocked
                    })
                })
                .map(|(_, _, a)| a);
            let images = render(&controls, &renderer, arm.as_ref(), current_ask, now);
            for (key, image) in images.iter().enumerate() {
                if previous.get(key) != Some(image) {
                    deck.set_button_image(key as u8, DynamicImage::ImageRgb8(image.clone()))?;
                }
            }
            deck.flush()?;
            previous = images;
            let level = if controls.frame.blocked() > 0 && (now / 500) % 2 == 0 {
                60
            } else {
                100
            };
            if level != brightness {
                deck.set_brightness(level)?;
                brightness = level;
            }
            draw_at = Instant::now() + Duration::from_millis(200);
        }
    }
    deck.reset()?;
    Ok(())
}
