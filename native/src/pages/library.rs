use crate::state::State;
use crate::ui::{self, Palette, btn, card, divider, eyebrow, label, link, page_header};
use rune::{El, TextStyle, col, el, icon, row, text_input};
use rune_kit::glyphs;

use super::export_transcript;

pub fn history(s: &State, p: &Palette, compact: bool) -> El {
    let search = row()
        .key("history-search-field")
        .gap(10.)
        .center_y()
        .h(43.)
        .px(13.)
        .radius(8.)
        .fill(p.surface)
        .border(1., p.border)
        .child(icon(&glyphs::SEARCH, 17.).color(p.muted))
        .child(
            text_input(&s.search, TextStyle::new(13., 400.))
                .key("history-search")
                .grow(1.)
                .color(p.ink)
                .placeholder("Find a thought, a phrase, a recording…", p.muted)
                .on_edit(|s: &mut State| &mut s.search),
        );
    let mut filters = row().gap(6.).flex_wrap();
    for (id, title) in [
        ("all", "Everything"),
        ("dictation", "Dictation"),
        ("file", "Files"),
    ] {
        let node = btn(
            format!("filter-{id}"),
            title,
            p,
            s.filter == id,
            true,
            move |s, _| s.filter = id,
        )
        .h(30.);
        filters = filters.child(node);
    }
    let needle = s.search.text.to_lowercase();
    let entries: Vec<_> = s
        .snapshot
        .history
        .iter()
        .filter(|e| {
            (s.filter == "all" || e.kind == s.filter)
                && format!("{} {}", e.title, e.preview)
                    .to_lowercase()
                    .contains(&needle)
        })
        .collect();
    let mut list = col()
        .key("history-list")
        .gap(4.)
        .radius(12.)
        .border(1., p.border)
        .fill(p.surface)
        .padding(6.);
    if entries.is_empty() {
        list = list.child(
            col()
                .padding(26.)
                .gap(9.)
                .child(label(
                    if needle.is_empty() {
                        "Your words will be right here."
                    } else {
                        "No matching recordings."
                    },
                    15.,
                    500.,
                    p.ink,
                ))
                .child(label(
                    if needle.is_empty() {
                        "Record an idea or transcribe a file to start your library."
                    } else {
                        "Try a different phrase or filter."
                    },
                    12.,
                    400.,
                    p.muted,
                )),
        );
    }
    for entry in entries {
        list = list.child(ui::entry_row(
            entry,
            p,
            s.selected.as_deref() == Some(&entry.id),
            false,
        ));
    }
    let list = col()
        .grow(1.)
        .min_w(0.)
        .gap(15.)
        .child(filters)
        .child(list)
        .child(label(
            "Showing the 60 most recent transcripts",
            10.5,
            400.,
            p.muted,
        ));
    let mut workspace = if compact { col() } else { row() }.gap(20.).child(list);
    if s.selected.is_some() {
        let detail = if s.show_raw { &s.detail_raw } else { &s.detail };
        let pane = card(p)
            .key("history-detail")
            .gap(18.)
            .child(
                row()
                    .gap(10.)
                    .child(eyebrow("TRANSCRIPT", p))
                    .child(el().grow(1.))
                    .child(link("close-detail", "Close", p, |s, _| s.selected = None)),
            )
            .child(label(&s.detail_title, 20., 500., p.ink))
            .child(
                row()
                    .gap(8.)
                    .flex_wrap()
                    .child(btn(
                        "copy-history",
                        "Copy text",
                        p,
                        true,
                        !detail.is_empty(),
                        |s, tx| {
                            tx.copy(if s.show_raw {
                                s.detail_raw.clone()
                            } else {
                                s.detail.clone()
                            });
                            s.notice = Some("Copied to clipboard".into());
                        },
                    ))
                    .child(btn(
                        "export-history",
                        "Export",
                        p,
                        false,
                        !detail.is_empty(),
                        export_transcript,
                    ))
                    .child(link(
                        "history-raw",
                        if s.show_raw { "Cleaned" } else { "As spoken" },
                        p,
                        |s, _| s.show_raw = !s.show_raw,
                    )),
            )
            .child(divider(p))
            .child(
                label(
                    if detail.is_empty() {
                        "Loading transcript…"
                    } else {
                        detail
                    },
                    13.,
                    400.,
                    p.ink,
                )
                .selectable(true),
            );
        workspace = workspace.child(if compact {
            pane.w_full()
        } else {
            pane.grow(1.).min_w(0.)
        });
    }
    col()
        .gap(23.)
        .child(page_header(
            "A home for your train of thought.",
            "Find it, copy it, pick up where you left off.",
            p,
        ))
        .child(search)
        .child(workspace)
}
