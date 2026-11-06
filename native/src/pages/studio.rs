use crate::state::{Page, State};
use crate::ui::{self, Palette, btn, card, divider, eyebrow, label, link, pill};
use rune::{El, col, el, icon, row};
use rune_kit::glyphs;
use serde_json::json;

use super::{recent, setting_toggle, transcript};

pub fn studio(s: &State, p: &Palette, compact: bool) -> El {
    let heading = row().center_y().gap(20.).child(
        col()
            .grow(1.)
            .gap(12.)
            .child(eyebrow("LESS TYPING. MORE THINKING.", p))
            .child(label(
                "Good things start with a thought.",
                if compact { 27. } else { 33. },
                500.,
                p.ink,
            ))
            .child(label(
                "Say what’s on your mind. We’ll take it from here.",
                13.,
                400.,
                p.muted,
            )),
    );
    let workspace = row().gap(20.).child(record_card(s, p).grow(1.).min_w(0.));
    let workspace = if compact {
        workspace
    } else {
        workspace.child(setup(s, p).w(254.))
    };
    col()
        .gap(27.)
        .child(heading)
        .child(workspace)
        .child(transcript(s, p))
        .child(recent(s, p))
        .child(
            row()
                .gap(9.)
                .center_y()
                .child(icon(&ui::SHIELD, 15.).color(p.muted))
                .child(label(
                    "Your voice. Your choice of models. Your workspace.",
                    11.,
                    400.,
                    p.muted,
                )),
        )
}

fn waveform(s: &State, p: &Palette) -> El {
    let phase = s
        .recording_since
        .map_or(1.4, |start| start.elapsed().as_secs_f32() * 3.);
    let bars = (0..49).map(|i| {
        let x = i as f32;
        let envelope = (1. - ((x - 24.) / 25.).abs()).max(0.);
        let noise =
            ((x * 1.8 + phase).sin() * 0.5 + 0.5) * 0.6 + ((x * 0.73).cos() * 0.5 + 0.5) * 0.4;
        let height = if s.snapshot.recording && !s.demo {
            5. + 85. * (s.snapshot.levels.get(i).copied().unwrap_or(0.).sqrt() * 4.).clamp(0., 1.)
        } else {
            5. + (if s.snapshot.recording { 75. } else { 46. }) * envelope * noise
        };
        el().key(format!("wave-{i}"))
            .size(3., height)
            .radius(2.)
            .fill(if s.snapshot.recording {
                p.accent
            } else {
                p.accent.alpha(0.32 + 0.25 * envelope)
            })
    });
    row()
        .key("waveform")
        .w_full()
        .h(105.)
        .center()
        .gap(4.)
        .children(bars)
}

fn record_card(s: &State, p: &Palette) -> El {
    let recording = s.snapshot.recording;
    let busy = s.snapshot.busy && !recording;
    let status = if recording {
        "LISTENING"
    } else if busy {
        "WORKING ON YOUR WORDS"
    } else {
        "A LITTLE SPACE TO SPEAK"
    };
    let title = if recording {
        "Go on. We’re listening."
    } else if busy {
        "Turning audio into words…"
    } else {
        "Your words, without the keyboard."
    };
    let mut controls = row().gap(10.).center().child(btn(
        "record-toggle",
        if recording && s.snapshot.mode == "walkthrough" {
            "Finish walkthrough"
        } else if recording {
            "Stop recording"
        } else if busy {
            "Transcribing…"
        } else {
            "Start recording"
        },
        p,
        true,
        s.available() && !s.pending && (recording || !s.working()),
        |s, _| s.toggle_record(),
    ));
    if recording || busy {
        controls = controls.child(btn(
            "record-cancel",
            "Cancel",
            p,
            false,
            !s.pending,
            |s, _| s.command("record.cancel", json!({})),
        ));
    }
    let elapsed = if recording {
        s.recording_since.map_or("00:12".into(), |start| {
            ui::duration(start.elapsed().as_secs_f64())
        })
    } else if !s.available() {
        "Connecting to your audio service…".into()
    } else if !s.demo && !s.snapshot.status.is_empty() {
        s.snapshot.status.clone()
    } else {
        "Ready when you are".into()
    };
    card(p)
        .key("record-studio")
        .gap(0.)
        .padding(0.)
        .child(
            row()
                .px(21.)
                .py(17.)
                .center_y()
                .gap(8.)
                .child(icon(&glyphs::MIC, 15.).color(p.accent))
                .child(label("Quick record", 12., 550., p.ink))
                .child(el().grow(1.))
                .child(pill(if recording { "REC" } else { "DICTATION" }, p)),
        )
        .child(divider(p))
        .child(
            col()
                .key("record-center")
                .w_full()
                .px(20.)
                .py(29.)
                .gap(14.)
                .center()
                .fill(p.soft)
                .child(eyebrow(status, p))
                .child(waveform(s, p))
                .child(label(title, 19., 500., p.ink))
                .child(label(
                    "Capture an idea, write a message, or think out loud.",
                    11.5,
                    400.,
                    p.muted,
                ))
                .child(el().h(6.))
                .child(controls)
                .child(label(
                    elapsed,
                    10.5,
                    450.,
                    if recording { p.accent } else { p.muted },
                )),
        )
        .child(divider(p))
        .child(
            row()
                .px(20.)
                .h(47.)
                .center_y()
                .gap(7.)
                .child(icon(&ui::SETTINGS, 14.).color(p.muted))
                .child(label(ui::engine(&s.snapshot.engine), 11., 500., p.muted))
                .child(el().grow(1.))
                .child(link(
                    "record-preferences",
                    "Recording settings",
                    p,
                    |s, _| s.navigate(Page::Settings),
                )),
        )
}

fn setup(s: &State, p: &Palette) -> El {
    card(p)
        .key("setup-card")
        .gap(19.)
        .child(
            row()
                .center_y()
                .child(label("Make it yours", 14., 550., p.ink))
                .child(el().grow(1.))
                .child(icon(&ui::SETTINGS, 17.).color(p.muted)),
        )
        .child(label(
            "A few little things to keep you in flow.",
            11.5,
            400.,
            p.muted,
        ))
        .child(divider(p))
        .child(
            col()
                .gap(8.)
                .child(eyebrow("VOICE MODEL", p))
                .child(
                    row()
                        .gap(8.)
                        .center_y()
                        .child(icon(&glyphs::SPARKLE, 15.).color(p.accent))
                        .child(label(ui::engine(&s.snapshot.engine), 12., 550., p.ink)),
                )
                .child(link("change-model", "Change model →", p, |s, _| {
                    s.navigate(Page::Settings);
                    s.selected_tab = "settings:models".into();
                })),
        )
        .child(divider(p))
        .child(setting_toggle(
            s,
            p,
            "transcript_cleanup_enabled",
            "AI cleanup",
            "Polish as you go",
        ))
        .child(setting_toggle(
            s,
            p,
            "auto_paste",
            "Auto-paste",
            "Right where you’re working",
        ))
        .child(el().grow(1.))
        .child(
            row()
                .gap(8.)
                .padding(12.)
                .radius(8.)
                .fill(p.soft)
                .child(icon(&glyphs::KEYBOARD, 17.).color(p.muted))
                .child(
                    label(
                        "Set a global shortcut in Settings to record from any app.",
                        10.5,
                        400.,
                        p.muted,
                    )
                    .grow(1.),
                ),
        )
}
