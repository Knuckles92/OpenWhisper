use crate::state::State;
use rune::AppHandle;
use serde_json::{Value, json};
use std::io::{BufRead, BufReader, Write};
use std::process::{Command, Stdio};
use std::sync::mpsc::{self, Sender};
use std::time::Duration;

pub struct Client {
    commands: Sender<(String, Value)>,
}

impl Client {
    pub fn start(window: AppHandle<State>) -> Self {
        let (commands, requests) = mpsc::channel::<(String, Value)>();
        std::thread::spawn(move || {
            let result = (|| -> Result<(), String> {
                let root = std::env::var_os("OPENWHISPER_ROOT")
                    .map(std::path::PathBuf::from)
                    .unwrap_or_else(|| {
                        std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
                            .parent()
                            .unwrap()
                            .into()
                    });
                let python = std::env::var_os("OPENWHISPER_PYTHON").unwrap_or_else(|| {
                    let venv = root.join(if cfg!(windows) {
                        ".venv/Scripts/python.exe"
                    } else {
                        ".venv/bin/python"
                    });
                    if venv.exists() {
                        venv.into_os_string()
                    } else {
                        "python".into()
                    }
                });
                let mut child = Command::new(python).arg(root.join("main.py")).arg("--rune-backend")
                    .current_dir(root).stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::inherit())
                    .spawn().map_err(|e| format!("Could not start the audio service: {e}. Set OPENWHISPER_PYTHON to your Python environment."))?;
                let mut input = child
                    .stdin
                    .take()
                    .ok_or("Audio service has no input pipe")?;
                let mut output = BufReader::new(
                    child
                        .stdout
                        .take()
                        .ok_or("Audio service has no output pipe")?,
                );
                let mut id = 0_u64;
                let session = (|| -> Result<(), String> {
                    loop {
                        let (method, params) = match requests
                            .recv_timeout(Duration::from_millis(700))
                        {
                            Ok(request) => request,
                            Err(mpsc::RecvTimeoutError::Timeout) => ("snapshot".into(), json!({})),
                            Err(mpsc::RecvTimeoutError::Disconnected) => break,
                        };
                        id += 1;
                        let request = json!({"id":id,"method":method,"params":params});
                        writeln!(input, "{request}").and_then(|_| input.flush()).map_err(|_| "The audio service stopped. Check the launch terminal and your Python environment, then restart OpenWhisper.".to_string())?;
                        let mut line = String::new();
                        if output.read_line(&mut line).map_err(|e| e.to_string())? == 0 {
                            return Err("The audio service stopped. Close any other OpenWhisper instance and launch again.".into());
                        }
                        let response: Value = serde_json::from_str(&line)
                            .map_err(|_| "The audio service sent an invalid reply".to_string())?;
                        let action = method.clone();
                        window.update_if(move |s, _| s.receive(&action, response));
                    }
                    Ok(())
                })();
                // Closing the pipe lets Python run its usual shutdown and drain
                // persistence. It also receives EOF if the native process exits.
                drop(input);
                let _ = child.wait();
                session
            })();
            if let Err(message) = result {
                window.update(move |s, _| {
                    s.error = Some(message);
                    s.pending = false;
                    s.client = None;
                });
            }
        });
        Self { commands }
    }
    pub fn send(&self, method: &str, params: Value) -> Result<(), String> {
        self.commands
            .send((method.into(), params))
            .map_err(|e| e.to_string())
    }
}
