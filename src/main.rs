use anyhow::{Result, ensure};
use clap::{Parser, Subcommand};
use herdr_streamdeck::{
    approval::{self, Arm, Sent},
    backend::{Backend, Fleet},
    controller,
    model::*,
    ui::Renderer,
};
use serde::Serialize;
use std::{
    collections::{BTreeMap, HashSet},
    fs,
    path::{Path, PathBuf},
    process::Command,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    thread,
    time::Duration,
};

/// Stream Deck controls for local and remote Herdr agents.
#[derive(Parser)]
#[command(version, about)]
struct Cli {
    #[command(subcommand)]
    command: Option<Cmd>,
}

#[derive(Subcommand)]
enum Cmd {
    /// Drive the Stream Deck (default)
    Run,
    /// Print agent tiles and machine status as JSON; exits 1 if any machine is offline
    Status {
        #[arg(long)]
        local_only: bool,
    },
    /// Approve recognized Codex requests while the arm file allows it
    Autoapprove {
        /// Ignore the arm file; local agents only unless --only names remote identities
        #[arg(long)]
        always: bool,
        /// Limit to these agent identities (from `status`)
        #[arg(long)]
        only: Vec<String>,
    },
    /// Show agents and controls in the macOS menu bar
    Menubar,
    /// Write LaunchAgent plists into this checkout; --install also starts them
    Install {
        #[arg(long)]
        install: bool,
        #[arg(long)]
        menubar: bool,
    },
    /// Render sample key images to a PNG
    Preview { path: PathBuf },
}

fn stop_flag() -> Result<Arc<AtomicBool>> {
    let stop = Arc::new(AtomicBool::new(false));
    for signal in [signal_hook::consts::SIGINT, signal_hook::consts::SIGTERM] {
        signal_hook::flag::register(signal, stop.clone())?;
    }
    Ok(stop)
}

fn main() -> Result<()> {
    let cli = Cli::parse();
    let mut config = Config::load(&config_path()?)?;
    match cli.command.unwrap_or(Cmd::Run) {
        Cmd::Run => controller::run(Backend::local(config), stop_flag()?),
        Cmd::Status { local_only } => {
            if local_only {
                config.include_remote_machines = false;
            }
            let frame = Fleet::new(Backend::local(config)).inspect();
            println!("{}", serde_json::to_string_pretty(&frame)?);
            ensure!(
                frame
                    .machines
                    .iter()
                    .all(|m| m.state != Connection::Offline),
                "Some machines are offline"
            );
            Ok(())
        }
        Cmd::Autoapprove { always, only } => autoapprove(
            Backend::local(config),
            always,
            only.into_iter().collect(),
            stop_flag()?,
        ),
        #[cfg(target_os = "macos")]
        Cmd::Menubar => herdr_streamdeck::menubar::run(Backend::local(config)),
        #[cfg(not(target_os = "macos"))]
        Cmd::Menubar => anyhow::bail!("The menu bar app requires macOS"),
        Cmd::Install { install, menubar } => install_services(install, menubar),
        Cmd::Preview { path } => Renderer::preview(&path),
    }
}

fn autoapprove(
    backend: Backend,
    always: bool,
    only: HashSet<String>,
    stop: Arc<AtomicBool>,
) -> Result<()> {
    let path = armed_path()?;
    let mut fleet = Fleet::new(backend.clone());
    let mut sent = Sent::new();
    let mut was_armed = false;
    while !stop.load(Ordering::Relaxed) {
        let arm = Arm::read(&path, &backend.config.session, now_ms() as f64 / 1000.0);
        if !(always || arm.is_some()) {
            if was_armed {
                eprintln!("stood down");
                was_armed = false;
            }
            thread::sleep(Duration::from_secs(5));
            continue;
        }
        if !was_armed {
            eprintln!("armed for Herdr session {}", backend.config.session);
            was_armed = true;
        }
        let scope = arm
            .and_then(|arm| arm.only)
            .map(|id| HashSet::from([id]))
            .unwrap_or_else(|| only.clone());
        let frame = fleet.tick();
        approval::poll(&backend, &frame, &path, &mut sent, always, &scope);
        thread::sleep(Duration::from_millis(500));
    }
    Ok(())
}

#[derive(Serialize)]
#[serde(rename_all = "PascalCase")]
struct Service {
    label: String,
    program_arguments: Vec<String>,
    working_directory: String,
    environment_variables: BTreeMap<String, String>,
    run_at_load: bool,
    keep_alive: bool,
    throttle_interval: u32,
    standard_out_path: String,
    standard_error_path: String,
}
fn service(name: &str, root: &Path, exe: &Path, home: &Path) -> Service {
    let log = home
        .join("Library/Logs")
        .join(format!("herdr-{name}.log"))
        .display()
        .to_string();
    let program_arguments = if name == "streamdeck" {
        vec![root.join("run.sh").display().to_string()]
    } else {
        vec![exe.display().to_string(), name.into()]
    };
    Service {
        label: format!("com.taek.herdr-{name}"),
        program_arguments,
        working_directory: root.display().to_string(),
        environment_variables: BTreeMap::from([(
            "PATH".into(),
            format!(
                "{}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
                home.display()
            ),
        )]),
        run_at_load: true,
        keep_alive: true,
        throttle_interval: 30,
        standard_out_path: log.clone(),
        standard_error_path: log,
    }
}
fn launchctl(args: &[&str]) -> Result<bool> {
    let status = Command::new("launchctl")
        .args(args)
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .status()?;
    Ok(status.success())
}
fn install_services(install: bool, menubar: bool) -> Result<()> {
    let root = std::env::current_dir()?;
    let exe = std::env::current_exe()?.canonicalize()?;
    let home = home()?;
    for name in ["streamdeck", "autoapprove", "menubar"] {
        let path = root.join(format!("com.taek.herdr-{name}.plist"));
        plist::to_file_xml(&path, &service(name, &root, &exe, &home))?;
        println!("Prepared {}", path.display());
    }
    if !install {
        return Ok(());
    }
    let agents = home.join("Library/LaunchAgents");
    fs::create_dir_all(&agents)?;
    fs::create_dir_all(home.join("Library/Logs"))?;
    let config_dir = home.join(".config/herdr-streamdeck");
    fs::create_dir_all(&config_dir)?;
    if !config_dir.join("config.json").exists() {
        fs::copy(
            root.join("config.example.json"),
            config_dir.join("config.json"),
        )?;
    }
    // Starting or reinstalling the services never grants approval authority.
    approval::disarm(&armed_path()?)?;
    let domain = format!("gui/{}", unsafe { libc::getuid() });
    let names: Vec<_> = ["streamdeck", "autoapprove"]
        .into_iter()
        .chain(menubar.then_some("menubar"))
        .collect();
    for name in names {
        let label = format!("com.taek.herdr-{name}");
        let target = agents.join(format!("{label}.plist"));
        if launchctl(&["print", &format!("{domain}/{label}")])? {
            ensure!(
                launchctl(&["bootout", &format!("{domain}/{label}")])?,
                "launchctl bootout failed for {label}"
            );
        }
        fs::copy(root.join(format!("{label}.plist")), &target)?;
        ensure!(
            launchctl(&["bootstrap", &domain, &target.display().to_string()])?,
            "launchctl bootstrap failed for {label}"
        );
        println!("Started {label}");
    }
    Ok(())
}
