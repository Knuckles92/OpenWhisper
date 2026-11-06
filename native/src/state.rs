use crate::bridge::Client;
use rune::TextEdit;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::collections::{HashMap, VecDeque};
use std::time::Instant;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Page {
    Record,
    Files,
    Meetings,
    Walkthrough,
    History,
    Settings,
    Scratchpad,
    Screenshot,
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
            Self::Scratchpad => "Scratchpad",
            Self::Screenshot => "Screenshot",
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
            Self::Scratchpad => "scratchpad",
            Self::Screenshot => "screenshot",
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
    pub window_active: bool,
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
    pub data: Value,
    pub edits: HashMap<String, TextEdit>,
    pub selected_tab: String,
    pub selected_item: Option<String>,
    pub confirmation: Option<Value>,
    pub library_page: usize,
    pub file_queue: Vec<String>,
    pub open_select: Option<String>,
    pub image_cache: Option<rune::ImageCache>,
    pub images: HashMap<String, rune::Image>,
    image_order: VecDeque<String>,
    pub desktop: Option<crate::platform::Desktop>,
    refresh_at: HashMap<String, Instant>,
    last_params: HashMap<String, Value>,
}

impl State {
    pub fn new() -> Self {
        let mut state = Self {
            page: Page::Record,
            dark: false,
            demo: std::env::args().any(|a| a == "--demo"),
            window_active: false,
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
            data: json!({}),
            edits: HashMap::new(),
            selected_tab: String::new(),
            selected_item: None,
            confirmation: None,
            library_page: 0,
            file_queue: vec![],
            open_select: None,
            image_cache: None,
            images: HashMap::new(),
            image_order: VecDeque::new(),
            desktop: None,
            refresh_at: HashMap::new(),
            last_params: HashMap::new(),
        };
        if state.demo {
            demo(&mut state);
        }
        state
    }
    pub fn available(&self) -> bool {
        self.demo || (self.client.is_some() && self.snapshot.ready)
    }
    pub fn set_window_active(&mut self, active: bool) {
        self.window_active = active;
        self.report_window_focus();
    }
    pub fn report_window_focus(&self) {
        if self.demo {
            return;
        }
        if let Some(client) = &self.client {
            let _ = client.send("window.state", json!({"active":self.window_active}));
            let mut scratchpad =
                json!({"active":self.window_active && self.page == Page::Scratchpad});
            if self.page == Page::Scratchpad && self.data["scratchpad.loaded"] == true {
                if let Some(edit) = self.edits.get("scratchpad") {
                    scratchpad["text"] = json!(edit.text);
                }
            }
            let _ = client.send("scratchpad.focus", scratchpad);
        }
    }
    pub fn edit(&mut self, key: &str) -> &mut TextEdit {
        self.edits
            .entry(key.into())
            .or_insert_with(|| TextEdit::new(""))
    }
    pub fn edit_seeded(&mut self, key: &str, value: &str) -> &mut TextEdit {
        self.edits
            .entry(key.into())
            .or_insert_with(|| TextEdit::new(value))
    }
    fn refresh(&mut self, method: &str) {
        if self
            .refresh_at
            .get(method)
            .is_some_and(|last| last.elapsed() < std::time::Duration::from_secs(1))
        {
            return;
        }
        self.refresh_at.insert(method.into(), Instant::now());
        self.command(method, json!({}));
    }
    pub fn navigate(&mut self, page: Page) {
        if self.page == Page::Screenshot && page != Page::Screenshot {
            self.data["screenshot.next_page"] = json!(page.key());
            crate::pages::finish_screenshot(self, true);
            return;
        }
        if self.page == Page::Scratchpad && page != Page::Scratchpad {
            let mut focus = json!({"active":false});
            if self.data["scratchpad.loaded"] == true {
                if let Some(edit) = self.edits.get("scratchpad") {
                    focus["text"] = json!(edit.text);
                }
            }
            self.command("scratchpad.focus", focus);
        }
        if self.page != page {
            self.selected_item = None;
            self.selected_tab.clear();
            self.confirmation = None;
        }
        self.page = page;
        self.notice = None;
        self.open_select = None;
        match page {
            Page::Settings => for method in ["settings.schema","settings.values","models.list","devices.list","collections.schema"] { self.command(method,json!({})); },
            Page::History => self.command("history.list",json!({"query":self.search.text,"kind":self.filter,"offset":self.library_page,"limit":50})),
            Page::Meetings => self.command("meetings.list",json!({})),
            Page::Walkthrough => self.command("walkthroughs.list",json!({})),
            Page::Files=>self.command("file.queue",json!({})),
            Page::Scratchpad=>{self.command("scratchpad.get",json!({}));self.command("scratchpad.focus",json!({"active":self.window_active}));},
            _=>{},
        }
    }
    pub fn load_image(&mut self, path: &str) {
        self.image_order.retain(|existing| existing != path);
        if self.images.contains_key(path) {
            self.image_order.push_back(path.into());
            return;
        }
        if let Some(cache) = &self.image_cache {
            while self.images.len() >= 32 {
                let Some(oldest) = self.image_order.pop_front() else {
                    break;
                };
                self.images.remove(&oldest);
            }
            self.images.insert(path.into(), cache.file(path, 800., 1.));
            self.image_order.push_back(path.into());
        }
    }
    pub fn receive_event(&mut self, event: &str, data: Value, tx: &mut rune::Tx) -> bool {
        self.data["events"][event] = data.clone();
        match event {
            "prompt" => {
                self.data["prompt"] = data.clone();
                self.data["dismissed_prompt"] = Value::Null;
            }
            "prompt.closed" => {
                if self.data["prompt"]["id"] == data["id"] {
                    self.data["prompt"] = Value::Null;
                }
            }
            "screenshot.choose" => {
                crate::pages::begin_screenshot(self, &data);
            }
            "screenshot.keep" => {
                crate::pages::finish_screenshot(self, false);
            }
            "scratchpad.insert" => {
                self.edits.entry("scratchpad".into()).or_default().text =
                    data["content"].as_str().unwrap_or("").into();
                self.data["scratchpad.loaded"] = json!(true);
            }
            "status" => self.snapshot.status = data["text"].as_str().unwrap_or("").into(),
            "notice" => self.notice = data["text"].as_str().map(str::to_owned),
            "error" | "fatal" => self.error = data["message"].as_str().map(str::to_owned),
            "audio.level" => {
                self.snapshot
                    .levels
                    .push(data["level"].as_f64().unwrap_or(0.) as f32);
                if self.snapshot.levels.len() > 49 {
                    self.snapshot.levels.remove(0);
                }
            }
            "transcription.preview" => {
                self.snapshot.preview = data["text"].as_str().unwrap_or("").into()
            }
            "transcription.result" => {
                self.snapshot.transcript = data["text"].as_str().unwrap_or("").into();
                self.snapshot.raw = data["raw"].as_str().unwrap_or("").into();
                self.snapshot.preview.clear();
            }
            "models.changed" | "models.download" => {
                self.refresh("models.list");
            }
            "components.changed" | "components.finished" => {
                self.refresh("components.list");
                self.refresh("models.list");
            }
            "meetings.changed" => {
                if self.page == Page::Meetings {
                    self.command("meetings.list", json!({}));
                }
            }
            "history.changed" => {
                if self.page == Page::History {
                    self.navigate(Page::History);
                }
            }
            "lifecycle" if data["state"] == "ready" => {
                self.command("settings.values", json!({}));
                self.report_window_focus();
            }
            "lifecycle" if data["state"] == "restart_required" => {
                self.snapshot.ready = false;
                self.theme_loaded = false;
                self.notice =
                    Some("Restarting the audio service to finish restoring your data…".into());
            }
            "lifecycle" if data["state"] == "already_running" => {
                tx.quit();
            }
            "system.operation" => {
                self.refresh("operations.list");
                self.refresh("remote.state");
                self.refresh("backup.state");
            }
            "collections.test.finished" => self.refresh("collections.tests"),
            _ => {}
        }
        for (prefix, method) in [
            ("remote.", "remote.state"),
            ("backup.", "backup.state"),
            ("updates.", "updates.state"),
            ("update.", "updates.state"),
            ("stats.", "stats.get"),
        ] {
            if event.starts_with(prefix) {
                self.refresh(method);
            }
        }
        crate::platform::event(self, event, &data, tx);
        crate::platform::sync(self);
        true
    }
    pub fn answer_prompt(&mut self, value: Value) {
        let prompt = self.data["prompt"].clone();
        let id = prompt["id"].clone();
        self.data["dismissed_prompt"] = id.clone();
        self.data["prompt"] = Value::Null;
        self.command("prompt.resolve", json!({"id":id,"value":value}));
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
        if read_command(method) {
            self.last_params.insert(method.into(), params.clone());
        }
        if self.demo {
            self.demo_command(method, params);
            return;
        }
        if let Some(client) = &self.client {
            if !read_command(method) {
                self.pending = true;
            }
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
        self.navigate(Page::Files);
        self.notice = None;
    }
    pub fn receive(&mut self, method: &str, response: Value) -> bool {
        if !read_command(method) {
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
        if matches!(method, "meetings.detail" | "walkthroughs.detail")
            && self.selected_item.as_deref() != result["id"].as_str()
        {
            return false;
        }
        if method == "app.quit" && result["ok"] == true {
            crate::platform::quit_accepted(self);
            return true;
        }
        crate::pages::settings_response(self, method, result);
        crate::pages::meeting_tools_response(self, method, result);
        if method != "snapshot" {
            self.data[method] = result.clone();
        }
        if method == "snapshot" {
            match serde_json::from_value::<Snapshot>(result.clone()) {
                Ok(next) => {
                    let was_ready = self.snapshot.ready;
                    let extra_changed = self.data["snapshot"] != *result;
                    self.data["snapshot"] = result.clone();
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
                    let changed = next != self.snapshot || next.recording || extra_changed;
                    self.snapshot = next;
                    if !was_ready && self.snapshot.ready {
                        self.navigate(self.page);
                    }
                    crate::platform::sync(self);
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
        if method == "walkthroughs.detail" {
            let page = self.data["walkthrough.gallery.page"].as_u64().unwrap_or(0) as usize;
            for step in result["steps"]
                .as_array()
                .into_iter()
                .flatten()
                .skip(page * 6)
                .take(6)
            {
                if let Some(path) = step["image_path"].as_str() {
                    self.load_image(path);
                }
            }
        }
        if method == "credentials.set" {
            self.edits
                .retain(|key, _| !key.starts_with("settings:credential:"));
        }
        if method == "scratchpad.get" {
            if result["save"]["loaded"] == false {
                self.data["scratchpad.loaded"] = json!(false);
                self.error = result["save"]["error"].as_str().map(str::to_owned);
                return true;
            }
            let first_load = self.data["scratchpad.loaded"] != true;
            let edit = self.edits.entry("scratchpad".into()).or_default();
            if first_load || !edit.focused {
                edit.text = result["text"].as_str().unwrap_or("").into();
            }
            self.data["scratchpad.loaded"] = json!(true);
        }
        if method == "file.enqueue" && result["ok"] == true {
            self.file_queue.clear();
            self.file = None;
        }
        if method == "history.update" {
            self.selected_tab.clear();
            if let Some(id) = self.selected.clone() {
                self.command("history.detail", json!({"id":id}));
            }
        }
        if method == "meetings.update" {
            self.selected_tab.clear();
            if let Some(id) = self.selected_item.clone() {
                self.command("meetings.detail", json!({"id":id}));
            }
        }
        if method == "walkthroughs.review" {
            self.selected_tab = "walkthrough-steps".into();
            if let Some(id) = self.selected_item.clone() {
                self.command("walkthroughs.detail", json!({"id":id}));
            }
        }
        if method.starts_with("meetings.") && result["item"].is_object() {
            if self.selected_item.as_deref() == result["item"]["id"].as_str() {
                self.data["meetings.detail"] = result["item"].clone();
            }
            if self.data["meetings.current"]["id"].as_str() == result["item"]["id"].as_str() {
                self.data["meetings.current"] = result["item"].clone();
            }
        }
        if matches!(method, "meetings.patch" | "meetings.review") {
            let reasons: Vec<&str> = result["results"]
                .as_array()
                .into_iter()
                .flatten()
                .filter(|entry| entry["ok"] == false)
                .filter_map(|entry| entry["reason"].as_str())
                .collect();
            if !reasons.is_empty() {
                self.error = Some(reasons.join("\n"));
            } else if result["ok"] == false {
                self.error = Some(
                    result["reason"]
                        .as_str()
                        .unwrap_or("The meeting could not be updated. Refresh and try again.")
                        .into(),
                );
            }
        }
        if method.ends_with(".delete") {
            self.confirmation = None;
            if method == "history.delete" {
                self.selected = None;
            } else {
                self.selected_item = None;
            }
        }
        if method == "screenshot.complete" {
            crate::pages::screenshot_finished(self);
            let target = self.data["screenshot.next_page"]
                .as_str()
                .map(str::to_owned);
            self.data["screenshot.next_page"] = Value::Null;
            if let Some(target) = target {
                let page = match target.as_str() {
                    "files" => Page::Files,
                    "meetings" => Page::Meetings,
                    "walkthrough" => Page::Walkthrough,
                    "history" => Page::History,
                    "settings" => Page::Settings,
                    "scratchpad" => Page::Scratchpad,
                    _ => Page::Record,
                };
                self.navigate(page);
            }
        }
        if let Some(refresh) = result["refresh"].as_array() {
            for target in refresh.iter().filter_map(Value::as_str) {
                let params = if target == "history.list" {
                    json!({"query":self.search.text,"kind":self.filter,"offset":self.library_page,"limit":50})
                } else {
                    self.last_params.get(target).cloned().unwrap_or(json!({}))
                };
                self.command(target, params);
            }
        }
        true
    }
    fn demo_command(&mut self, method: &str, params: Value) {
        if crate::pages::meeting_tools_demo_command(self, method, &params) {
            return;
        }
        if crate::pages::settings_demo_command(self, method, &params) {
            return;
        }
        if crate::pages::library_demo_command(self, method, &params) {
            return;
        }
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
            "cancel" | "record.cancel" => {
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

fn read_command(method: &str) -> bool {
    method == "snapshot"
        || [
            ".list", ".detail", ".schema", ".values", ".state", ".get", ".queue",
        ]
        .iter()
        .any(|suffix| method.ends_with(suffix))
}

fn demo(s: &mut State) {
    s.demo = true;
    s.snapshot.ready = true;
    s.snapshot.engine = "parakeet".into();
    s.snapshot.version = "Rune preview".into();
    s.snapshot.status = "Ready when you are".into();
    crate::pages::settings_demo(s);
    s.snapshot.history = [
        ("A thought for the next release", "Let's make the everyday things feel effortless. A quieter workspace, fewer clicks, and more room to think.", "dictation", 86, 32.0),
        ("Design review notes.wav", "A more focused recording experience, with the important controls always close at hand.", "file", 324, 148.0),
        ("A quick reminder", "Revisit the onboarding flow and simplify the first three steps before Friday.", "dictation", 42, 18.0),
    ].iter().enumerate().map(|(i,(title,preview,kind,words,duration))| Entry { id: format!("demo-{i}"), title: title.to_string(), preview: preview.to_string(), timestamp: "2026-11-06T09:41:00".into(), kind:kind.to_string(), words:*words,duration:*duration,model:"Parakeet".into() }).collect();
    crate::pages::library_demo(s);
    crate::pages::meeting_tools_demo(s);
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
    ("settings-models", |s| {
        demo(s);
        s.page = Page::Settings;
        s.selected_tab = "settings:models".into();
    }),
    ("settings-keys", |s| {
        demo(s);
        s.page = Page::Settings;
        s.selected_tab = "settings:credentials".into();
    }),
    ("settings-remote", |s| {
        demo(s);
        s.page = Page::Settings;
        s.selected_tab = "settings:system:remote".into();
    }),
    ("settings-backup", |s| {
        demo(s);
        s.page = Page::Settings;
        s.selected_tab = "settings:system:backup".into();
    }),
    ("settings-updates", |s| {
        demo(s);
        s.page = Page::Settings;
        s.selected_tab = "settings:system:updates".into();
    }),
    ("settings-stats", |s| {
        demo(s);
        s.page = Page::Settings;
        s.selected_tab = "settings:system:stats".into();
    }),
    ("settings-setup", |s| {
        demo(s);
        s.page = Page::Settings;
        s.selected_tab = "settings:system:setup".into();
    }),
    ("settings-mcp", |s| {
        demo(s);
        s.page = Page::Settings;
        s.selected_tab = "settings:system:mcp".into();
    }),
    ("settings-dictionary", |s| {
        demo(s);
        s.page = Page::Settings;
        s.selected_tab = "settings:collection:dictionary".into();
        s.demo_command("collections.list", json!({"collection":"dictionary"}));
    }),
    ("meeting-review", |s| {
        demo(s);
        s.page = Page::Meetings;
        s.selected_item = Some("meeting-demo-1".into());
        s.demo_command("meetings.detail", json!({"id":"meeting-demo-1"}));
    }),
];
