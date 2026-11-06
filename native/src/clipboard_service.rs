//! Multi-MIME Wayland clipboard service for the Python speech process.
//! A prepared data-control source never creates or focuses a window. On EOF,
//! retain the last rendered copy until its native source loses ownership.

#[cfg(target_os = "linux")]
fn main() {
    use base64::{Engine, engine::general_purpose::STANDARD};
    use serde_json::{Value, json};
    use std::collections::BTreeMap;
    use std::io::{self, BufRead, Read, Write};
    use std::os::fd::AsFd;
    use std::sync::mpsc;
    use wl_clipboard_rs::{copy, paste};
    const LIMIT: usize = 32 * 1024 * 1024;
    fn read_data(mut reader: impl Read + AsFd) -> Result<Vec<u8>, String> {
        use rustix::event::{PollFd, PollFlags, Timespec, poll};
        let mut data = Vec::new();
        let mut buffer = [0_u8; 65536];
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(3);
        loop {
            if std::time::Instant::now() >= deadline {
                return Err("The clipboard owner did not finish its transfer".into());
            }
            let ready = {
                let mut wait = [PollFd::new(&reader, PollFlags::IN)];
                poll(
                    &mut wait,
                    Some(&Timespec {
                        tv_sec: 1,
                        tv_nsec: 0,
                    }),
                )
                .map_err(|e| e.to_string())?
            };
            if ready == 0 {
                continue;
            }
            let count = reader.read(&mut buffer).map_err(|e| e.to_string())?;
            if count == 0 {
                return Ok(data);
            }
            data.extend_from_slice(&buffer[..count]);
            if data.len() > LIMIT {
                return Err("Clipboard item exceeds capture limit".into());
            }
        }
    }
    let mut owners: Vec<std::thread::JoinHandle<()>> = Vec::new();
    for line in io::stdin().lock().lines() {
        let Ok(line) = line else {
            break;
        };
        let response = (|| -> Result<Value, String> {
            if line.len() > LIMIT * 2 {
                return Err("Clipboard request exceeds capture limit".into());
            }
            let request: Value = serde_json::from_str(&line).map_err(|e| e.to_string())?;
            match request["method"].as_str().unwrap_or("") {
                "read" => {
                    let types = match paste::get_mime_types(
                        paste::ClipboardType::Regular,
                        paste::Seat::Unspecified,
                    ) {
                        Ok(types) => types,
                        Err(
                            paste::Error::ClipboardEmpty
                            | paste::Error::NoMimeType
                            | paste::Error::NoSeats,
                        ) => return Ok(json!({"formats":{},"complete":true})),
                        Err(error) => return Err(error.to_string()),
                    };
                    let mut formats = BTreeMap::new();
                    let mut total = 0;
                    let mut complete = true;
                    for mime in &types {
                        match paste::get_contents(
                            paste::ClipboardType::Regular,
                            paste::Seat::Unspecified,
                            paste::MimeType::Specific(mime),
                        ) {
                            Ok((reader, _)) => {
                                let data = match read_data(reader) {
                                    Ok(data) => data,
                                    Err(_) => {
                                        complete = false;
                                        continue;
                                    }
                                };
                                total += data.len();
                                if total > LIMIT {
                                    complete = false;
                                    break;
                                }
                                formats.insert(mime.clone(), STANDARD.encode(data));
                            }
                            Err(_) => complete = false,
                        }
                    }
                    if paste::get_mime_types(
                        paste::ClipboardType::Regular,
                        paste::Seat::Unspecified,
                    )
                    .ok()
                    .as_ref()
                        != Some(&types)
                    {
                        complete = false;
                    }
                    Ok(json!({"formats":formats,"complete":complete}))
                }
                "write" => {
                    let data = request["formats"]
                        .as_object()
                        .ok_or("Expected clipboard formats")?;
                    if data.is_empty() {
                        copy::clear(copy::ClipboardType::Regular, copy::Seat::All)
                            .map_err(|e| e.to_string())?;
                        return Ok(json!({"ok":true}));
                    }
                    let mut sources = Vec::new();
                    let mut total = 0;
                    for (mime, data) in data {
                        let bytes = STANDARD
                            .decode(data.as_str().ok_or("Expected encoded clipboard bytes")?)
                            .map_err(|e| e.to_string())?;
                        total += bytes.len();
                        if total > LIMIT {
                            return Err("Clipboard contents exceed capture limit".into());
                        }
                        sources.push(copy::MimeSource {
                            source: copy::Source::Bytes(bytes.into_boxed_slice()),
                            mime_type: copy::MimeType::Specific(mime.clone()),
                        });
                    }
                    let (ready, result) = mpsc::sync_channel(1);
                    owners.retain(|owner| !owner.is_finished());
                    owners.push(std::thread::spawn(move || {
                        let mut options = copy::Options::new();
                        options.foreground(true);
                        options.omit_additional_text_mime_types(true);
                        match options.prepare_copy_multi(sources) {
                            Ok(source) => {
                                let _ = ready.send(Ok(()));
                                let _ = source.serve();
                            }
                            Err(error) => {
                                let _ = ready.send(Err(error.to_string()));
                            }
                        }
                    }));
                    result.recv().map_err(|e| e.to_string())??;
                    Ok(json!({"ok":true}))
                }
                _ => Err("Unknown clipboard method".into()),
            }
        })();
        let packet = match response {
            Ok(value) => value,
            Err(error) => json!({"error":error}),
        };
        if writeln!(io::stdout().lock(), "{packet}")
            .and_then(|_| io::stdout().flush())
            .is_err()
        {
            break;
        }
    }
    for owner in owners {
        let _ = owner.join();
    }
}

#[cfg(not(target_os = "linux"))]
fn main() {
    eprintln!("The Wayland clipboard helper is only used on Linux.");
}
