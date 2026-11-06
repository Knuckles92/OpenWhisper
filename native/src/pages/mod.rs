use crate::state::{Page, State};
use crate::ui::{self, Palette, btn, card, eyebrow, label, link};
use rune::{El, FileDialogOptions, Tx, col, el, row};
use rune_kit::Switch;
use serde_json::json;

mod capture;
mod library;
mod scratchpad;
mod screenshot;
mod settings;
mod studio;

pub use capture::{files, meetings, walkthrough};
pub use capture::{meeting_tools_demo, meeting_tools_demo_command, meeting_tools_response};
pub use capture::{walkthrough_block_navigation, walkthrough_event, walkthrough_response};
pub use library::calendar_demo;
pub use library::calendar_refresh;
pub use library::calendar_response;
pub use library::history;
pub use library::{library_demo, library_demo_command};
pub use scratchpad::scratchpad;
pub use screenshot::{
    begin_screenshot, finish_screenshot, screenshot, screenshot_finished, screenshot_key,
};
pub use settings::settings;
pub use settings::settings_response;
pub use settings::{settings_demo, settings_demo_command};
pub use studio::studio;

fn setting_toggle(s: &State, p: &Palette, key: &'static str, title: &str, description: &str) -> El {
    row()
        .gap(15.)
        .center_y()
        .child(
            col()
                .grow(1.)
                .min_w(0.)
                .gap(5.)
                .child(label(title, 12., 550., p.ink))
                .child(label(description, 11., 400., p.muted)),
        )
        .child(
            Switch::new(s.setting(key))
                .label(title)
                .hide_label()
                .disabled(!s.available() || s.working())
                .on_toggle(move |s: &mut State| {
                    let value = !s.setting(key);
                    s.command("settings.update", json!({"key":key,"value":value}));
                })
                .build(&p.kit)
                .key(format!("toggle-{key}")),
        )
}

fn transcript(s: &State, p: &Palette) -> El {
    if s.snapshot.recording && !s.snapshot.preview.is_empty() {
        return card(p)
            .key("live-preview")
            .child(eyebrow("LIVE PREVIEW", p))
            .child(label(&s.snapshot.preview, 15., 400., p.ink).selectable(true));
    }
    if s.snapshot.transcript.is_empty() {
        return el();
    }
    let value = if s.show_raw {
        &s.snapshot.raw
    } else {
        &s.snapshot.transcript
    };
    card(p)
        .key("transcript-result")
        .child(
            row()
                .center_y()
                .gap(10.)
                .child(label("Your words", 15., 550., p.ink))
                .child(el().grow(1.))
                .child(link(
                    "toggle-raw",
                    if s.show_raw {
                        "Show cleaned"
                    } else {
                        "As spoken"
                    },
                    p,
                    |s, _| s.show_raw = !s.show_raw,
                ))
                .child(btn(
                    "copy-transcript",
                    "Copy text",
                    p,
                    false,
                    true,
                    |s, tx| {
                        tx.copy(if s.show_raw {
                            s.snapshot.raw.clone()
                        } else {
                            s.snapshot.transcript.clone()
                        });
                        s.notice = Some("Copied to clipboard".into());
                    },
                )),
        )
        .child(label(value, 14., 400., p.ink).selectable(true))
        .child(link(
            "export-transcript",
            "Export transcript →",
            p,
            export_transcript,
        ))
}

pub fn export_transcript(s: &mut State, tx: &mut Tx) {
    let content = if s.page == Page::History {
        if s.show_raw {
            s.detail_raw.clone()
        } else {
            s.detail.clone()
        }
    } else if s.show_raw {
        s.snapshot.raw.clone()
    } else {
        s.snapshot.transcript.clone()
    };
    let options = FileDialogOptions {
        title: "Export transcript".into(),
        file_name: Some("transcript.txt".into()),
        ..Default::default()
    }
    .filter("Plain text", &["txt"]);
    tx.save_file(options, move |s: &mut State, result, _| match result {
        Ok(Some(path)) => match std::fs::write(path, &content) {
            Ok(()) => s.notice = Some("Transcript exported".into()),
            Err(error) => s.error = Some(format!("Could not export transcript: {error}")),
        },
        Ok(None) => {}
        Err(error) => s.error = Some(error.to_string()),
    });
}

fn recent(s: &State, p: &Palette) -> El {
    let heading = row()
        .center_y()
        .child(label("Recently captured", 15., 550., p.ink))
        .child(el().grow(1.))
        .child(link("all-history", "View all history →", p, |s, _| {
            s.navigate(Page::History)
        }));
    let mut list = col()
        .radius(12.)
        .fill(p.surface)
        .border(1., p.border)
        .padding(5.)
        .gap(2.);
    if s.snapshot.history.is_empty() {
        list = list.child(
            col()
                .padding(23.)
                .gap(7.)
                .child(label("A fresh page for your thoughts.", 13., 500., p.ink))
                .child(label(
                    "Your recordings and transcribed files will appear here.",
                    12.,
                    400.,
                    p.muted,
                )),
        );
    } else {
        for entry in s.snapshot.history.iter().take(3) {
            list = list.child(ui::entry_row(entry, p, false, true));
        }
    }
    col()
        .key("recent-history")
        .gap(12.)
        .child(heading)
        .child(list)
}
