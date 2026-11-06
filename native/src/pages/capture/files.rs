use super::super::library::{field, value};
use super::super::{recent, transcript};
use crate::state::State;
use crate::ui::{self, Palette, btn, card, label, link, page_header};
use rune::{El, FileDialogOptions, Tx, col, icon, row};
use rune_kit::glyphs;
use serde_json::json;

fn choose_file(_: &mut State, tx: &mut Tx) {
    tx.open_file(
        FileDialogOptions {
            title: "Choose an audio or video file".into(),
            ..Default::default()
        }
        .filter(
            "Audio & video",
            &[
                "wav", "mp3", "m4a", "flac", "ogg", "opus", "mp4", "webm", "aac", "aiff", "mov",
            ],
        ),
        |s: &mut State, result, _| match result {
            Ok(Some(path)) => {
                let path = path.to_string_lossy().into_owned();
                if !s.file_queue.contains(&path) {
                    s.file_queue.push(path.clone());
                }
                s.choose_file(path);
            }
            Ok(None) => {}
            Err(error) => s.error = Some(error.to_string()),
        },
    );
}

pub fn files(s: &State, p: &Palette) -> El {
    let mut content = col()
        .gap(24.)
        .child(page_header(
            "Let your recordings do the talking.",
            "Queue audio and video files. Keep every finished transcript in your library.",
            p,
        ))
        .child(
            card(p)
                .key("file-drop")
                .min_h(224.)
                .center()
                .gap(15.)
                .fill(p.soft)
                .border_dash(5., 5.)
                .on_drop_files(|s: &mut State, files, _| {
                    for path in files {
                        let path = path.to_string_lossy().into_owned();
                        if !s.file_queue.contains(&path) {
                            s.file_queue.push(path);
                        }
                    }
                    s.file = s.file_queue.first().cloned();
                    s.notice = Some(format!("{} files ready to transcribe", s.file_queue.len()));
                })
                .accepts_extensions(&[
                    "wav", "mp3", "m4a", "flac", "ogg", "opus", "mp4", "webm", "aac", "aiff", "mov",
                ])
                .child(
                    row()
                        .size(50., 50.)
                        .center()
                        .radius(15.)
                        .fill(p.accent_soft)
                        .child(icon(&ui::UPLOAD, 24.).color(p.accent)),
                )
                .child(label("Drop a recording. Find the words.", 23., 500., p.ink))
                .child(label(
                    "Drop several files to build a queue, or choose one at a time.",
                    12.,
                    400.,
                    p.muted,
                ))
                .child(btn(
                    "choose-file",
                    "Browse files",
                    p,
                    true,
                    true,
                    choose_file,
                ))
                .child(label(
                    "WAV, MP3, M4A, FLAC, MP4 and more",
                    10.5,
                    400.,
                    p.muted,
                )),
        );
    let mut chosen = s.file_queue.clone();
    if let Some(path) = &s.file {
        if !chosen.contains(path) {
            chosen.push(path.clone());
        }
    }
    if !chosen.is_empty() {
        let mut queue = card(p).key("file-queue").gap(10.)
            .child(row().gap(12.).center_y().flex_wrap()
                .child(label(format!("{} ready to transcribe", chosen.len()), 15., 550., p.ink).grow(1.))
                .child(btn("transcribe-file", if s.snapshot.busy { "Transcribing…" } else { "Transcribe queue" }, p, true, s.available() && !s.working(), |s, _| {
                    let mut paths = s.file_queue.clone();
                    if let Some(path) = &s.file { if !paths.contains(path) { paths.push(path.clone()); } }
                    let relation = s.edits.get("file-relation").map(|edit|edit.text.clone()).unwrap_or_else(||"separate".into());
                    let instructions = s.edit("file-instructions").text.clone();
                    let combine = s.edit("file-combine").text == "yes";
                    s.command("file.enqueue", json!({"paths":paths,"relation":relation,"instructions":instructions,"combine":combine}));
                }))
                .child(link("clear-file-selection", "Clear", p, |s, _| { s.file_queue.clear(); s.file = None; })));
        let relation = s
            .edits
            .get("file-relation")
            .map(|edit| edit.text.as_str())
            .unwrap_or("separate");
        let mut grouping = row().gap(7.).flex_wrap();
        for (id, title) in [
            ("separate", "Separate transcripts"),
            ("sequential", "One continuous recording"),
            ("custom", "Custom instructions"),
        ] {
            grouping = grouping.child(btn(
                format!("file-relation-{id}"),
                title,
                p,
                relation == id,
                !s.working(),
                move |s, _| s.edit("file-relation").text = id.into(),
            ));
        }
        queue = queue.child(grouping);
        if relation == "custom" {
            let combine = s
                .edits
                .get("file-combine")
                .is_some_and(|edit| edit.text == "yes");
            queue = queue
                .child(field(
                    s,
                    p,
                    "file-instructions",
                    "How should these recordings be handled together?",
                    true,
                ))
                .child(btn(
                    "file-custom-combine",
                    if combine {
                        "Combine into one transcript: on"
                    } else {
                        "Combine into one transcript: off"
                    },
                    p,
                    combine,
                    !s.working(),
                    move |s, _| {
                        s.edit("file-combine").text = if combine { "no" } else { "yes" }.into()
                    },
                ));
        }
        for (index, path) in chosen.iter().enumerate() {
            let name = std::path::Path::new(path)
                .file_name()
                .unwrap_or_default()
                .to_string_lossy()
                .to_string();
            let remove = path.clone();
            queue = queue.child(
                row()
                    .gap(12.)
                    .center_y()
                    .padding(10.)
                    .radius(8.)
                    .fill(p.soft)
                    .child(icon(&glyphs::FILE, 19.).color(p.accent))
                    .child(
                        col()
                            .grow(1.)
                            .min_w(0.)
                            .gap(4.)
                            .child(label(name, 12., 550., p.ink).max_lines(1))
                            .child(label(path, 10., 400., p.muted).max_lines(1).help(path)),
                    )
                    .child(btn(
                        format!("move-file-up-{index}"),
                        "Up",
                        p,
                        false,
                        index > 0 && !s.working(),
                        move |s, _| {
                            if index > 0 && index < s.file_queue.len() {
                                s.file_queue.swap(index, index - 1);
                                s.file = s.file_queue.first().cloned();
                            }
                        },
                    ))
                    .child(link(
                        format!("remove-file-{index}"),
                        "Remove",
                        p,
                        move |s, _| {
                            s.file_queue.retain(|path| path != &remove);
                            if s.file.as_ref() == Some(&remove) {
                                s.file = s.file_queue.first().cloned();
                            }
                        },
                    )),
            );
        }
        content = content.child(queue);
    }
    let queue = if s.data["snapshot"]["file_queue"].is_object() {
        &s.data["snapshot"]["file_queue"]
    } else {
        &s.data["file.queue"]
    };
    if let Some(items) = queue["items"].as_array().filter(|items| !items.is_empty()) {
        let active = queue["active"].as_bool().unwrap_or(false);
        let mut panel = card(p).key("file-jobs").gap(12.).child(
            row()
                .gap(8.)
                .center_y()
                .flex_wrap()
                .child(label("Transcription queue", 15., 550., p.ink).grow(1.))
                .child(link("refresh-file-jobs", "Refresh", p, |s, _| {
                    s.command("file.queue", json!({}))
                })),
        );
        for item in items {
            let id = value(item, "id", "").to_string();
            let status = value(item, "status", "queued");
            panel = panel.child(
                row()
                    .gap(12.)
                    .center_y()
                    .padding(10.)
                    .radius(8.)
                    .fill(p.soft)
                    .child(icon(&glyphs::FILE_TEXT, 18.).color(p.accent))
                    .child(
                        col()
                            .grow(1.)
                            .min_w(0.)
                            .gap(4.)
                            .child(
                                label(
                                    value(item, "name", value(item, "path", "Audio file")),
                                    12.,
                                    550.,
                                    p.ink,
                                )
                                .max_lines(1),
                            )
                            .child(label(
                                value(item, "error", status),
                                11.,
                                400.,
                                if status == "failed" {
                                    p.danger
                                } else {
                                    p.muted
                                },
                            )),
                    )
                    .child(btn(
                        format!("file-job-remove-{id}"),
                        if status == "running" {
                            "Cancel batch"
                        } else {
                            "Remove"
                        },
                        p,
                        false,
                        !s.pending && (!active || status == "running"),
                        move |s, _| {
                            if active {
                                s.command("file.cancel", json!({}));
                            } else {
                                s.command("file.remove", json!({"id":id}));
                            }
                        },
                    )),
            );
        }
        panel = panel.child(btn(
            "clear-finished-files",
            "Clear queue",
            p,
            false,
            !active && !s.pending,
            |s, _| s.command("file.clear", json!({})),
        ));
        content = content.child(panel);
    }
    if s.snapshot.busy && !s.snapshot.recording {
        content = content.child(
            card(p).child(
                row()
                    .gap(16.)
                    .center_y()
                    .flex_wrap()
                    .child(label(&s.snapshot.status, 12., 400., p.muted).grow(1.))
                    .child(btn(
                        "file-cancel",
                        "Cancel transcription",
                        p,
                        false,
                        !s.pending,
                        |s, _| s.command("file.cancel", json!({})),
                    )),
            ),
        );
    }
    content.child(transcript(s, p)).child(recent(s, p))
}
