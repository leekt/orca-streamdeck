use anyhow::{Context, Result, bail};
use std::{
    io::Read,
    process::{Command, Output, Stdio},
    thread,
    time::Duration,
};
use wait_timeout::ChildExt;

pub const COMMAND_TIMEOUT: Duration = Duration::from_secs(8);
const OUTPUT_LIMIT: u64 = 16 * 1024 * 1024;

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
