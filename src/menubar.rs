//! macOS menu bar view of the same fleet the deck shows, built on AppKit directly.
use crate::{
    approval::{self, Arm},
    backend::{Backend, Monitor},
    controller::{Action, Actions},
    model::*,
    ui::age,
};
use anyhow::{Context, Result};
use objc2::{
    DefinedClass, MainThreadMarker, MainThreadOnly, define_class, msg_send, rc::Retained,
    runtime::AnyObject, sel,
};
use objc2_app_kit::{
    NSApplication, NSApplicationActivationPolicy, NSMenu, NSMenuItem, NSStatusBar,
    NSVariableStatusItemLength,
};
use objc2_foundation::{NSDate, NSObject, NSRunLoop, NSString};
use std::cell::RefCell;

enum Pick {
    Run(Action, Box<Item>),
    Auto(usize),
    Off,
}
#[derive(Default)]
struct Ivars {
    picks: RefCell<Vec<Pick>>,
    chosen: RefCell<Vec<usize>>,
}

define_class!(
    #[unsafe(super(NSObject))]
    #[thread_kind = MainThreadOnly]
    #[ivars = Ivars]
    struct Handler;
    impl Handler {
        #[unsafe(method(pick:))]
        fn pick(&self, sender: &NSMenuItem) { self.ivars().chosen.borrow_mut().push(sender.tag() as usize); }
    }
);

fn glyph(state: State) -> &'static str {
    match state {
        State::Blocked => "🟠",
        State::Working => "🔵",
        State::Done => "🟢",
        State::Idle => "⚪",
        State::Unknown => "⚫",
    }
}
const DURATIONS: [&str; 3] = ["30 minutes", "1 hour", "Forever"];

struct Bar {
    mtm: MainThreadMarker,
    handler: Retained<Handler>,
    menu: Retained<NSMenu>,
    status: Retained<objc2_app_kit::NSStatusItem>,
    signature: String,
}
impl Bar {
    fn item(&self, title: &str, pick: Option<Pick>) -> Retained<NSMenuItem> {
        let action = pick.as_ref().map(|_| sel!(pick:));
        let item = unsafe {
            NSMenuItem::initWithTitle_action_keyEquivalent(
                NSMenuItem::alloc(self.mtm),
                &NSString::from_str(title),
                action,
                &NSString::from_str(""),
            )
        };
        if let Some(pick) = pick {
            let mut picks = self.handler.ivars().picks.borrow_mut();
            item.setTag(picks.len() as isize);
            picks.push(pick);
            let target: &AnyObject = &self.handler;
            unsafe { item.setTarget(Some(target)) };
        }
        item
    }
    fn rebuild(&mut self, frame: &Frame, arm: Option<&Arm>, now: u64) {
        let seconds = now as f64 / 1000.0;
        let session_wide = arm.filter(|arm| arm.only.is_none());
        let signature = format!(
            "{:?}|{:?}|{:?}|{}",
            frame
                .agents
                .iter()
                .map(|a| (&a.id, &a.machine_label, &a.label, &a.sub, a.state))
                .collect::<Vec<_>>(),
            frame.machines,
            session_wide.map(|a| a.index),
            now / 60000
        );
        if signature == self.signature {
            return;
        }
        self.signature = signature;
        let blocked = frame.blocked();
        let offline = frame
            .machines
            .iter()
            .any(|m| m.state == Connection::Offline);
        let title = format!(
            "{}{}",
            if blocked > 0 {
                format!("H · {blocked}")
            } else {
                "H".into()
            },
            if offline { " ⚠" } else { "" }
        );
        if let Some(button) = self.status.button(self.mtm) {
            button.setTitle(&NSString::from_str(&title));
        }
        self.handler.ivars().picks.borrow_mut().clear();
        self.menu.removeAllItems();
        for machine in &frame.machines {
            let symbol = match machine.state {
                Connection::Online => "🟢",
                Connection::Connecting => "⏳",
                Connection::Offline => "🔴",
            };
            let entry = self.item(
                &format!("{symbol} {} · {}", machine.label, machine.state.as_str()),
                None,
            );
            if !machine.error.is_empty() {
                let sub = NSMenu::new(self.mtm);
                sub.addItem(&self.item(&machine.error.chars().take(240).collect::<String>(), None));
                entry.setSubmenu(Some(&sub));
            }
            self.menu.addItem(&entry);
        }
        self.menu.addItem(&NSMenuItem::separatorItem(self.mtm));
        for item in &frame.agents {
            let mut label = format!(
                "{} {} · {} ({})",
                glyph(item.state),
                item.label,
                item.sub.chars().take(28).collect::<String>(),
                age(now.saturating_sub(item.state_since))
            );
            if item.show_machine {
                label = format!("{} · {label}", item.machine_label);
            }
            let parent = self.item(&label, None);
            let sub = NSMenu::new(self.mtm);
            sub.addItem(&self.item(
                "Focus",
                Some(Pick::Run(Action::Focus, Box::new(item.clone()))),
            ));
            if item.state.active() {
                sub.addItem(&self.item(
                    "Interrupt",
                    Some(Pick::Run(Action::Interrupt, Box::new(item.clone()))),
                ));
            }
            if item.cwd.is_some() {
                sub.addItem(&self.item(
                    "Changes",
                    Some(Pick::Run(Action::Diffs, Box::new(item.clone()))),
                ));
            }
            parent.setSubmenu(Some(&sub));
            self.menu.addItem(&parent);
        }
        if frame.agents.is_empty() {
            self.menu.addItem(&self.item("No agents", None));
        }
        self.menu.addItem(&NSMenuItem::separatorItem(self.mtm));
        let badge = session_wide
            .map(|arm| format!(" · {}", arm.badge(seconds)))
            .unwrap_or_default();
        let auto = self.item(&format!("Local Codex auto-approve{badge}"), None);
        let sub = NSMenu::new(self.mtm);
        for (index, name) in DURATIONS.iter().enumerate() {
            let check = if session_wide.is_some_and(|arm| arm.index == index) {
                "✓ "
            } else {
                ""
            };
            sub.addItem(&self.item(&format!("{check}{name}"), Some(Pick::Auto(index))));
        }
        sub.addItem(&self.item("Off", Some(Pick::Off)));
        auto.setSubmenu(Some(&sub));
        self.menu.addItem(&auto);
        self.menu.addItem(&NSMenuItem::separatorItem(self.mtm));
        let quit = unsafe {
            NSMenuItem::initWithTitle_action_keyEquivalent(
                NSMenuItem::alloc(self.mtm),
                &NSString::from_str("Quit"),
                Some(sel!(terminate:)),
                &NSString::from_str("q"),
            )
        };
        self.menu.addItem(&quit);
    }
}

pub fn run(backend: Backend) -> Result<()> {
    let mtm = MainThreadMarker::new().context("The menu bar must run on the main thread")?;
    let app = NSApplication::sharedApplication(mtm);
    app.setActivationPolicy(NSApplicationActivationPolicy::Accessory);
    let status = NSStatusBar::systemStatusBar().statusItemWithLength(NSVariableStatusItemLength);
    let menu = NSMenu::new(mtm);
    status.setMenu(Some(&menu));
    let handler: Retained<Handler> =
        unsafe { msg_send![super(Handler::alloc(mtm).set_ivars(Ivars::default())), init] };
    let mut bar = Bar {
        mtm,
        handler,
        menu,
        status,
        signature: String::new(),
    };
    let monitor = Monitor::start(backend.clone());
    let actions = Actions::start(backend.clone());
    let path = armed_path()?;
    let mut frame = Frame::default();
    app.finishLaunching();
    loop {
        NSRunLoop::mainRunLoop().runUntilDate(&NSDate::dateWithTimeIntervalSinceNow(0.25));
        while let Ok(next) = monitor.frames.try_recv() {
            frame = next;
        }
        while let Ok(reply) = actions.replies.try_recv() {
            if let Err(error) = reply.result {
                eprintln!("Menu action failed: {error:#}");
            }
        }
        let now = now_ms();
        let arm = Arm::read(&path, &backend.config.session, now as f64 / 1000.0);
        let chosen: Vec<usize> = bar.handler.ivars().chosen.borrow_mut().drain(..).collect();
        for index in chosen {
            let result = match bar.handler.ivars().picks.borrow().get(index) {
                Some(Pick::Run(action, item)) => {
                    if !actions.submit(*action, item) {
                        eprintln!("Another action is still pending");
                    }
                    Ok(())
                }
                Some(Pick::Auto(index))
                    if arm
                        .as_ref()
                        .is_some_and(|arm| arm.only.is_none() && arm.index == *index) =>
                {
                    approval::disarm(&path)
                }
                Some(Pick::Auto(index)) => Arm::write(
                    &path,
                    &backend.config.session,
                    *index,
                    None,
                    now as f64 / 1000.0,
                )
                .map(|_| ()),
                Some(Pick::Off) => approval::disarm(&path),
                None => Ok(()),
            };
            if let Err(error) = result {
                eprintln!("Menu action failed: {error:#}");
            }
            bar.signature.clear();
        }
        bar.rebuild(&frame, arm.as_ref(), now);
    }
}
