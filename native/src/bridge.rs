//! Request/reply transport and unsolicited service events use independent pipes.
//! A long download or confirmation never blocks replies from the desktop.
use crate::state::State;
use rune::AppHandle;
use serde_json::{Value, json};
use std::collections::HashMap;
use std::io::{BufRead, BufReader, Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::{
    Arc, Mutex,
    atomic::{AtomicBool, AtomicU64, Ordering},
    mpsc::{self, Sender},
};
use std::time::{Duration, Instant};

// Five million persisted Unicode characters can occupy 60 MB after the
// service's Windows-safe ASCII JSON escaping (surrogate pairs included).
const MAX_REPLY_BYTES: u64 = 64 * 1024 * 1024;
type Requests = Arc<Mutex<HashMap<u64, String>>>;
pub struct Client {
    commands: Sender<(u64, String, Value)>,
    sequence: Arc<AtomicU64>,
}

fn service_root() -> PathBuf {
    if let Some(root) = std::env::var_os("OPENWHISPER_ROOT") {
        return root.into();
    }
    if let Ok(exe) = std::env::current_exe() {
        if let Some(folder) = exe.parent() {
            if folder.join("native/bin/openwhisper-service").exists() {
                return folder.into();
            }
            let resources = folder.join("../Resources");
            if resources.join("native/bin/openwhisper-service").exists() {
                return resources;
            }
        }
    }
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap()
        .into()
}
fn service_command(root: &Path) -> Command {
    let name = if cfg!(windows) {
        "openwhisper-service.exe"
    } else {
        "openwhisper-service"
    };
    let bundled = root.join("native/bin/openwhisper-service").join(name);
    if let Some(service) = std::env::var_os("OPENWHISPER_SERVICE") {
        return Command::new(service);
    }
    if bundled.is_file() {
        return Command::new(bundled);
    }
    let python = std::env::var_os("OPENWHISPER_PYTHON").unwrap_or_else(|| {
        [
            root.join(if cfg!(windows) {
                ".venv/Scripts/python.exe"
            } else {
                ".venv/bin/python"
            }),
            root.join(if cfg!(windows) {
                "venv/Scripts/python.exe"
            } else {
                "venv/bin/python"
            }),
        ]
        .into_iter()
        .find(|p| p.is_file())
        .map(PathBuf::into_os_string)
        .unwrap_or_else(|| "python".into())
    });
    let mut command = Command::new(python);
    command.arg(root.join("main.py"));
    command
}
fn failure(window: &AppHandle<State>, message: String) {
    window.update(move |s, _| {
        s.error = Some(message);
        s.pending = false;
        s.client = None;
    });
}
impl Client {
    pub fn start(window: AppHandle<State>) -> Self {
        let (commands, requests) = mpsc::channel::<(u64, String, Value)>();
        let sequence = Arc::new(AtomicU64::new(1));
        let ids = sequence.clone();
        std::thread::spawn(move || {
            loop {
                let root = service_root();
                let mut command = service_command(&root);
                command
                    .arg("--rune-backend")
                    .current_dir(&root)
                    .env("OPENWHISPER_DESKTOP_RUNTIME", "native")
                    .stdin(Stdio::piped())
                    .stdout(Stdio::piped())
                    .stderr(Stdio::inherit());
                #[cfg(target_os = "windows")]
                {
                    use std::os::windows::process::CommandExt;
                    command.creation_flags(0x08000000);
                }
                let mut child = match command.spawn() {
                    Ok(c) => c,
                    Err(e) => {
                        failure(
                            &window,
                            format!(
                                "Could not start the audio service: {e}. Check your OpenWhisper installation."
                            ),
                        );
                        return;
                    }
                };
                let Some(mut input) = child.stdin.take() else {
                    failure(&window, "The audio service has no input pipe".into());
                    return;
                };
                let Some(output) = child.stdout.take() else {
                    failure(&window, "The audio service has no output pipe".into());
                    return;
                };
                let inflight: Requests = Arc::new(Mutex::new(HashMap::new()));
                let alive = Arc::new(AtomicBool::new(true));
                let reader_alive = alive.clone();
                let restart = Arc::new(AtomicBool::new(false));
                let reader_restart = restart.clone();
                let replies = inflight.clone();
                let reader_window = window.clone();
                let reader = std::thread::spawn(move || {
                    let mut output = BufReader::new(output);
                    let result = (|| -> Result<(), String> {
                        loop {
                            let mut line = String::new();
                            let size = output
                                .by_ref()
                                .take(MAX_REPLY_BYTES + 1)
                                .read_line(&mut line)
                                .map_err(|e| e.to_string())?;
                            if size == 0 {
                                return Err("The audio service stopped. Check the launch log and restart OpenWhisper.".into());
                            }
                            if size as u64 > MAX_REPLY_BYTES {
                                return Err(
                                    "The audio service reply exceeded its size limit.".into()
                                );
                            }
                            let packet: Value = serde_json::from_str(&line).map_err(|_| {
                                "The audio service sent an invalid reply".to_string()
                            })?;
                            if let Some(event) = packet["event"].as_str() {
                                let event = event.to_owned();
                                let data = packet["data"].clone();
                                if event == "lifecycle" && data["state"] == "restart_required" {
                                    reader_restart.store(true, Ordering::Release);
                                }
                                reader_window
                                    .update_if(move |s, tx| s.receive_event(&event, data, tx));
                            } else if let Some(id) = packet["id"].as_u64() {
                                let method = replies.lock().unwrap().remove(&id);
                                if let Some(method) = method {
                                    reader_window.update_if(move |s, _| s.receive(&method, packet));
                                }
                            }
                        }
                    })();
                    reader_alive.store(false, Ordering::Release);
                    if let Err(message) = result {
                        if !reader_restart.load(Ordering::Acquire) {
                            failure(&reader_window, message);
                        }
                    }
                });
                let mut snapshot_at = Instant::now() - Duration::from_secs(1);
                while alive.load(Ordering::Acquire) {
                    let request = match requests.recv_timeout(Duration::from_millis(100)) {
                        Ok(request) => Some(request),
                        Err(mpsc::RecvTimeoutError::Disconnected) => break,
                        Err(mpsc::RecvTimeoutError::Timeout) => {
                            let reading =
                                inflight.lock().unwrap().values().any(|m| m == "snapshot");
                            if snapshot_at.elapsed() >= Duration::from_millis(700) && !reading {
                                snapshot_at = Instant::now();
                                Some((
                                    ids.fetch_add(1, Ordering::Relaxed),
                                    "snapshot".into(),
                                    json!({}),
                                ))
                            } else {
                                None
                            }
                        }
                    };
                    let Some((id, method, params)) = request else {
                        continue;
                    };
                    inflight.lock().unwrap().insert(id, method.clone());
                    let packet = json!({"id":id,"method":method,"params":params});
                    if writeln!(input, "{packet}")
                        .and_then(|_| input.flush())
                        .is_err()
                    {
                        break;
                    }
                }
                drop(input);
                let status = child.wait();
                let _ = reader.join();
                if restart.load(Ordering::Acquire) || status.is_ok_and(|s| s.code() == Some(73)) {
                    continue;
                }
                break;
            }
        });
        Self { commands, sequence }
    }
    pub fn send(&self, method: &str, params: Value) -> Result<(), String> {
        let id = self.sequence.fetch_add(1, Ordering::Relaxed);
        self.commands
            .send((id, method.into(), params))
            .map_err(|e| e.to_string())
    }
}
