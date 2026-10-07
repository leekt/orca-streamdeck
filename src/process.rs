use anyhow::{Context, Result, bail};
use sha2::{Digest, Sha256};
use std::{
    fs,
    io::Read,
    os::unix::fs::{DirBuilderExt, MetadataExt},
    path::{Path, PathBuf},
    process::{Command, Output, Stdio},
    thread,
    time::Duration,
};
use wait_timeout::ChildExt;

pub const COMMAND_TIMEOUT: Duration = Duration::from_secs(8);
const OUTPUT_LIMIT: u64 = 16 * 1024 * 1024;

fn ssh_control_directory() -> PathBuf {
    // Keep this short: macOS Unix socket paths (including SSH's temporary
    // suffix) must fit in 104 bytes. It must also be shared across services.
    PathBuf::from(format!("/tmp/hsd-ssh-{}", unsafe { libc::geteuid() }))
}

pub(crate) fn ssh_control_path(target: &str) -> String {
    // Separate aliases that may select different SSH identities. OpenSSH's %C
    // additionally separates resolved hosts, ports, users, and jump hosts.
    let alias = format!("{:x}", Sha256::digest(target.as_bytes()));
    format!("{}/{}-%C", ssh_control_directory().display(), &alias[..16])
}

pub(crate) fn prepare_ssh_control_directory() -> Result<()> {
    private_directory(&ssh_control_directory())
}

fn private_directory(path: &Path) -> Result<()> {
    match fs::DirBuilder::new().mode(0o700).create(path) {
        Ok(()) => {}
        Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {}
        Err(error) => return Err(error).context("Could not create SSH control directory"),
    }
    let metadata = fs::symlink_metadata(path)?;
    anyhow::ensure!(
        metadata.is_dir()
            && metadata.uid() == unsafe { libc::geteuid() }
            && metadata.mode() & 0o777 == 0o700,
        "SSH control directory {} must be an owned directory with mode 0700",
        path.display()
    );
    Ok(())
}

pub fn clean_context(command: &mut Command) {
    for (key, _) in std::env::vars_os() {
        if key.to_string_lossy().starts_with("HERDR_") {
            command.env_remove(key);
        }
    }
}

pub fn quote(value: &str) -> String {
    format!("'{}'", value.replace('\'', "'\"'\"'"))
}
pub fn shell_join(args: &[String]) -> String {
    args.iter().map(|s| quote(s)).collect::<Vec<_>>().join(" ")
}

pub fn run(mut command: Command, timeout: Duration) -> Result<Output> {
    use std::os::unix::process::CommandExt;
    command
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .process_group(0);
    let program = command.get_program().to_string_lossy().into_owned();
    let mut child = command
        .spawn()
        .with_context(|| format!("Could not start {program}"))?;
    let stdout = child.stdout.take().context("Missing command stdout")?;
    let stderr = child.stderr.take().context("Missing command stderr")?;
    let read = |stream: Box<dyn Read + Send>| {
        thread::spawn(move || {
            let mut bytes = Vec::new();
            stream
                .take(OUTPUT_LIMIT + 1)
                .read_to_end(&mut bytes)
                .map(|_| bytes)
        })
    };
    let out = read(Box::new(stdout));
    let err = read(Box::new(stderr));
    let status = match child.wait_timeout(timeout) {
        Ok(Some(status)) => status,
        result => {
            // The process group belongs to this invocation, including SSH proxies.
            unsafe {
                libc::kill(-(child.id() as i32), libc::SIGKILL);
            }
            let _ = child.wait();
            let _ = out.join();
            let _ = err.join();
            match result {
                Err(error) => return Err(error.into()),
                _ => bail!("{program} timed out after {}s", timeout.as_secs()),
            }
        }
    };
    let stdout = out
        .join()
        .map_err(|_| anyhow::anyhow!("stdout reader failed"))??;
    let stderr = err
        .join()
        .map_err(|_| anyhow::anyhow!("stderr reader failed"))??;
    anyhow::ensure!(
        stdout.len() as u64 <= OUTPUT_LIMIT && stderr.len() as u64 <= OUTPUT_LIMIT,
        "{program} output exceeded 16 MiB"
    );
    Ok(Output {
        status,
        stdout,
        stderr,
    })
}

pub fn text(command: Command) -> Result<String> {
    let output = run(command, COMMAND_TIMEOUT)?;
    if !output.status.success() {
        let error = if output.stderr.is_empty() {
            &output.stdout
        } else {
            &output.stderr
        };
        bail!("{}", String::from_utf8_lossy(error).trim());
    }
    String::from_utf8(output.stdout).context("Command returned invalid UTF-8")
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::{PermissionsExt, symlink};

    #[test]
    fn ssh_control_directory_is_private_and_reusable() {
        let root = tempfile::tempdir().unwrap();
        let path = root.path().join("control");
        private_directory(&path).unwrap();
        private_directory(&path).unwrap();
        assert_eq!(fs::metadata(&path).unwrap().mode() & 0o777, 0o700);

        fs::set_permissions(&path, fs::Permissions::from_mode(0o755)).unwrap();
        assert!(private_directory(&path).is_err());
        // Fail closed; do not repair or take over an unexpected directory.
        assert_eq!(fs::metadata(&path).unwrap().mode() & 0o777, 0o755);
    }

    #[test]
    fn ssh_control_directory_rejects_files_and_symlinks() {
        let root = tempfile::tempdir().unwrap();
        let file = root.path().join("file");
        fs::write(&file, "keep").unwrap();
        assert!(private_directory(&file).is_err());
        assert_eq!(fs::read_to_string(&file).unwrap(), "keep");

        let link = root.path().join("link");
        symlink(root.path(), &link).unwrap();
        assert!(private_directory(&link).is_err());
    }

    #[test]
    fn ssh_control_socket_fits_macos_path_limit() {
        let path = ssh_control_path(&"long-host-alias".repeat(100));
        // %C expands to 40 hex characters; SSH adds a dot and 16 characters
        // while atomically creating its socket, plus the terminating NUL.
        assert!(path.replace("%C", &"a".repeat(40)).len() + 17 < 104);
    }
}
