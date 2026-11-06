use crate::bridge::Client;
use rune::TextEdit;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::time::Instant;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Page {
    Record,
    Files,
    Meetings,
    Walkthrough,
    History,
    Settings,
}

impl Page {
    pub fn name(self) -> &'static str {
        match self {
            Self::Record => "Quick record",
            Self::Files => "Audio files",
            Self::Meetings => "Meetings",
            Self::Walkthrough => "Walkthroughs",
            Self::History => "History",
            Self::Settings => "Settings",
        }
    }
    pub fn key(self) -> &'static str {
        match self {
            Self::Record => "record",
            Self::Files => "files",
            Self::Meetings => "meetings",
            Self::Walkthrough => "walkthrough",
            Self::History => "history",
            Self::Settings => "settings",
        }
    }
}

#[derive(Clone, Default, Deserialize, Serialize, PartialEq)]
#[serde(default)]
pub struct Entry {
    pub id: String,
    pub title: String,
    pub preview: String,
    pub timestamp: String,
    pub duration: f64,
    pub kind: String,
    pub words: usize,
    pub model: String,
}

#[derive(Clone, Deserialize, Serialize, PartialEq)]
#[serde(default)]
pub struct Snapshot {
    pub recording: bool,
    pub busy: bool,
    pub meeting: bool,
    pub ready: bool,
    pub version: String,
    pub status: String,
    pub transcript: String,
    pub raw: String,
    pub mode: String,
    pub levels: Vec<f32>,
    pub preview: String,
    pub history: Vec<Entry>,
    pub engine: String,
    pub settings: Value,
}

impl Default for Snapshot {
    fn default() -> Self {
        Self {
            recording: false,
            busy: false,
            meeting: false,
            ready: false,
            version: String::new(),
            mode: String::new(),
            status: "Connecting to OpenWhisper…".into(),
            transcript: String::new(),
            raw: String::new(),
            history: vec![],
            engine: String::new(),
            levels: vec![],
            preview: String::new(),
            settings: json!({"auto_paste":true,"copy_clipboard":true,"transcript_cleanup_enabled":false,"local_asr_language":"en"}),
        }
    }
}

pub struct State {
    pub page: Page,
    pub dark: bool,
    pub demo: bool,
    pub snapshot: Snapshot,
    pub client: Option<Client>,
    pub error: Option<String>,
    pub notice: Option<String>,
    pub search: TextEdit,
    pub filter: &'static str,
    pub selected: Option<String>,
    pub detail: String,
    pub detail_raw: String,
    pub detail_title: String,
    pub show_raw: bool,
    pub file: Option<String>,
    pub pending: bool,
    pub recording_since: Option<Instant>,
    pub theme_loaded: bool,
}

impl State {
    pub fn new() -> Self {
        let mut state = Self {
            page: Page::Record,
            dark: false,
            demo: std::env::args().any(|a| a == "--demo"),
            snapshot: Snapshot::default(),
            client: None,
            error: None,
            notice: None,
            search: TextEdit::new(""),
            filter: "all",
            selected: None,
            detail: String::new(),
            detail_raw: String::new(),
            detail_title: String::new(),
            show_raw: false,
            file: None,
            pending: false,
            recording_since: None,
            theme_loaded: false,
        };
        if state.demo {
            demo(&mut state);
        }
        state
    }
    pub fn available(&self) -> bool {
        self.demo || (self.client.is_some() && self.snapshot.ready)
    }
    pub fn working(&self) -> bool {
        self.pending || self.snapshot.recording || self.snapshot.busy || self.snapshot.meeting
    }
    pub fn setting(&self, key: &str) -> bool {
        self.snapshot.settings[key].as_bool().unwrap_or(false)
    }
    pub fn set_dark(&mut self, dark: bool) {
        self.dark = dark;
        self.theme_loaded = true;
        if self.demo {
            return;
        }
        if let Some(client) = &self.client {
            if let Err(error) = client.send(
                "appearance.update",
                json!({"theme":if dark {"dark"} else {"light"}}),
            ) {
                self.error = Some(error);
            }
        }
    }
    pub fn command(&mut self, method: &str, params: Value) {
        self.error = None;
        self.notice = None;
        if self.demo {
            self.demo_command(method, params);
            return;
        }
        if let Some(client) = &self.client {
            self.pending = true;
            if client.send(method, params).is_err() {
                self.pending = false;
                self.error = Some("The audio service disconnected. Close this window and launch OpenWhisper again.".into());
            }
        } else {
            self.error = Some("The audio service is still starting. Try again in a moment.".into());
        }
    }
    pub fn toggle_record(&mut self) {
        if self.snapshot.recording && self.snapshot.mode == "walkthrough" {
            self.command("walkthrough.toggle", json!({}));
            return;
        }
        self.command(
            if self.snapshot.recording {
                "record.stop"
            } else {
                "record.start"
            },
            json!({}),
        );
    }
    pub fn choose_file(&mut self, path: String) {
        self.file = Some(path);
        self.page = Page::Files;
        self.notice = None;
    }
    pub fn receive(&mut self, method: &str, response: Value) -> bool {
        if method != "snapshot" {
            self.pending = false;
        }
        if let Some(error) = response.get("error").and_then(Value::as_str) {
            if self.error.as_deref() == Some(error) {
                return false;
            }
            self.error = Some(error.into());
            return true;
        }
        let result = &response["result"];
        if method == "snapshot" {
            match serde_json::from_value::<Snapshot>(result.clone()) {
                Ok(next) => {
                    if !self.theme_loaded {
                        self.dark = next.settings["ui_theme"].as_str() == Some("dark");
                        self.theme_loaded = true;
                    }
                    if next.recording && !self.snapshot.recording {
                        self.recording_since = Some(Instant::now());
                    }
                    if !next.recording {
                        self.recording_since = None;
                    }
                    let changed = next != self.snapshot || next.recording;
                    self.snapshot = next;
                    return changed;
                }
                Err(_) => {
                    self.error = Some("The audio service returned an unreadable update.".into());
                }
            }
        } else if method == "history.detail" {
            // A slower response may arrive after the user has selected another entry.
            if self.selected.as_deref() == result["id"].as_str() {
                self.detail = result["text"].as_str().unwrap_or("").into();
                self.detail_raw = result["raw"].as_str().unwrap_or("").into();
                self.detail_title = result["title"].as_str().unwrap_or("Dictation").into();
            }
        } else if method == "settings.update" {
            self.notice = Some("Preference saved".into());
        }
        true
    }
    fn demo_command(&mut self, method: &str, params: Value) {
        match method {
            "record.start" => {
                self.snapshot.recording = true;
                self.recording_since = Some(Instant::now());
                self.snapshot.transcript.clear();
            }
            "record.stop" => {
                self.snapshot.recording = false;
                self.recording_since = None;
                self.snapshot.transcript = "A small idea, captured before it disappears. Turn your thoughts into something you can work with.".into();
                self.snapshot.raw = self.snapshot.transcript.clone();
            }
            "cancel" => {
                self.snapshot.recording = false;
                self.snapshot.busy = false;
            }
            "settings.update" => {
                if let Some(key) = params["key"].as_str() {
                    self.snapshot.settings[key] = params["value"].clone();
                }
            }
            "history.detail" => {
                self.detail_title = "A thought for the next release".into();
                self.detail = "Let's make the everyday things feel effortless. A quieter workspace, fewer clicks, and more room to think.\n\nStart with the recording experience. Keep the important controls close, and let everything else get out of the way.".into();
                self.detail_raw = self.detail.clone();
            }
            _ => {
                self.notice =
                    Some("Preview mode · launch without --demo to use your audio services".into())
            }
        }
    }
}

fn demo(s: &mut State) {
    s.demo = true;
    s.snapshot.ready = true;
    s.snapshot.engine = "parakeet".into();
    s.snapshot.version = "Rune preview".into();
    s.snapshot.status = "Ready when you are".into();
    s.snapshot.history = [
        ("A thought for the next release", "Let's make the everyday things feel effortless. A quieter workspace, fewer clicks, and more room to think.", "dictation", 86, 32.0),
        ("Design review notes.wav", "A more focused recording experience, with the important controls always close at hand.", "file", 324, 148.0),
        ("A quick reminder", "Revisit the onboarding flow and simplify the first three steps before Friday.", "dictation", 42, 18.0),
    ].iter().enumerate().map(|(i,(title,preview,kind,words,duration))| Entry { id: format!("demo-{i}"), title: title.to_string(), preview: preview.to_string(), timestamp: "2026-11-06T09:41:00".into(), kind:kind.to_string(), words:*words,duration:*duration,model:"Parakeet".into() }).collect();
}
pub static SCENES: &[(&str, fn(&mut State))] = &[
    ("studio", demo),
    ("dark", |s| {
        demo(s);
        s.dark = true;
    }),
    ("recording", |s| {
        demo(s);
        s.snapshot.recording = true;
    }),
    ("transcript", |s| {
        demo(s);
        s.demo_command("record.stop", json!({}));
    }),
    ("files", |s| {
        demo(s);
        s.page = Page::Files;
    }),
    ("meetings", |s| {
        demo(s);
        s.page = Page::Meetings;
    }),
    ("walkthrough", |s| {
        demo(s);
        s.page = Page::Walkthrough;
    }),
    ("history", |s| {
        demo(s);
        s.page = Page::History;
        s.selected = Some("demo-0".into());
        s.demo_command("history.detail", json!({}));
    }),
    ("settings", |s| {
        demo(s);
        s.page = Page::Settings;
    }),
];
