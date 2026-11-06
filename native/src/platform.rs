//! Native windows, tray, nonactivating recording HUD and media playback.
//!
//! The audio service retains the user's global shortcut registrations, including
//! release events needed for push-and-hold. There is one owner for every chord.

use crate::state::{Page, State};
use crate::ui::{self, Palette};
#[cfg(target_os = "macos")]
use rune::MenuItem;
use rune::{
    Align, App, AppHandle, Cx, El, OverlayHandle, OverlayInput, OverlayOptions, OverlaySpec,
    ScreenEdge, Tray, TrayEvent, TrayIcon, TrayItem, TraySpec, Tx, col, el, row,
};
use serde_json::json;
use std::cell::RefCell;
use std::fs::File;
use std::io::BufReader;
use std::path::Path;
use std::process::Command;

#[cfg(target_os = "macos")]
mod mac_tray;

pub struct Desktop {
    app: AppHandle<State>,
    hud: OverlayHandle<Hud>,
    tray: Option<Tray>,
    recording: bool,
    meeting: bool,
    busy: bool,
    walkthrough: bool,
    started: Option<std::time::Instant>,
    event_id: u64,
    pub quit_pending: bool,
    capture_active: bool,
    capture_return_visible: bool,
}

struct Hud {
    dark: bool,
    title: String,
    caption: String,
    levels: Vec<f32>,
    app: AppHandle<State>,
    look: String,
    dot: bool,
    show_title: bool,
    clock: bool,
    elapsed: String,
    caption_size: f32,
    footer: bool,
    phase: f32,
}

pub fn install(app: &mut App<State>) -> Desktop {
    let handle = app.handle();
    let hud = app.overlay(
        OverlaySpec {
            name: "OpenWhisper recording",
            size: (390., 100.),
            edge: ScreenEdge::Bottom,
            margin: 32.,
            align: Align::Center,
            inset: 0.,
            interactive: true,
            view: hud_view,
        }
        .options(OverlayOptions {
            input: OverlayInput::Regions,
            exclude_from_capture: true,
            xwayland_input: std::env::var_os("DISPLAY").is_some(),
            ..Default::default()
        })
        .release_gpu_when_hidden(),
        Hud {
            dark: true,
            title: String::new(),
            caption: String::new(),
            levels: Vec::new(),
            app: handle.clone(),
            look: "bars".into(),
            dot: true,
            show_title: true,
            clock: true,
            elapsed: String::new(),
            caption_size: 12.,
            footer: false,
            phase: 0.,
        },
    );
    Desktop {
        app: handle,
        hud,
        tray: None,
        recording: false,
        meeting: false,
        busy: false,
        walkthrough: false,
        started: None,
        event_id: 0,
        quit_pending: false,
        capture_active: false,
        capture_return_visible: false,
    }
}

pub fn menus(app: App<State>) -> App<State> {
    let app = app.standard_menus();
    #[cfg(target_os = "macos")]
    {
        app.menu_item(
            MenuItem::new("New Dictation", |s: &mut State, _| s.toggle_record())
                .id("dictation")
                .menu("File"),
        )
        .menu_item(
            MenuItem::new("History", |s: &mut State, _| s.navigate(Page::History))
                .id("history")
                .menu("View"),
        )
        .menu_item(
            MenuItem::new("Settings…", |s: &mut State, _| s.navigate(Page::Settings))
                .id("settings")
                .key_equivalent("cmd+,".parse().unwrap()),
        )
        .menu_item(
            MenuItem::new("Open OpenWhisper", |s: &mut State, _| {
                if let Some(d) = &s.desktop {
                    d.app.open();
                }
            })
            .dock(),
        )
    }
    #[cfg(not(target_os = "macos"))]
    {
        app
    }
}

pub fn ready(state: &mut State, mut desktop: Desktop) {
    #[cfg(target_os = "macos")]
    if let Err(error) = mac_tray::install_quit_guard(desktop.app.clone()) {
        eprintln!("OpenWhisper safe quit: {error}");
        state.error = Some(format!("Could not install safe quit: {error}"));
    }
    if !state.demo {
        #[cfg(target_os = "macos")]
        if let Err(error) = mac_tray::install(desktop.app.clone()) {
            eprintln!("OpenWhisper menu bar: {error}");
        }
        let handle = desktop.app.clone();
        desktop.tray = Tray::new(
            TraySpec::new("openwhisper", "OpenWhisper", tray_icon())
                .tooltip("OpenWhisper · Ready")
                .menu(tray_menu(false, false, false, false)),
            move |event| match event {
                TrayEvent::Activate { token } => {
                    if let Some(token) = token {
                        handle.activate(token);
                    } else {
                        handle.open();
                    }
                }
                TrayEvent::Menu(action) => {
                    let app = handle.clone();
                    handle.update(move |state, _| tray_action(state, &app, &action));
                }
                _ => {}
            },
        )
        .ok();
    }
    state.desktop = Some(desktop);
    sync(state);
}

fn tray_action(state: &mut State, app: &AppHandle<State>, action: &str) {
    match action {
        "open" => app.open(),
        "record" => state.toggle_record(),
        "meeting" => {
            let method = if state.snapshot.meeting {
                "meeting.stop"
            } else {
                "meeting.start"
            };
            state.command(method, json!({}));
        }
        "cancel" => state.command(
            if state.snapshot.mode == "walkthrough" {
                "walkthrough.cancel"
            } else {
                "record.cancel"
            },
            json!({}),
        ),
        "history" => {
            state.navigate(Page::History);
            app.open();
        }
        "settings" => {
            state.navigate(Page::Settings);
            app.open();
        }
        "scratchpad" => {
            state.navigate(Page::Scratchpad);
            app.open();
        }
        "quit" => request_quit(state),
        _ => {}
    }
}

/// Closing a window keeps captures alive when the desktop has a way to reopen it.
pub fn on_close(state: &mut State, tx: &mut Tx) {
    if let Some(desktop) = &state.desktop {
        let minimize = setting(state, "minimize_tray")
            .and_then(|v| v.as_bool())
            .unwrap_or(true);
        if !minimize || (desktop.tray.is_none() && !cfg!(target_os = "macos")) {
            request_quit(state);
        }
    } else {
        tx.quit();
    }
}

pub fn request_quit(state: &mut State) {
    if let Some(desktop) = &mut state.desktop {
        if state.snapshot.recording || state.snapshot.meeting || state.snapshot.busy {
            desktop.quit_pending = true;
            desktop.app.open();
        } else {
            finish_quit(state);
        }
    }
}

fn finish_quit(state: &mut State) {
    if let Some(desktop) = &mut state.desktop {
        desktop.quit_pending = false;
        // A close-button request may already have hidden the window. Keep notes
        // reachable if the service rejects shutdown because it cannot save them.
        if state.client.is_some() && !state.demo {
            desktop.app.open();
        }
    }
    // The service flushes durable scratchpad text before acknowledging. A save
    // failure leaves this window open with the recoverable text and error.
    if state.client.is_some() && !state.demo {
        state.report_window_focus();
        state.command("app.quit", json!({}));
    } else if let Some(desktop) = &state.desktop {
        desktop.app.quit();
    }
}

pub fn quit_accepted(state: &State) {
    if let Some(desktop) = &state.desktop {
        desktop.app.quit();
    }
}

pub fn quit_confirmation(state: &State, p: &Palette) -> Option<El> {
    if !state.desktop.as_ref().is_some_and(|d| d.quit_pending) {
        return None;
    }
    let body = ui::card(p)
        .key("quit-confirmation")
        .w(440.)
        .max_w(440.)
        .role("dialog")
        .label("A capture is still in progress")
        .modal(false, |s: &mut State| {
            if let Some(d) = &mut s.desktop {
                d.quit_pending = false;
            }
        })
        .child(ui::label(
            "A capture is still in progress",
            20.,
            600.,
            p.ink,
        ))
        .child(ui::label(
            "Finish your capture to save it, or quit and discard the unfinished recording.",
            13.,
            400.,
            p.muted,
        ))
        .child(
            row()
                .gap(10.)
                .child(ui::btn(
                    "keep-openwhisper",
                    "Keep capturing",
                    p,
                    true,
                    true,
                    |s, _| {
                        if let Some(d) = &mut s.desktop {
                            d.quit_pending = false;
                        }
                    },
                ))
                .child(ui::btn(
                    "quit-openwhisper",
                    "Discard and quit",
                    p,
                    false,
                    true,
                    |s, _| {
                        s.command(
                            if s.snapshot.mode == "walkthrough" {
                                "walkthrough.cancel"
                            } else {
                                "record.cancel"
                            },
                            json!({}),
                        );
                        finish_quit(s);
                    },
                )),
        );
    Some(
        el().key("quit-backdrop")
            .absolute(0., 0.)
            .w_full()
            .h_full()
            .fill(rune::Color::BLACK.alpha(0.3))
            .child(col().w_full().h_full().center().padding(24.).child(body)),
    )
}

pub fn event(state: &mut State, name: &str, data: &serde_json::Value, _: &mut Tx) {
    let Some(app) = state.desktop.as_ref().map(|d| d.app.clone()) else {
        return;
    };
    match name {
        "window.show" => app.open(),
        "window.hide" => app.hide(),
        "window.toggle" => {
            if app.is_visible() {
                app.hide();
            } else {
                app.open();
            }
        }
        "scratchpad.show" | "scratchpad.toggle" => {
            state.edits.entry("scratchpad".into()).or_default().text =
                data["text"].as_str().unwrap_or("").into();
            if name == "scratchpad.show"
                || state.data["snapshot"]["scratchpad_save"]["loaded"] == true
            {
                state.data["scratchpad.loaded"] = json!(true);
            }
            if name == "scratchpad.toggle" && state.page == Page::Scratchpad && app.is_visible() {
                app.hide();
            } else {
                state.navigate(Page::Scratchpad);
                app.open();
            }
        }
        "navigate" => {
            match data["page"].as_str().unwrap_or("") {
                "settings" => state.navigate(Page::Settings),
                "models" => {
                    state.navigate(Page::Settings);
                    state.selected_tab = "settings:models".into();
                }
                "history" => state.navigate(Page::History),
                "walkthroughs" => state.navigate(Page::Walkthrough),
                _ => {}
            }
            app.open();
        }
        "overlay.capture_exclusion" => {
            if let Some(desktop) = &state.desktop {
                desktop
                    .hud
                    .set_exclude_from_capture(data["enabled"].as_bool().unwrap_or(true));
            }
        }
        "platform.permission" => {
            state.data["permission_prompt"] = data.clone();
            app.open();
        }
        "screenshot.capture" => {
            if let Some(desktop) = &mut state.desktop {
                if data["active"] == true {
                    if !desktop.capture_active {
                        desktop.capture_return_visible = app.is_visible();
                    }
                    desktop.capture_active = true;
                    desktop.hud.hide();
                    app.hide();
                } else {
                    desktop.capture_active = false;
                    if desktop.capture_return_visible {
                        app.show();
                    } else {
                        app.hide();
                    }
                }
            }
        }
        "screenshot.choose" => app.open(),
        "application.quit" => request_quit(state),
        _ => {}
    }
}

pub fn permission_prompt(state: &State, p: &Palette) -> Option<El> {
    let permission = state.data["permission_prompt"]["permission"].as_str()?;
    let (title, message) = match permission {
        "accessibility" => (
            "Allow auto-paste",
            "Allow OpenWhisper in Privacy & Security → Accessibility so it can paste your words into the app you are using. Your transcript is still available to copy.",
        ),
        "screen_recording" => (
            "Allow screen recording",
            "Allow OpenWhisper in Privacy & Security → Screen Recording to capture walkthrough screenshots and system audio. Return here and try again after allowing access.",
        ),
        "microphone" => (
            "Allow microphone access",
            "Allow OpenWhisper to use your microphone in system privacy settings, then return here and start your recording again.",
        ),
        _ => (
            "Desktop permission needed",
            "Open your system privacy settings to allow this feature, then try again.",
        ),
    };
    let message = state.data["permission_prompt"]["message"]
        .as_str()
        .filter(|v| !v.is_empty())
        .unwrap_or(message);
    let permission = permission.to_owned();
    let mut actions = row().gap(10.).justify_end().child(ui::btn(
        "permission-later",
        "Later",
        p,
        false,
        true,
        |s, _| s.data["permission_prompt"] = serde_json::Value::Null,
    ));
    if cfg!(any(target_os = "macos", target_os = "windows")) {
        actions = actions.child(ui::btn(
            "permission-settings",
            "Open System Settings",
            p,
            true,
            true,
            move |s, _| match open_permission_settings(&permission) {
                Ok(()) => s.data["permission_prompt"] = serde_json::Value::Null,
                Err(error) => s.error = Some(error),
            },
        ));
    }
    let body = ui::card(p)
        .key("permission-prompt")
        .w(530.)
        .max_w(530.)
        .role("dialog")
        .label(title)
        .modal(false, |s: &mut State| {
            s.data["permission_prompt"] = serde_json::Value::Null
        })
        .child(ui::label(title, 22., 550., p.ink))
        .child(ui::label(message, 13., 400., p.muted))
        .child(actions);
    Some(
        el().key("permission-backdrop")
            .absolute(0., 0.)
            .w_full()
            .h_full()
            .fill(rune::Color::BLACK.alpha(0.3))
            .child(col().w_full().h_full().center().padding(24.).child(body)),
    )
}

fn open_permission_settings(permission: &str) -> Result<(), String> {
    #[cfg(target_os = "macos")]
    let result = {
        let pane = match permission {
            "accessibility" => "Privacy_Accessibility",
            "microphone" => "Privacy_Microphone",
            _ => "Privacy_ScreenCapture",
        };
        Command::new("open")
            .arg(format!(
                "x-apple.systempreferences:com.apple.preference.security?{pane}"
            ))
            .spawn()
    };
    #[cfg(target_os = "windows")]
    let result = {
        let pane = match permission {
            "microphone" => "privacy-microphone",
            "screen_recording" => "privacy-screenshotborders",
            _ => "easeofaccess",
        };
        Command::new("explorer.exe")
            .arg(format!("ms-settings:{pane}"))
            .spawn()
    };
    #[cfg(not(any(target_os = "macos", target_os = "windows")))]
    let result = {
        let _ = permission;
        Err::<std::process::Child, _>(std::io::Error::other(
            "Open your desktop's privacy settings",
        ))
    };
    result
        .map(|_| ())
        .map_err(|e| format!("Could not open privacy settings: {e}"))
}

pub fn sync(state: &mut State) {
    let look = setting(state, "recording_overlay_look")
        .and_then(|v| v.as_str())
        .unwrap_or("bars")
        .to_owned();
    let dot = setting(state, "recording_overlay_dot")
        .and_then(|v| v.as_bool())
        .unwrap_or(true);
    let show_title = setting(state, "recording_overlay_text")
        .and_then(|v| v.as_bool())
        .unwrap_or(true);
    let clock = setting(state, "recording_overlay_clock")
        .and_then(|v| v.as_bool())
        .unwrap_or(true);
    let captions = setting(state, "streaming_overlay_enabled")
        .and_then(|v| v.as_bool())
        .unwrap_or(true);
    let caption_size = setting(state, "streaming_overlay_font_size")
        .and_then(|v| v.as_f64())
        .unwrap_or(16.) as f32;
    let footer =
        setting(state, "streaming_overlay_layout").and_then(|v| v.as_str()) == Some("footer");
    let Some(desktop) = &mut state.desktop else {
        return;
    };
    if state.demo {
        return;
    }
    let recording = state.snapshot.recording;
    let meeting = state.snapshot.meeting;
    let busy = state.snapshot.busy;
    let walkthrough = state.snapshot.mode == "walkthrough";
    if (recording || meeting) && !(desktop.recording || desktop.meeting) {
        desktop.started = Some(std::time::Instant::now());
    }
    if !recording && !meeting && !busy {
        desktop.started = None;
    }
    if recording != desktop.recording
        || meeting != desktop.meeting
        || busy != desktop.busy
        || walkthrough != desktop.walkthrough
    {
        #[cfg(target_os = "macos")]
        mac_tray::update(recording, meeting, busy, walkthrough);
        if let Some(tray) = &desktop.tray {
            tray.set_menu(tray_menu(recording, meeting, busy, walkthrough));
            tray.set_tooltip(if meeting {
                "OpenWhisper · Meeting in progress"
            } else if recording {
                "OpenWhisper · Recording"
            } else {
                "OpenWhisper · Ready"
            });
        }
        desktop.recording = recording;
        desktop.meeting = meeting;
        desktop.busy = busy;
        desktop.walkthrough = walkthrough;
    }
    let overlay_enabled = state
        .snapshot
        .settings
        .get("show_overlay")
        .or_else(|| state.snapshot.settings.get("overlay_enabled"))
        .and_then(|v| v.as_bool())
        .unwrap_or(true);
    let visible = !desktop.capture_active && overlay_enabled && (recording || busy || meeting);
    if visible {
        let dark = state.dark;
        let title = if walkthrough {
            "Walkthrough in progress"
        } else if meeting {
            "Meeting in progress"
        } else if recording {
            "Listening"
        } else {
            "Preparing your words"
        }
        .to_string();
        let caption = if !captions {
            String::new()
        } else if state.snapshot.preview.is_empty() {
            state.snapshot.status.clone()
        } else {
            state.snapshot.preview.clone()
        };
        let levels = state.snapshot.levels.clone();
        let phase = state
            .recording_since
            .or(desktop.started)
            .map_or(0., |start| start.elapsed().as_secs_f32());
        let elapsed = ui::duration(phase as f64);
        let height = if footer {
            if caption.is_empty() { 98. } else { 150. }
        } else if caption.is_empty() {
            64.
        } else {
            112.
        };
        desktop.hud.resize((390., height));
        desktop.hud.update(move |hud, _| {
            hud.dark = dark;
            hud.title = title;
            hud.caption = caption;
            hud.levels = levels;
            hud.look = look;
            hud.dot = dot;
            hud.show_title = show_title;
            hud.clock = clock;
            hud.elapsed = elapsed;
            hud.caption_size = caption_size.clamp(10., 24.);
            hud.footer = footer;
            hud.phase = phase;
        });
        desktop.hud.show();
    } else {
        desktop.hud.hide();
    }
    // UI adapters emit monotonically numbered events. Only explicit window
    // requests activate a window; recording/status updates never steal focus.
    let events = state
        .data
        .pointer("/snapshot/ui/events")
        .or_else(|| state.data.pointer("/snapshot/events"))
        .and_then(|v| v.as_array())
        .cloned()
        .unwrap_or_default();
    for event in events {
        let id = event.get("id").and_then(|v| v.as_u64()).unwrap_or(0);
        if id <= desktop.event_id {
            continue;
        }
        desktop.event_id = id;
        let kind = event
            .get("type")
            .or_else(|| event.get("event"))
            .and_then(|v| v.as_str())
            .unwrap_or("");
        match kind {
            "window.show" | "window.main" | "window.scratchpad" => desktop.app.open(),
            "window.hide" => desktop.app.hide(),
            "application.quit" => desktop.app.quit(),
            _ => {}
        }
    }
}

fn setting<'a>(state: &'a State, key: &str) -> Option<&'a serde_json::Value> {
    state
        .data
        .get("settings.values")
        .and_then(|v| v.get("values"))
        .and_then(|v| v.get(key))
        .or_else(|| state.snapshot.settings.get(key))
}

fn tray_menu(recording: bool, meeting: bool, busy: bool, walkthrough: bool) -> Vec<TrayItem> {
    vec![
        TrayItem::action("open", "Open OpenWhisper"),
        TrayItem::Separator,
        TrayItem::action(
            "record",
            if recording && walkthrough {
                "Finish walkthrough"
            } else if recording {
                "Finish dictation"
            } else {
                "Start dictation"
            },
        )
        .enabled(recording || (!meeting && !busy)),
        TrayItem::action(
            "meeting",
            if meeting {
                "End meeting"
            } else {
                "Start meeting"
            },
        )
        .enabled(meeting || (!recording && !busy)),
        TrayItem::action("cancel", "Cancel capture").enabled(recording || busy),
        TrayItem::Separator,
        TrayItem::action("history", "History"),
        TrayItem::action("settings", "Settings"),
        TrayItem::action("scratchpad", "Scratchpad"),
        TrayItem::Separator,
        TrayItem::action("quit", "Quit OpenWhisper"),
    ]
}

fn tray_icon() -> TrayIcon {
    let mut pixels = vec![0; 32 * 32 * 4];
    for y in 0..32_i32 {
        for x in 0..32_i32 {
            let inside = (x - 16).pow(2) + (y - 16).pow(2) < 15_i32.pow(2);
            let microphone = ((12..20).contains(&x) && (6..20).contains(&y))
                || ((8..24).contains(&x) && (16..24).contains(&y) && !(10..22).contains(&x))
                || ((15..17).contains(&x) && (21..27).contains(&y))
                || ((11..21).contains(&x) && (26..28).contains(&y));
            let color = if microphone {
                [235, 245, 231, 255]
            } else if inside {
                [49, 94, 66, 255]
            } else {
                [0, 0, 0, 0]
            };
            let i = ((y * 32 + x) * 4) as usize;
            pixels[i..i + 4].copy_from_slice(&color);
        }
    }
    TrayIcon::rgba(32, 32, pixels)
}

fn hud_view(hud: &Hud, _: &mut Cx<Hud>) -> El {
    let p = Palette::new(hud.dark);
    let stop = hud.app.clone();
    let mut heading = row().gap(9.).center_y();
    if hud.dot {
        heading = heading.child(el().size(6., 6.).radius(3.).fill(p.danger));
    }
    if hud.show_title {
        heading = heading.child(ui::label(hud.title.clone(), 13., 650., p.ink));
    }
    if hud.clock {
        heading = heading.child(ui::label(hud.elapsed.clone(), 11., 450., p.muted));
    }
    heading = heading.child(el().grow(1.));
    if !hud.footer {
        heading = heading.child(meter(hud, &p));
    }
    heading = heading.child(
        ui::label("Finish", 11., 600., p.accent)
            .px(9.)
            .py(6.)
            .radius(6.)
            .fill(p.accent_soft)
            .hit()
            .on_press_tx(move |_: &mut Hud, _| {
                stop.update(|state, _| {
                    if state.snapshot.meeting {
                        state.command("meeting.stop", json!({}));
                    } else {
                        state.toggle_record();
                    }
                })
            }),
    );
    let mut content = col()
        .gap(8.)
        .padding(15.)
        .w(390.)
        .radius(18.)
        .fill(p.surface)
        .border(1., p.border)
        .child(heading);
    if !hud.caption.is_empty() {
        content = content.child(
            ui::label(
                hud.caption.chars().take(120).collect::<String>(),
                hud.caption_size,
                400.,
                p.muted,
            )
            .max_h(43.),
        );
    }
    if hud.footer {
        content = content.child(row().center().child(meter(hud, &p)));
    }
    content
}

fn meter(hud: &Hud, p: &Palette) -> El {
    let level = hud
        .levels
        .last()
        .copied()
        .unwrap_or(0.)
        .sqrt()
        .clamp(0., 1.);
    let phase = hud.phase;
    let mut canvas = el().size(94., 26.);
    match hud.look.as_str() {
        "pulse" => {
            for i in 0..3 {
                let size = 10. + i as f32 * 6. + level * 6.;
                canvas = canvas.child(
                    el().absolute(47. - size / 2., 13. - size / 2.)
                        .size(size, size)
                        .radius(size / 2.)
                        .border(1.4, p.accent.alpha(0.8 - i as f32 * 0.22)),
                );
            }
        }
        "glow" | "aurora" => {
            for i in 0..5 {
                let height = 10. + ((phase * 2. + i as f32).sin() * 0.5 + 0.5) * level * 16.;
                canvas = canvas.child(
                    el().absolute(i as f32 * 17., 13. - height / 2.)
                        .size(23., height)
                        .radius(12.)
                        .gradient(p.accent.alpha(0.25 + level * 0.5), p.accent_soft),
                );
            }
        }
        "dots" | "baitball" | "fizz" | "fish" => {
            for i in 0..12 {
                let angle = i as f32 * 2.4 + phase * (0.5 + level);
                let (x, y, size) = match hud.look.as_str() {
                    "dots" => (
                        i as f32 * 7.5,
                        13.,
                        3. + level * ((i as f32 + phase).sin() + 1.) * 2.,
                    ),
                    "baitball" => (
                        47. + angle.cos() * (11. + i as f32 * 1.7),
                        13. + angle.sin() * (4. + level * 7.),
                        2. + level * 2.,
                    ),
                    "fish" => (
                        9. + i as f32 * 6.8,
                        13. + (phase * 2. + i as f32 * 0.7).sin() * (2. + level * 8.),
                        3. + (i as f32 / 12.) * 2.,
                    ),
                    _ => (
                        i as f32 * 7.5,
                        3. + ((phase * 0.8 + i as f32 * 0.19).fract()) * 20.,
                        2. + level * 3.,
                    ),
                };
                canvas = canvas.child(
                    el().absolute(x - size / 2., y - size / 2.)
                        .size(size, size)
                        .radius(size / 2.)
                        .fill(p.accent.alpha(0.5 + i as f32 / 24.)),
                );
            }
        }
        _ => {
            let mut bars = row()
                .size(94., 26.)
                .center_y()
                .gap(if hud.look == "ribbon" { 0. } else { 2. });
            for i in 0..16 {
                let sample = hud
                    .levels
                    .get(hud.levels.len().saturating_sub(16) + i)
                    .copied()
                    .unwrap_or(0.)
                    .sqrt()
                    .clamp(0., 1.);
                let envelope = 1. - ((i as f32 - 7.5) / 8.).abs();
                let height = if hud.look == "wave" {
                    3. + sample * envelope * 22.
                } else {
                    3. + sample * 22.
                };
                let bar = el()
                    .w(if hud.look == "ribbon" { 5.8 } else { 3.8 })
                    .h(height)
                    .radius(if hud.look == "ribbon" { 1. } else { 2. })
                    .fill(p.accent);
                bars = bars.child(if hud.look == "ribbon" {
                    bar.gradient(p.accent, p.accent_soft)
                } else {
                    bar
                });
            }
            canvas = canvas.child(bars);
        }
    }
    canvas
}

pub fn open_path(path: &str) -> Result<(), String> {
    if !Path::new(path).exists() {
        return Err("This file is no longer available.".into());
    }
    #[cfg(target_os = "windows")]
    let status = Command::new("explorer.exe").arg(path).spawn();
    #[cfg(target_os = "macos")]
    let status = Command::new("open").arg(path).spawn();
    #[cfg(not(any(target_os = "windows", target_os = "macos")))]
    let status = Command::new("xdg-open").arg(path).spawn();
    status
        .map(|_| ())
        .map_err(|e| format!("Could not open the file: {e}"))
}

thread_local! {
    static AUDIO: RefCell<Option<(rodio::OutputStream, rodio::Sink)>> = const { RefCell::new(None) };
}

pub fn play_audio(path: &str) -> Result<(), String> {
    let file = File::open(path).map_err(|e| format!("Could not read this recording: {e}"))?;
    let audio = rodio::Decoder::new(BufReader::new(file))
        .map_err(|e| format!("Could not decode this recording: {e}"))?;
    let (stream, handle) = rodio::OutputStream::try_default()
        .map_err(|e| format!("Could not open audio output: {e}"))?;
    let sink =
        rodio::Sink::try_new(&handle).map_err(|e| format!("Could not start playback: {e}"))?;
    sink.append(audio);
    AUDIO.with_borrow_mut(|current| {
        if let Some((_, old)) = current.take() {
            old.stop();
        }
        *current = Some((stream, sink));
    });
    Ok(())
}

pub fn stop_audio() {
    AUDIO.with_borrow_mut(|current| {
        if let Some((_, sink)) = current.take() {
            sink.stop();
        }
    });
}

pub fn audio_playing() -> bool {
    AUDIO.with_borrow(|current| current.as_ref().is_some_and(|(_, sink)| !sink.empty()))
}
