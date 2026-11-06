use crate::{
    pages,
    state::{Entry, Page, State},
};
use rune::{Color, Cx, El, Icon, ScrollSpec, TextStyle, Tx, col, el, icon, row, text};
use rune_kit::{Button, ButtonVariant, ControlSize, Kit, PaletteSeed, glyphs};
use serde_json::json;
use std::cell::Cell;
mod hotkeys;
pub use hotkeys::suppress_hotkey;

thread_local! {static TYPE_SCALE:Cell<f32>=const {Cell::new(1.)};}
fn type_scale() -> f32 {
    TYPE_SCALE.with(Cell::get)
}

pub struct Palette {
    pub bg: Color,
    pub sidebar: Color,
    pub surface: Color,
    pub soft: Color,
    pub ink: Color,
    pub muted: Color,
    pub faint: Color,
    pub border: Color,
    pub accent: Color,
    pub accent_soft: Color,
    pub on_accent: Color,
    pub danger: Color,
    pub kit: Kit,
}

fn hex(value: u32) -> Color {
    let decode = |shift: u32| rune::color::srgb_decode(((value >> shift) & 255_u32) as f32 / 255.);
    Color {
        r: decode(16),
        g: decode(8),
        b: decode(0),
        a: 1.,
    }
}
impl Palette {
    pub fn new(dark: bool) -> Self {
        let (bg, sidebar, surface, soft, ink, muted, faint, border, accent, accent_soft, on_accent) =
            if dark {
                (
                    0x171c19, 0x121713, 0x1e2520, 0x242e27, 0xe7eee7, 0xa5b4a8, 0x819086, 0x344137,
                    0xa7d5ae, 0x2c4032, 0x183522,
                )
            } else {
                (
                    0xf8f9f6, 0xf0f3ed, 0xffffff, 0xf5f7f1, 0x253b2e, 0x69796c, 0x778479, 0xe1e7dd,
                    0x315e42, 0xe6efdf, 0xffffff,
                )
            };
        let mut kit = Kit::from_palette(PaletteSeed {
            bg: hex(surface),
            fg: hex(ink),
            accent: hex(accent),
        });
        kit.radius.sm = 7.;
        let scale = type_scale();
        for style in [
            &mut kit.ty.ui,
            &mut kit.ty.ui_medium,
            &mut kit.ty.ui_compact,
            &mut kit.ty.small,
            &mut kit.ty.small_medium,
            &mut kit.ty.body,
            &mut kit.ty.body_medium,
            &mut kit.ty.text_small,
            &mut kit.ty.text_small_medium,
            &mut kit.ty.text_xsmall,
            &mut kit.ty.text_xsmall_medium,
            &mut kit.ty.mono,
        ] {
            style.size *= scale;
            style.line_height *= scale;
        }
        Self {
            bg: hex(bg),
            sidebar: hex(sidebar),
            surface: hex(surface),
            soft: hex(soft),
            ink: hex(ink),
            muted: hex(muted),
            faint: hex(faint),
            border: hex(border),
            accent: hex(accent),
            accent_soft: hex(accent_soft),
            on_accent: hex(on_accent),
            danger: hex(if dark { 0xf1a6a1 } else { 0xa63b35 }),
            kit,
        }
    }
}

pub static HISTORY: Icon = Icon {
    name: "ow-history",
    view: 24.,
    body: r#"<path d="M3 11a9 9 0 1 1 3 7M3 4v7h7M12 7v5l3 2" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>"#,
};
pub static MEETING: Icon = Icon {
    name: "ow-meeting",
    view: 24.,
    body: r#"<rect x="3" y="5" width="12" height="14" rx="3" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="m15 10 6-3v10l-6-3" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/>"#,
};
pub static WALK: Icon = Icon {
    name: "ow-walk",
    view: 24.,
    body: r#"<rect x="3" y="4" width="18" height="16" rx="3" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="m10 9 5 3-5 3ZM3 8h18" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/>"#,
};
pub static SETTINGS: Icon = Icon {
    name: "ow-settings",
    view: 24.,
    body: r#"<path d="M4 6h16M4 12h16M4 18h16" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/><circle cx="9" cy="6" r="2" fill="currentColor"/><circle cx="15" cy="12" r="2" fill="currentColor"/><circle cx="9" cy="18" r="2" fill="currentColor"/>"#,
};
pub static UPLOAD: Icon = Icon {
    name: "ow-upload",
    view: 24.,
    body: r#"<path d="M12 16V3m-5 5 5-5 5 5M4 16v3a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-3" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>"#,
};
pub static SUN: Icon = Icon {
    name: "ow-sun",
    view: 24.,
    body: r#"<circle cx="12" cy="12" r="4" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1.5 1.5m11 11L19 19M5 19l1.5-1.5m11-11L19 5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>"#,
};
pub static SHIELD: Icon = Icon {
    name: "ow-shield",
    view: 24.,
    body: r#"<path d="M12 3 4 6v6c0 5 8 9 8 9s8-4 8-9V6ZM8 12l3 3 5-6" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>"#,
};

pub fn label(value: impl Into<String>, size: f32, weight: f32, color: Color) -> El {
    text(value)
        .font(TextStyle::new(size * type_scale(), weight))
        .color(color)
        .wrap_anywhere()
        .min_w(0.)
}
pub fn eyebrow(value: &str, p: &Palette) -> El {
    text(value)
        .font(TextStyle::new(10., 600.).tracking(1.4))
        .color(p.muted)
}
pub fn divider(p: &Palette) -> El {
    el().w_full().h(1.).fill(p.border)
}
pub fn card(p: &Palette) -> El {
    col()
        .radius(14.)
        .fill(p.surface)
        .border(1., p.border)
        .padding(22.)
        .gap(16.)
}
pub fn pill(value: &str, p: &Palette) -> El {
    row()
        .px(9.)
        .py(4.)
        .radius(5.)
        .fill(p.accent_soft)
        .child(label(value, 10., 500., p.accent))
}
pub fn btn(
    key: impl Into<String>,
    title: &str,
    p: &Palette,
    primary: bool,
    enabled: bool,
    action: impl Fn(&mut State, &mut Tx) + 'static,
) -> El {
    let button = Button::new(title)
        .variant(if primary {
            ButtonVariant::Contrast
        } else {
            ButtonVariant::Outline
        })
        .control_size(ControlSize::Large)
        .disabled(!enabled)
        .on_press_tx(action)
        .build(&p.kit)
        .key(key)
        .h(39.)
        .px(16.)
        .radius(7.);
    if primary {
        button.fill(p.accent).color(p.on_accent)
    } else {
        button
    }
}
pub fn link(
    key: impl Into<String>,
    title: &str,
    p: &Palette,
    action: impl Fn(&mut State, &mut Tx) + 'static,
) -> El {
    Button::new(title)
        .variant(ButtonVariant::Ghost)
        .on_press_tx(action)
        .build(&p.kit)
        .key(key)
        .color(p.accent)
}
pub fn page_header(title: &str, subtitle: &str, p: &Palette) -> El {
    col()
        .gap(9.)
        .child(label(title, 31., 500., p.ink))
        .child(label(subtitle, 13., 400., p.muted))
}
pub fn page_icon(page: Page) -> &'static Icon {
    match page {
        Page::Record => &glyphs::MIC,
        Page::Files => &UPLOAD,
        Page::Meetings => &MEETING,
        Page::Walkthrough => &WALK,
        Page::History => &HISTORY,
        Page::Settings => &SETTINGS,
        Page::Scratchpad => &glyphs::CURSOR_TEXT,
        Page::Screenshot => &glyphs::FILE_IMAGE,
    }
}
fn logo(p: &Palette) -> El {
    row()
        .gap(3.)
        .center()
        .size(31., 31.)
        .radius(9.)
        .fill(p.accent)
        .children([9., 17., 23., 13.].into_iter().enumerate().map(|(i, h)| {
            el().key(format!("logo-{i}"))
                .size(2.6, h)
                .radius(1.3)
                .fill(p.on_accent)
        }))
}
fn nav(s: &State, page: Page, p: &Palette, compact: bool) -> El {
    let active = s.page == page;
    let node = row()
        .key(format!("nav-{}", page.key()))
        .role("button")
        .label(page.name())
        .focusable(true)
        .focus_ring(2., 2., p.accent)
        .h(42.)
        .px(13.)
        .gap(12.)
        .center_y()
        .radius(8.)
        .fill(if active { p.accent_soft } else { p.sidebar })
        .hover(p.border.alpha(0.5))
        .on_press(move |s: &mut State| {
            s.navigate(page);
            s.notice = None;
        })
        .child(icon(page_icon(page), 18.).color(if active { p.accent } else { p.muted }));
    if compact {
        node.help(page.name())
    } else {
        node.child(label(
            page.name(),
            12.5,
            if active { 600. } else { 450. },
            if active { p.accent } else { p.muted },
        ))
    }
}
fn sidebar(s: &State, p: &Palette, compact: bool) -> El {
    let brand = row().gap(10.).center_y().child(logo(p));
    let brand = if compact {
        brand
    } else {
        brand.child(label("OpenWhisper", 16., 650., p.ink))
    };
    let mut top = col()
        .gap(5.)
        .child(nav(s, Page::Record, p, compact))
        .child(nav(s, Page::Files, p, compact))
        .child(nav(s, Page::Meetings, p, compact))
        .child(nav(s, Page::Walkthrough, p, compact));
    if !compact {
        top = top.child(el().h(14.)).child(eyebrow("LIBRARY", p));
    }
    top = top
        .child(nav(s, Page::History, p, compact))
        .child(nav(s, Page::Scratchpad, p, compact));
    let bottom = col().gap(9.).child(nav(s, Page::Settings, p, compact));
    let bottom = if compact {
        bottom
    } else {
        bottom.child(divider(p)).child(
            row()
                .gap(9.)
                .center_y()
                .py(8.)
                .child(
                    row()
                        .size(30., 30.)
                        .radius(15.)
                        .fill(p.accent_soft)
                        .center()
                        .child(icon(&SHIELD, 16.).color(p.accent)),
                )
                .child(
                    col()
                        .gap(3.)
                        .child(label("Your workspace", 11.5, 550., p.ink))
                        .child(label("On this computer", 10., 400., p.muted)),
                ),
        )
    };
    col()
        .key("sidebar")
        .w(if compact { 72. } else { 216. })
        .h_full()
        .padding(if compact { 14. } else { 18. })
        .pt(if cfg!(target_os = "macos") { 42. } else { 29. })
        .gap(37.)
        .fill(p.sidebar)
        .child(brand)
        .child(top)
        .child(el().grow(1.))
        .child(bottom)
}
pub fn shell(s: &State, cx: &mut Cx<State>) -> El {
    let scale = s
        .data
        .pointer("/settings.values/values/ui_font_scale")
        .or_else(|| s.snapshot.settings.get("ui_font_scale"))
        .and_then(|v| v.as_f64())
        .unwrap_or(100.);
    TYPE_SCALE.with(|value| value.set((scale as f32 / 100.).clamp(0.9, 1.5)));
    let p = Palette::new(s.dark);
    let size = cx.layout_size("app").unwrap_or([1220., 860.]);
    let compact = size[0] < 1040.;
    let toolbar = row()
        .key("toolbar")
        .h(65.)
        .px(if compact { 24. } else { 36. })
        .gap(9.)
        .center_y()
        .window_drag()
        .child(label("Workspace", 11., 450., p.muted))
        .child(label("/", 11., 400., p.faint))
        .child(label(s.page.name(), 11., 550., p.ink))
        .child(el().grow(1.))
        .child(if s.demo { pill("PREVIEW", &p) } else { el() })
        .child(
            Button::new(if s.dark { "Light mode" } else { "Dark mode" })
                .icon(&SUN)
                .variant(ButtonVariant::Ghost)
                .on_press(|s: &mut State| s.set_dark(!s.dark))
                .build(&p.kit)
                .key("theme-toggle"),
        )
        .child(
            Button::new("Search")
                .icon(&glyphs::SEARCH)
                .variant(ButtonVariant::Ghost)
                .on_press(|s: &mut State| s.navigate(Page::History))
                .build(&p.kit)
                .key("global-search")
                .help("Search history · Ctrl/Cmd+K"),
        );
    let mut layer = Vec::new();
    let content = match s.page {
        Page::Record => pages::studio(s, &p, compact),
        Page::Files => pages::files(s, &p),
        Page::Meetings => pages::meetings(s, &p),
        Page::Walkthrough => pages::walkthrough(s, &p),
        Page::History => pages::history(s, &p, compact),
        Page::Settings => pages::settings(s, &p, &mut layer),
        Page::Scratchpad => pages::scratchpad(s, &p),
        Page::Screenshot => pages::screenshot(s, &p),
    };
    let mut body = col()
        .key("page-scroll")
        .grow(1.)
        .min_h(0.)
        .scroll(ScrollSpec {
            stick_bottom: false,
        });
    if let Some(error) = &s.error {
        body = body.child(
            row()
                .key("error")
                .mx(24.)
                .padding(14.)
                .radius(8.)
                .fill(p.danger.alpha(0.12))
                .gap(12.)
                .child(label(error, 12., 450., p.danger).grow(1.))
                .child(link("dismiss-error", "Dismiss", &p, |s, _| s.error = None)),
        );
    }
    if let Some(notice) = &s.notice {
        body = body.child(
            row()
                .key("notice")
                .mx(24.)
                .padding(12.)
                .radius(8.)
                .fill(p.accent_soft)
                .child(label(notice, 12., 450., p.accent).grow(1.))
                .child(link("dismiss-notice", "Dismiss", &p, |s, _| {
                    s.notice = None
                })),
        );
    }
    body = body.child(
        col()
            .key(format!("page-{}", s.page.key()))
            .padding(if compact { 24. } else { 36. })
            .pt(23.)
            .gap(27.)
            .child(content),
    );
    let status = if s.demo {
        "Preview workspace"
    } else if s.snapshot.recording {
        "Recording in progress"
    } else if s.snapshot.meeting {
        "Meeting in progress"
    } else if s.snapshot.busy {
        "Transcribing your audio"
    } else if s.available() {
        if s.snapshot.status.is_empty() {
            "Audio service connected"
        } else {
            &s.snapshot.status
        }
    } else {
        "Connecting to audio service"
    };
    let footer = row()
        .h(35.)
        .px(if compact { 24. } else { 36. })
        .gap(7.)
        .center_y()
        .child(el().size(5., 5.).radius(3.).fill(if s.snapshot.recording {
            p.danger
        } else {
            p.accent
        }))
        .child(
            label(status, 10., 400., p.muted)
                .grow(1.)
                .max_lines(1)
                .help(status),
        )
        .child(label("Made for your train of thought.", 10., 400., p.faint));
    let shell = row()
        .key("app")
        .w_full()
        .h_full()
        .fill(p.bg)
        .child(sidebar(s, &p, compact))
        .child(
            col()
                .grow(1.)
                .min_w(0.)
                .h_full()
                .child(toolbar)
                .child(divider(&p))
                .child(body)
                .child(footer),
        );
    if let Some(prompt) = crate::dialogs::prompt(s, &p) {
        layer.push(prompt);
    }
    if let Some(quit) = crate::platform::quit_confirmation(s, &p) {
        layer.push(quit);
    }
    if let Some(permission) = crate::platform::permission_prompt(s, &p) {
        layer.push(permission);
    }
    el().w_full().h_full().child(shell).children(layer)
}

pub fn duration(seconds: f64) -> String {
    let total = seconds as u64;
    format!("{}:{:02}", total / 60, total % 60)
}
pub fn engine(value: &str) -> &str {
    match value {
        "parakeet" => "Parakeet",
        "api" => "Speech API",
        "local_whisper" => "Local Whisper",
        "openai" => "OpenAI",
        "remote" => "Remote engine",
        "" => "Loading…",
        other => other,
    }
}
pub fn entry_row(entry: &Entry, p: &Palette, selected: bool, small: bool) -> El {
    let id = entry.id.clone();
    row()
        .key(format!("entry-{id}"))
        .role("button")
        .label(&entry.title)
        .focusable(true)
        .focus_ring(2., 1., p.accent)
        .padding(14.)
        .gap(13.)
        .center_y()
        .radius(8.)
        .fill(if selected { p.accent_soft } else { p.surface })
        .hover(p.soft)
        .on_press_tx(move |s: &mut State, tx| {
            s.navigate(Page::History);
            s.selected = Some(id.clone());
            s.detail.clear();
            s.detail_raw.clear();
            s.show_raw = false;
            s.command("history.detail", json!({"id":id}));
            tx.scroll_key_to("history-detail", 0.);
        })
        .child(
            row().size(34., 34.).center().radius(8.).fill(p.soft).child(
                icon(
                    if entry.kind == "file" {
                        &glyphs::FILE
                    } else {
                        &glyphs::MIC
                    },
                    16.,
                )
                .color(p.muted),
            ),
        )
        .child(
            col()
                .grow(1.)
                .min_w(0.)
                .gap(5.)
                .child(label(&entry.title, 12., 550., p.ink).max_lines(1))
                .child(
                    label(
                        if small {
                            format!("{} words  ·  {}", entry.words, duration(entry.duration))
                        } else {
                            entry.preview.clone()
                        },
                        11.,
                        400.,
                        p.muted,
                    )
                    .max_lines(1),
                ),
        )
        .child(label(duration(entry.duration), 10., 450., p.muted))
}
