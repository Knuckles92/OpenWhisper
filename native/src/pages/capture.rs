use crate::state::State;
use crate::ui::{self, Palette, btn, card, divider, eyebrow, label, link, page_header};
use rune::{El, FileDialogOptions, Tx, col, icon, row};
use rune_kit::glyphs;
use serde_json::json;

use super::{recent, transcript};

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
            Ok(Some(path)) => s.choose_file(path.to_string_lossy().into()),
            Ok(None) => {}
            Err(e) => s.error = Some(e.to_string()),
        },
    );
}

pub fn files(s: &State, p: &Palette) -> El {
    let drop = card(p)
        .key("file-drop")
        .h(270.)
        .center()
        .gap(17.)
        .fill(p.soft)
        .border_dash(5., 5.)
        .on_drop_files(|s: &mut State, files, _| {
            if let Some(path) = files.first() {
                s.choose_file(path.to_string_lossy().into());
            }
        })
        .accepts_extensions(&[
            "wav", "mp3", "m4a", "flac", "ogg", "opus", "mp4", "webm", "aac", "aiff", "mov",
        ])
        .child(
            row()
                .size(55., 55.)
                .center()
                .radius(16.)
                .fill(p.accent_soft)
                .child(icon(&ui::UPLOAD, 25.).color(p.accent)),
        )
        .child(label("Drop a recording. Find the words.", 23., 500., p.ink))
        .child(label(
            "Audio, video, a voice memo — give it a home here.",
            12.,
            400.,
            p.muted,
        ))
        .child(btn(
            "choose-file",
            "Browse files",
            p,
            true,
            !s.working(),
            choose_file,
        ))
        .child(label(
            "WAV, MP3, M4A, FLAC, MP4 and more",
            10.5,
            400.,
            p.muted,
        ));
    let mut content = col()
        .gap(24.)
        .child(page_header(
            "Let your recordings do the talking.",
            "Turn an audio or video file into a transcript you can use.",
            p,
        ))
        .child(drop);
    if let Some(file) = &s.file {
        let name = std::path::Path::new(file)
            .file_name()
            .unwrap_or_default()
            .to_string_lossy();
        content = content.child(
            card(p).child(
                row()
                    .center_y()
                    .gap(13.)
                    .child(icon(&glyphs::FILE, 22.).color(p.accent))
                    .child(
                        col()
                            .grow(1.)
                            .min_w(0.)
                            .gap(5.)
                            .child(label(name, 14., 550., p.ink).max_lines(1))
                            .child(label(
                                "Ready to transcribe with your selected voice model",
                                11.,
                                400.,
                                p.muted,
                            )),
                    )
                    .child(btn(
                        "transcribe-file",
                        if s.snapshot.busy {
                            "Transcribing…"
                        } else {
                            "Transcribe file"
                        },
                        p,
                        true,
                        s.available() && !s.working(),
                        |s, _| {
                            if let Some(path) = s.file.clone() {
                                s.command("file.transcribe", json!({"path":path}));
                            }
                        },
                    ))
                    .child(link("remove-file", "Remove", p, |s, _| s.file = None)),
            ),
        );
    }
    if s.snapshot.busy && !s.snapshot.recording {
        content = content.child(
            card(p).child(
                row()
                    .center_y()
                    .gap(16.)
                    .child(label(&s.snapshot.status, 12., 400., p.muted).grow(1.))
                    .child(btn(
                        "file-cancel",
                        "Cancel transcription",
                        p,
                        false,
                        !s.pending,
                        |s, _| s.command("cancel", json!({})),
                    )),
            ),
        );
    }
    content.child(transcript(s, p)).child(recent(s, p))
}

pub fn meetings(s: &State, p: &Palette) -> El {
    let active = s.snapshot.meeting;
    col().gap(25.).child(page_header("Be in the conversation.","Keep your attention on the people. Leave the notes to OpenWhisper.",p))
        .child(card(p).key("meeting-card").padding(32.).gap(22.).fill(p.soft)
            .child(row().gap(20.).center_y().child(row().size(61.,61.).radius(18.).fill(p.accent_soft).center().child(icon(&ui::MEETING,30.).color(p.accent)))
                .child(col().grow(1.).gap(8.).child(label(if active {"Your meeting is underway."} else {"Make room for a better meeting."},24.,500.,p.ink))
                    .child(label("Microphone + system audio, with a live transcript and searchable notes.",12.,400.,p.muted))))
            .child(divider(p))
            .child(row().gap(12.).center_y().flex_wrap()
                .child(btn("meeting-toggle",if active {"End meeting"} else {"Start a meeting"},p,true,s.available() && !s.pending && (active || !s.working()),|s,_|s.command(if s.snapshot.meeting {"meeting.stop"} else {"meeting.start"},json!({}))))
                .child(btn("meeting-dashboard","Open live dashboard",p,false,active,|s,_|s.command("meeting.dashboard",json!({}))))
                .child(label(if active {"Recording the conversation"} else {"You’ll review audio and consent before capture begins."},11.,400.,p.muted))))
        .child(row().gap(16.).flex_wrap()
            .child(feature(p,"01","Capture the conversation","Keep microphone and system audio together."))
            .child(feature(p,"02","Follow the thread","See the transcript and insights in the live dashboard."))
            .child(feature(p,"03","Keep what matters","Review notes, play recordings, and export your meeting.")))
        .child(card(p).child(row().gap(15.).center_y().child(icon(&ui::SHIELD,21.).color(p.accent))
            .child(col().grow(1.).gap(6.).child(label("A good meeting starts with everyone knowing.",13.,550.,p.ink))
                .child(label("Let participants know you’re recording. Your existing capture permissions and model preferences apply.",12.,400.,p.muted)))))
        .child(link("past-meetings","Past meetings & advanced controls →",p,|s,_|s.command("window.classic",json!({"page":"meeting"}))))
}
fn feature(p: &Palette, number: &str, title: &str, description: &str) -> El {
    card(p)
        .grow(1.)
        .min_w(190.)
        .gap(14.)
        .child(eyebrow(number, p))
        .child(label(title, 14., 550., p.ink))
        .child(label(description, 12., 400., p.muted))
}

pub fn walkthrough(s: &State, p: &Palette) -> El {
    col().gap(25.).child(page_header("Show it. Say it. Save it.","Turn a spoken walkthrough and screenshots into a document worth sharing.",p))
        .child(card(p).key("walkthrough-card").padding(32.).gap(22.).fill(p.soft)
            .child(row().gap(18.).center_y().child(row().size(61.,61.).radius(18.).fill(p.accent_soft).center().child(icon(&ui::WALK,30.).color(p.accent)))
                .child(col().grow(1.).gap(8.).child(label("A little context goes a long way.",24.,500.,p.ink))
                    .child(label("Talk through a task. Capture the moments. Keep the story together.",12.,400.,p.muted))))
            .child(divider(p))
            .child(row().gap(12.).flex_wrap()
                .child(btn("walkthrough-toggle",if s.snapshot.mode=="walkthrough" && s.snapshot.recording {"Finish walkthrough"} else {"Start walkthrough"},p,true,s.available()&&!s.pending&&((s.snapshot.mode=="walkthrough" && s.snapshot.recording) || !s.working()),
                    |s,_|s.command("walkthrough.toggle",json!({}))))
                .child(btn("walkthrough-snap","Capture screenshot",p,false,s.snapshot.mode=="walkthrough"&&s.snapshot.recording&&!s.pending,|s,_|s.command("walkthrough.snap",json!({}))))
                .child(btn("walkthrough-cancel","Cancel",p,false,s.snapshot.mode=="walkthrough"&&s.snapshot.recording&&!s.pending,|s,_|s.command("cancel",json!({}))))))
        .child(row().gap(16.).flex_wrap().child(feature(p,"01","Walk through it","Explain a feature, a bug, or a process in your own words."))
            .child(feature(p,"02","Capture the detail","Add screenshots as you go. They stay beside your words."))
            .child(feature(p,"03","Pass it on","Get a PDF, Word document, and Markdown with your screenshots.")))
        .child(link("walkthrough-library","Saved walkthroughs & write-up preferences →",p,|s,_|s.command("window.classic",json!({"page":"walkthrough"}))))
}
