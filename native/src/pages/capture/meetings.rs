use super::super::library::{
    collection_pager, collection_search, confirm_delete, empty, export_item, field, value,
};
use crate::state::State;
use crate::ui::{self, Palette, btn, card, divider, eyebrow, label, link, page_header};
use rune::{El, col, el, icon, row};
use serde_json::{Value, json};

fn document(v: &Value) -> String {
    if let Some(text) = v.as_str() {
        return text.to_string();
    }
    for key in ["markdown", "text", "summary", "rolling_summary"] {
        if let Some(text) = v[key].as_str() {
            return text.to_string();
        }
    }
    String::new()
}

pub fn meetings(s: &State, p: &Palette) -> El {
    let active = s.snapshot.meeting;
    let live = &s.data["snapshot"]["meeting_live"];
    let paused = live["paused"].as_bool().unwrap_or(false);
    if let Some(id) = &s.selected_item {
        let mut review = col().key("meeting-review").gap(18.);
        if active {
            review = review.child(
                card(p).padding(16.).child(
                    row()
                        .gap(10.)
                        .center_y()
                        .flex_wrap()
                        .child(
                            label(
                                if paused {
                                    "Your current meeting is paused."
                                } else {
                                    "Your current meeting is still recording."
                                },
                                12.,
                                450.,
                                p.ink,
                            )
                            .grow(1.)
                            .min_w(0.),
                        )
                        .child(btn(
                            "review-meeting-pause",
                            if paused { "Resume" } else { "Pause" },
                            p,
                            false,
                            !s.pending,
                            move |s, _| {
                                s.command(
                                    if paused {
                                        "meeting.resume"
                                    } else {
                                        "meeting.pause"
                                    },
                                    json!({}),
                                )
                            },
                        ))
                        .child(btn(
                            "review-meeting-stop",
                            "End meeting",
                            p,
                            true,
                            !s.pending,
                            |s, _| s.command("meeting.stop", json!({})),
                        )),
                ),
            );
        }
        return review.child(meeting_detail(s, p, id));
    }
    let mut controls = card(p)
        .key("meeting-card")
        .padding(26.)
        .gap(20.)
        .fill(p.soft)
        .child(
            row()
                .gap(18.)
                .center_y()
                .child(
                    row()
                        .size(55., 55.)
                        .radius(16.)
                        .fill(p.accent_soft)
                        .center()
                        .child(icon(&ui::MEETING, 27.).color(p.accent)),
                )
                .child(
                    col()
                        .grow(1.)
                        .min_w(0.)
                        .gap(7.)
                        .child(label(
                            if active {
                                if paused {
                                    "Take a breath. Your meeting is paused."
                                } else {
                                    "Your meeting is underway."
                                }
                            } else {
                                "Make room for a better meeting."
                            },
                            23.,
                            500.,
                            p.ink,
                        ))
                        .child(label(
                            "Keep the conversation, live transcript, and notes together.",
                            12.,
                            400.,
                            p.muted,
                        )),
                ),
        );
    if !active {
        let mode = s
            .edits
            .get("meeting-capture")
            .map(|e| e.text.as_str())
            .unwrap_or("auto");
        controls = controls.child(field(s, p, "meeting-title", "What is this meeting about? (optional)", false))
            .child(row().gap(8.).flex_wrap()
                .child(btn("meeting-audio-both", "Microphone + system audio", p, mode != "mic_only", true, |s, _| s.edit("meeting-capture").text = "auto".into()))
                .child(btn("meeting-audio-mic", "Microphone only", p, mode == "mic_only", true, |s, _| s.edit("meeting-capture").text = "mic_only".into())))
            .child(label("Let participants know you are recording. Capture permissions are reviewed when you start.", 11., 400., p.muted));
    }
    let mut buttons = row().gap(10.).center_y().flex_wrap().child(btn(
        "meeting-toggle",
        if active {
            "End meeting"
        } else {
            "Start a meeting"
        },
        p,
        true,
        s.available() && !s.pending && (active || !s.working()),
        |s, _| {
            if s.snapshot.meeting {
                s.command("meeting.stop", json!({}));
            } else {
                let title = s.edit("meeting-title").text.clone();
                let policy = if s.edit("meeting-capture").text == "mic_only" {
                    "disabled"
                } else {
                    "auto"
                };
                s.command(
                    "meeting.start",
                    json!({"title":title,"system_audio_policy":policy}),
                );
            }
        },
    ));
    if active {
        buttons = buttons
            .child(btn(
                "meeting-pause",
                if paused { "Resume" } else { "Pause" },
                p,
                false,
                !s.pending,
                move |s, _| {
                    s.command(
                        if paused {
                            "meeting.resume"
                        } else {
                            "meeting.pause"
                        },
                        json!({}),
                    )
                },
            ))
            .child(btn(
                "meeting-dashboard",
                "Open web dashboard",
                p,
                false,
                true,
                |s, _| s.command("meeting.dashboard", json!({})),
            ));
    }
    controls = controls.child(divider(p)).child(buttons);
    let mut content = col()
        .gap(24.)
        .child(page_header(
            "Be in the conversation.",
            "Keep your attention on the people. Leave the notes to OpenWhisper.",
            p,
        ))
        .child(controls);
    if active {
        let transcript = document(&live["transcript"]);
        let summary = document(&live["summary"]);
        let mut transcript_body = col().gap(12.).child(
            label(
                if transcript.is_empty() {
                    "Your conversation will appear here as people speak."
                } else {
                    &transcript
                },
                14.,
                400.,
                p.ink,
            )
            .selectable(true),
        );
        if let Some(segments) = live["segments"]
            .as_array()
            .filter(|_| transcript.is_empty())
        {
            for (i, segment) in segments.iter().enumerate() {
                transcript_body = transcript_body.child(
                    col()
                        .key(format!("meeting-segment-{i}"))
                        .gap(4.)
                        .child(label(
                            value(segment, "speaker", "Speaker"),
                            10.,
                            550.,
                            p.accent,
                        ))
                        .child(
                            label(value(segment, "text", ""), 13., 400., p.ink).selectable(true),
                        ),
                );
            }
        }
        let mut panel = card(p)
            .key("meeting-live-transcript")
            .child(
                row()
                    .gap(10.)
                    .center_y()
                    .flex_wrap()
                    .child(eyebrow(
                        if paused { "PAUSED" } else { "LIVE TRANSCRIPT" },
                        p,
                    ))
                    .child(el().grow(1.))
                    .child(label(
                        value(live, "status", "Listening to the conversation"),
                        11.,
                        400.,
                        p.muted,
                    )),
            )
            .child(
                col()
                    .key("meeting-transcript-scroll")
                    .max_h(220.)
                    .scroll(rune::ScrollSpec { stick_bottom: true })
                    .child(transcript_body),
            );
        if !summary.is_empty() {
            panel = panel
                .child(divider(p))
                .child(eyebrow("MEETING NOTES", p))
                .child(label(summary, 13., 400., p.ink).selectable(true));
        }
        content = content.child(panel);
    }
    content = content.child(super::meeting_tools::tools(s, p));
    let mut library = card(p).key("meetings-library").gap(14.).child(
        row()
            .gap(12.)
            .center_y()
            .flex_wrap()
            .child(label("Past meetings", 18., 500., p.ink).grow(1.))
            .child(link("refresh-meetings", "Refresh", p, |s, _| {
                s.command("meetings.list", json!({}))
            })),
    );
    library = library.child(collection_search(
        s,
        p,
        "meetings",
        "meeting-query",
        "Search meetings and transcripts…",
    ));
    if let Some(items) = s.data["meetings.list"]["items"]
        .as_array()
        .filter(|items| !items.is_empty())
    {
        for item in items {
            let id = value(item, "id", "").to_string();
            library = library.child(
                row()
                    .key(format!("meeting-{id}"))
                    .gap(12.)
                    .padding(13.)
                    .radius(9.)
                    .fill(if s.selected_item.as_deref() == Some(&id) {
                        p.accent_soft
                    } else {
                        p.soft
                    })
                    .role("button")
                    .label(value(item, "title", "Meeting"))
                    .focusable(true)
                    .focus_ring(2., 1., p.accent)
                    .hover(p.accent_soft)
                    .on_press_tx(move |s: &mut State, tx| {
                        s.selected_item = Some(id.clone());
                        s.selected_tab.clear();
                        s.confirmation = None;
                        s.command("meetings.detail", json!({"id":id}));
                        tx.scroll_key_to("meeting-review", 0.);
                    })
                    .child(icon(&ui::MEETING, 19.).color(p.accent))
                    .child(
                        col()
                            .grow(1.)
                            .min_w(0.)
                            .gap(5.)
                            .child(
                                label(value(item, "title", "Meeting"), 13., 550., p.ink)
                                    .max_lines(1),
                            )
                            .child(
                                label(
                                    value(
                                        item,
                                        "preview",
                                        value(item, "started_at", "Saved meeting"),
                                    ),
                                    11.,
                                    400.,
                                    p.muted,
                                )
                                .max_lines(2),
                            ),
                    )
                    .child(label(value(item, "status", "Saved"), 10., 450., p.muted)),
            );
        }
    } else {
        library = library.child(empty(
            p,
            "A place for every conversation.",
            "Your saved meetings, transcripts, and reports will appear here.",
        ));
    }
    content = content.child(library.child(collection_pager(s, p, "meetings", "meeting-query")));
    content
}

fn meeting_detail(s: &State, p: &Palette, id: &str) -> El {
    let detail = &s.data["meetings.detail"];
    if detail["id"].as_str() != Some(id) {
        return card(p).child(label("Loading your meeting…", 13., 400., p.muted));
    }
    let title = value(detail, "title", "Meeting");
    let transcript = document(&detail["transcript"]);
    let report = document(&detail["report"]);
    let summary = document(&detail["summary"]);
    let copy = transcript.clone();
    let export_id = id.to_string();
    let md_id = id.to_string();
    let delete_id = id.to_string();
    let delete_title = title.to_string();
    let audio = value(detail, "playback_path", "").to_string();
    let audio_ready = !audio.is_empty() && std::path::Path::new(&audio).is_file();
    let rename_title = title.to_string();
    let mut panel = card(p)
        .key("meeting-detail")
        .gap(17.)
        .child(
            row()
                .gap(12.)
                .center_y()
                .child(eyebrow("MEETING REVIEW", p))
                .child(el().grow(1.))
                .child(link("close-meeting-detail", "Close", p, |s, _| {
                    s.selected_item = None;
                    s.confirmation = None;
                    s.selected_tab.clear();
                })),
        )
        .child(label(title, 23., 500., p.ink))
        .child(
            row()
                .gap(7.)
                .flex_wrap()
                .child(btn(
                    "copy-meeting",
                    "Copy transcript",
                    p,
                    true,
                    !transcript.is_empty(),
                    move |s, tx| {
                        tx.copy(copy.clone());
                        s.notice = Some("Meeting transcript copied".into());
                    },
                ))
                .child(btn(
                    "export-meeting-txt",
                    "Export text",
                    p,
                    false,
                    true,
                    move |s, tx| export_item(s, tx, "meetings", export_id.clone(), "txt"),
                ))
                .child(btn(
                    "export-meeting-md",
                    "Markdown",
                    p,
                    false,
                    true,
                    move |s, tx| export_item(s, tx, "meetings", md_id.clone(), "md"),
                ))
                .child(btn(
                    "play-meeting",
                    if crate::platform::audio_playing() {
                        "Stop audio"
                    } else {
                        "Play audio"
                    },
                    p,
                    false,
                    audio_ready || crate::platform::audio_playing(),
                    move |s, _| {
                        if crate::platform::audio_playing() {
                            crate::platform::stop_audio();
                        } else if let Err(error) = crate::platform::play_audio(&audio) {
                            s.error = Some(error);
                        }
                    },
                )),
        )
        .child(
            row()
                .gap(9.)
                .flex_wrap()
                .child(link("rename-meeting", "Rename", p, move |s, _| {
                    s.selected_tab = "meeting-edit".into();
                    s.edit("saved-meeting-title").text = rename_title.clone();
                }))
                .child(link("delete-meeting", "Delete meeting", p, move |s, _| {
                    s.confirmation =
                        Some(json!({"scope":"meetings","id":delete_id,"title":delete_title}))
                })),
        );
    if s.selected_tab == "meeting-edit" {
        let id = id.to_string();
        panel = panel
            .child(field(s, p, "saved-meeting-title", "Meeting title", false))
            .child(
                row()
                    .gap(8.)
                    .flex_wrap()
                    .child(btn(
                        "save-meeting-title",
                        "Save title",
                        p,
                        true,
                        !s.pending,
                        move |s, _| {
                            let title = s.edit("saved-meeting-title").text.clone();
                            s.command("meetings.update", json!({"id":id,"title":title}));
                        },
                    ))
                    .child(btn(
                        "cancel-meeting-title",
                        "Cancel",
                        p,
                        false,
                        true,
                        |s, _| s.selected_tab.clear(),
                    )),
            );
    }
    if !summary.is_empty() {
        panel = panel
            .child(divider(p))
            .child(eyebrow("SUMMARY", p))
            .child(label(summary, 14., 400., p.ink).selectable(true));
    }
    let tab = match s.selected_tab.as_str() {
        "meeting-report" => "report",
        "meeting-transcript" => "transcript",
        _ => "workspace",
    };
    panel = panel.child(divider(p)).child(
        row()
            .gap(8.)
            .flex_wrap()
            .child(btn(
                "meeting-workspace-tab",
                "Notes & review",
                p,
                tab == "workspace",
                true,
                |s, _| s.selected_tab = "meeting-workspace".into(),
            ))
            .child(btn(
                "meeting-transcript-tab",
                "Transcript",
                p,
                tab == "transcript",
                true,
                |s, _| s.selected_tab = "meeting-transcript".into(),
            ))
            .child(btn(
                "meeting-report-tab",
                "Report",
                p,
                tab == "report",
                true,
                |s, _| s.selected_tab = "meeting-report".into(),
            )),
    );
    let text = if tab == "report" {
        &report
    } else {
        &transcript
    };
    panel = panel.child(if tab == "workspace" {
        super::meeting_tools::tools(s, p)
    } else if tab == "report" && !text.is_empty() {
        super::super::library::markdown(text, p)
    } else {
        label(
            if text.is_empty() {
                if tab == "report" {
                    "No report has been generated for this meeting yet."
                } else {
                    "No transcript was saved for this meeting."
                }
            } else {
                text
            },
            13.,
            400.,
            p.ink,
        )
        .selectable(true)
    });
    if let Some(finalization) = detail["finalization"].as_object() {
        let id = id.to_string();
        let status = finalization
            .get("status")
            .and_then(Value::as_str)
            .unwrap_or("");
        if ["failed", "partial", "interrupted", "needs_retry"].contains(&status) {
            panel = panel
                .child(label(
                    "Some meeting processing needs another try.",
                    12.,
                    400.,
                    p.muted,
                ))
                .child(btn(
                    "retry-meeting-report",
                    "Retry processing",
                    p,
                    false,
                    !s.pending && !s.working(),
                    move |s, _| s.command("meetings.retry", json!({"id":id})),
                ));
        }
    }
    panel.child(confirm_delete(s, p, "meetings"))
}
