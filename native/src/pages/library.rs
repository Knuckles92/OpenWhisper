use crate::state::{Entry, State};
use crate::ui::{self, Palette, btn, card, divider, eyebrow, label, link, page_header};
use rune::{El, FileDialogOptions, TextEdit, TextStyle, Tx, col, el, icon, row, text_input};
use rune_kit::glyphs;
use serde_json::{Value, json};

mod calendar;
mod demo;
mod remote;
pub use calendar::{calendar_refresh, calendar_response};

pub fn calendar_demo(s: &mut State) {
    s.page = crate::state::Page::History;
    calendar::open(s);
}
pub use demo::{library_demo, library_demo_command};
#[cfg(test)]
mod tests;

pub(super) fn value<'a>(v: &'a Value, key: &str, fallback: &'a str) -> &'a str {
    v[key].as_str().unwrap_or(fallback)
}

pub(super) fn field(s: &State, p: &Palette, key: &str, placeholder: &str, multiline: bool) -> El {
    let empty = TextEdit::new("");
    let edit = s.edits.get(key).unwrap_or(&empty);
    let access = key.to_string();
    let field = text_input(edit, TextStyle::new(13., 400.))
        .key(key)
        .w_full()
        .min_w(0.)
        .color(p.ink)
        .padding(12.)
        .radius(8.)
        .fill(p.soft)
        .border(1., p.border)
        .placeholder(placeholder, p.muted)
        .on_key(|s: &mut State, event, tx| ui::suppress_hotkey(s, event, tx))
        .on_edit(move |s: &mut State| s.edit(&access));
    if multiline {
        field.min_h(190.)
    } else {
        field.single_line().h(43.)
    }
}

pub(super) fn empty(p: &Palette, title: &str, subtitle: &str) -> El {
    col()
        .padding(24.)
        .gap(9.)
        .child(label(title, 15., 500., p.ink))
        .child(label(subtitle, 12., 400., p.muted))
}

pub(super) fn markdown(value: &str, p: &Palette) -> El {
    rune_kit::Markdown::new(value)
        .selectable(true)
        .scroll_wide(true)
        .on_link_tx(|s: &mut State, url, tx| {
            if url.starts_with("https://") || url.starts_with("http://") {
                if let Err(error) = tx.open_url(url) {
                    s.error = Some(format!("Could not open link: {error}"));
                }
            }
        })
        .build(&p.kit)
        .min_w(0.)
}

pub(super) fn collection_search(
    s: &State,
    p: &Palette,
    scope: &'static str,
    key: &'static str,
    placeholder: &str,
) -> El {
    row()
        .gap(8.)
        .center_y()
        .child(
            field(s, p, key, placeholder, false)
                .grow(1.)
                .on_submit(move |s: &mut State, _| {
                    let query = s.edit(key).text.clone();
                    s.command(
                        &format!("{scope}.list"),
                        json!({"query":query,"offset":0,"limit":50}),
                    );
                }),
        )
        .child(btn(
            format!("{scope}-search-submit"),
            "Search",
            p,
            false,
            !s.pending,
            move |s, _| {
                let query = s.edit(key).text.clone();
                s.command(
                    &format!("{scope}.list"),
                    json!({"query":query,"offset":0,"limit":50}),
                );
            },
        ))
}

pub(super) fn collection_pager(
    s: &State,
    p: &Palette,
    scope: &'static str,
    key: &'static str,
) -> El {
    let result = &s.data[format!("{scope}.list")];
    let offset = result["offset"].as_u64().unwrap_or(0);
    let count = result["items"].as_array().map_or(0, Vec::len) as u64;
    row()
        .gap(8.)
        .center_y()
        .flex_wrap()
        .child(
            label(
                if count == 0 {
                    "No saved items".to_string()
                } else {
                    format!("Items {}–{}", offset + 1, offset + count)
                },
                10.5,
                400.,
                p.muted,
            )
            .grow(1.),
        )
        .child(btn(
            format!("{scope}-previous"),
            "Previous",
            p,
            false,
            offset > 0 && !s.pending,
            move |s, _| {
                let query = s.edit(key).text.clone();
                s.command(
                    &format!("{scope}.list"),
                    json!({"query":query,"offset":offset.saturating_sub(50),"limit":50}),
                );
            },
        ))
        .child(btn(
            format!("{scope}-next"),
            "Next",
            p,
            false,
            result["has_more"].as_bool().unwrap_or(false) && !s.pending,
            move |s, _| {
                let query = s.edit(key).text.clone();
                s.command(
                    &format!("{scope}.list"),
                    json!({"query":query,"offset":offset+50,"limit":50}),
                );
            },
        ))
}

pub(super) fn confirm_delete(s: &State, p: &Palette, scope: &'static str) -> El {
    let Some(confirmation) = &s.confirmation else {
        return el();
    };
    if confirmation["scope"] != scope {
        return el();
    }
    let id = value(confirmation, "id", "").to_string();
    let host = value(confirmation, "stored_on", "");
    let explanation = if host.is_empty() {
        "This removes the saved item from your library. This action cannot be undone.".to_string()
    } else {
        format!("This permanently removes the transcript and any retained recording from {host}.")
    };
    let delete_audio = confirmation["delete_audio"].as_bool().unwrap_or(false);
    card(p)
        .key(format!("{scope}-delete-confirmation"))
        .border(1., p.danger.alpha(0.45))
        .child(label(
            format!("Delete “{}”?", value(confirmation, "title", "this item")),
            15.,
            550.,
            p.ink,
        ))
        .child(label(explanation, 12., 400., p.muted))
        .child(
            if scope == "history" && host.is_empty() && confirmation["audio_available"] == true {
                btn(
                    "history-delete-audio-choice",
                    if delete_audio {
                        "✓ Also delete the recording"
                    } else {
                        "Also delete the recording"
                    },
                    p,
                    delete_audio,
                    true,
                    |s, _| {
                        if let Some(confirmation) = s.confirmation.as_mut() {
                            confirmation["delete_audio"] =
                                json!(confirmation["delete_audio"] != true);
                        }
                    },
                )
            } else {
                el()
            },
        )
        .child(
            row()
                .gap(8.)
                .flex_wrap()
                .child(btn(
                    format!("{scope}-confirm-delete"),
                    "Delete permanently",
                    p,
                    false,
                    !s.pending,
                    move |s, _| {
                        let params = if scope == "history" {
                            json!({"id":id,"delete_audio":delete_audio})
                        } else {
                            json!({"id":id})
                        };
                        s.command(&format!("{scope}.delete"), params);
                    },
                ))
                .child(btn(
                    format!("{scope}-keep"),
                    "Keep it",
                    p,
                    true,
                    true,
                    |s, _| s.confirmation = None,
                )),
        )
}

pub(super) fn export_item(
    _s: &mut State,
    tx: &mut Tx,
    scope: &'static str,
    id: String,
    format: &'static str,
) {
    let extension = if format == "markdown" { "md" } else { format };
    let options = FileDialogOptions {
        title: "Export saved item".into(),
        file_name: Some(format!("openwhisper.{extension}")),
        ..Default::default()
    }
    .filter("Export document", &[extension]);
    tx.save_file(options, move |s: &mut State, result, _| match result {
        Ok(Some(path)) => s.command(
            &format!("{scope}.export"),
            json!({"id":id,"format":format,"path":path.to_string_lossy()}),
        ),
        Ok(None) => {}
        Err(error) => s.error = Some(error.to_string()),
    });
}

fn search(s: &mut State) {
    s.library_page = 0;
    s.command(
        "history.list",
        json!({"query":s.search.text,"kind":s.filter,"limit":50,"offset":0}),
    );
}

pub fn history(s: &State, p: &Palette, compact: bool) -> El {
    if s.data["history.view"] == "calendar" {
        return calendar::view(s, p, compact);
    }
    let search_bar = row()
        .gap(8.)
        .center_y()
        .child(
            row()
                .key("history-search-field")
                .grow(1.)
                .min_w(0.)
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
                        .single_line()
                        .key("history-search")
                        .grow(1.)
                        .min_w(0.)
                        .color(p.ink)
                        .placeholder("Search your entire transcript library…", p.muted)
                        .on_key(|s: &mut State, event, tx| ui::suppress_hotkey(s, event, tx))
                        .on_edit(|s: &mut State| &mut s.search)
                        .on_submit(|s: &mut State, _| search(s)),
                ),
        )
        .child(btn(
            "history-search-submit",
            "Search",
            p,
            true,
            s.available() && !s.pending,
            |s, _| search(s),
        ));
    let mut filters = row().gap(6.).flex_wrap();
    for (id, title) in [
        ("all", "Everything"),
        ("dictation", "Dictation"),
        ("file", "Files"),
    ] {
        filters = filters.child(
            btn(
                format!("filter-{id}"),
                title,
                p,
                s.filter == id,
                true,
                move |s, _| {
                    s.filter = id;
                    search(s);
                },
            )
            .h(31.),
        );
    }
    let result = &s.data["history.list"];
    let entries: Vec<Entry> = if let Some(items) = result["items"].as_array() {
        items
            .iter()
            .filter_map(|v| serde_json::from_value(v.clone()).ok())
            .collect()
    } else {
        s.snapshot.history.clone()
    };
    let needle = s.search.text.to_lowercase();
    let shown: Vec<_> = entries
        .iter()
        .filter(|e| {
            if result["items"].is_array() && result["query"].as_str() == Some(s.search.text.trim())
            {
                return s.filter == "all"
                    || e.kind == s.filter
                    || (s.filter == "dictation" && e.kind != "file");
            }
            (s.filter == "all"
                || e.kind == s.filter
                || (s.filter == "dictation" && e.kind != "file"))
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
    if shown.is_empty() {
        list = list.child(empty(
            p,
            if needle.is_empty() {
                "Your words will be right here."
            } else {
                "No matching transcripts."
            },
            if needle.is_empty() {
                "Record an idea or transcribe a file to start your library."
            } else {
                "Try another phrase, or switch to Everything."
            },
        ));
    }
    for entry in shown {
        list = list.child(ui::entry_row(
            entry,
            p,
            s.selected.as_deref() == Some(&entry.id),
            false,
        ));
    }
    let offset = result["offset"].as_u64().unwrap_or(0);
    let total = result["total"].as_u64().unwrap_or(entries.len() as u64);
    let end = (offset + entries.len() as u64).min(total);
    let pagination = row().gap(8.).center_y().flex_wrap()
        .child(label(if total == 0 { "No saved transcripts".into() } else { format!("{}–{} of {} transcripts", offset + 1, end, total) }, 11., 400., p.muted).grow(1.))
        .child(btn("history-previous", "Previous", p, false, offset > 0 && !s.pending, move |s, _| {
            s.library_page = offset.saturating_sub(50) as usize;
            s.command("history.list", json!({"query":s.search.text,"kind":s.filter,"limit":50,"offset":s.library_page}));
        }))
        .child(btn("history-next", "Next", p, false, result["has_more"].as_bool().unwrap_or(false) && !s.pending, move |s, _| {
            s.library_page = (offset + 50) as usize;
            s.command("history.list", json!({"query":s.search.text,"kind":s.filter,"limit":50,"offset":s.library_page}));
        }));
    let list = col()
        .grow(1.)
        .min_w(0.)
        .gap(15.)
        .child(filters)
        .child(list)
        .child(pagination)
        .child(remote::view(s, p));
    let mut workspace = if compact { col() } else { row() }.gap(20.).child(list);
    if let Some(id) = &s.selected {
        let detail_result = &s.data["history.detail"];
        let matching = detail_result["id"].as_str() == Some(id.as_str());
        let editable = matching && detail_result["editable"].as_bool().unwrap_or(true);
        let can_delete = matching && detail_result["can_delete"].as_bool().unwrap_or(editable);
        let detail = if s.show_raw { &s.detail_raw } else { &s.detail };
        let edit_id = id.clone();
        let delete_id = id.clone();
        let export_id = id.clone();
        let title = s.detail_title.clone();
        let stored_on = value(detail_result, "stored_on", "").to_string();
        let delete_host = stored_on.clone();
        let delete_audio_available =
            matching && detail_result["audio_available"] == true && detail_result["remote"] != true;
        let mut playback_path = if matching {
            value(detail_result, "playback_path", "").to_string()
        } else {
            String::new()
        };
        let audio_ready = &s.data["events"]["history.audio.ready"];
        if audio_ready["id"].as_str() == Some(id.as_str()) {
            if let Some(path) = audio_ready["path"].as_str() {
                playback_path = path.into();
            }
        }
        let playback_ready =
            !playback_path.is_empty() && std::path::Path::new(&playback_path).is_file();
        let has_remote_audio =
            matching && detail_result["remote"] == true && detail_result["audio_available"] == true;
        let mut pane = card(p)
            .key("history-detail")
            .gap(18.)
            .child(
                row()
                    .gap(10.)
                    .child(eyebrow("TRANSCRIPT", p))
                    .child(el().grow(1.))
                    .child(link("close-detail", "Close", p, |s, _| {
                        s.selected = None;
                        s.selected_tab.clear();
                        s.confirmation = None;
                    })),
            )
            .child(label(&s.detail_title, 20., 500., p.ink))
            .child(if stored_on.is_empty() {el()}else{label(format!("Kept on {stored_on}"),11.5,500.,p.accent)})
            .child(
                row()
                    .gap(7.)
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
                        "Export text",
                        p,
                        false,
                        !detail.is_empty(),
                        move |s, tx| export_item(s, tx, "history", export_id.clone(), "txt"),
                    ))
                    .child(link(
                        "history-raw",
                        if s.show_raw { "Cleaned" } else { "As spoken" },
                        p,
                        |s, _| s.show_raw = !s.show_raw,
                    )),
            )
            .child(
                row()
                    .gap(7.)
                    .flex_wrap()
                    .child(btn(
                        "history-play",
                        if crate::platform::audio_playing() {
                            "Stop audio"
                        } else {
                            "Play recording"
                        },
                        p,
                        false,
                        playback_ready || crate::platform::audio_playing(),
                        move |s, _| {
                            if crate::platform::audio_playing() {
                                crate::platform::stop_audio();
                            } else if let Err(error) = crate::platform::play_audio(&playback_path) {
                                s.error = Some(error);
                            }
                        },
                    ))
                    .child(btn(
                        "history-edit",
                        "Edit",
                        p,
                        false,
                        editable && !s.pending && !s.detail.is_empty(),
                        move |s, _| {
                            s.selected_tab = "history-edit".into();
                            s.edit("history-title").text = s.detail_title.clone();
                            s.edit("history-body").text = s.detail.clone();
                            s.selected = Some(edit_id.clone());
                        },
                    ))
                    .child(btn(
                        "history-delete",
                        "Delete",
                        p,
                        false,
                        can_delete && !s.pending,
                        move |s, _| {
                            s.confirmation =
                                Some(json!({"scope":"history","id":delete_id,"title":title,"stored_on":delete_host,"audio_available":delete_audio_available,"delete_audio":false}))
                        },
                    )),
            )
            .child(divider(p));
        if has_remote_audio && !playback_ready {
            let audio_id = id.clone();
            pane = pane.child(btn(
                "history-fetch-audio",
                "Get recording from host",
                p,
                false,
                !s.pending,
                move |s, _| {
                    s.command("history.audio.get", json!({"id":audio_id}));
                    s.notice = Some("Getting the recording from your paired host…".into());
                },
            ));
            if audio_ready["id"].as_str() == Some(id.as_str()) {
                if let Some(error) = audio_ready["error"].as_str() {
                    pane = pane.child(label(error, 11., 400., p.danger));
                }
            }
        }
        if playback_ready {
            let retry_id = id.clone();
            pane = pane.child(btn(
                "history-retranscribe",
                "Transcribe recording again",
                p,
                false,
                s.available() && !s.working() && !s.pending,
                move |s, _| {
                    s.command("history.retranscribe", json!({"id":retry_id}));
                },
            ));
        }
        if editable
            && detail_result["version"]
                .as_str()
                .is_some_and(|version| !version.is_empty())
        {
            let selected = detail_result["version"].as_str().unwrap_or("");
            let mut versions =
                row()
                    .gap(7.)
                    .flex_wrap()
                    .child(label("Use in saved history:", 11., 400., p.muted));
            for (version, title) in [("original", "As spoken"), ("ai", "AI cleaned")] {
                let version_id = id.clone();
                versions = versions.child(btn(
                    format!("history-use-{version}"),
                    title,
                    p,
                    selected == version,
                    !s.pending && selected != version,
                    move |s, _| {
                        s.command("history.update", json!({"id":version_id,"version":version}))
                    },
                ));
            }
            pane = pane.child(versions);
        }
        let mut formats = row().gap(7.).flex_wrap();
        for (format, title) in [("md", "Markdown"), ("json", "JSON"), ("srt", "Subtitles")] {
            let id = id.clone();
            formats = formats.child(link(
                format!("history-export-{format}"),
                title,
                p,
                move |s, tx| export_item(s, tx, "history", id.clone(), format),
            ));
        }
        pane = pane.child(formats);
        if s.selected_tab == "history-edit" {
            let id = id.clone();
            pane = pane
                .child(field(s, p, "history-title", "Transcript title", false))
                .child(field(s, p, "history-body", "Transcript text", true))
                .child(
                    row()
                        .gap(8.)
                        .flex_wrap()
                        .child(btn(
                            "history-save",
                            "Save changes",
                            p,
                            true,
                            !s.pending,
                            move |s, _| {
                                let title = s.edit("history-title").text.clone();
                                let text = s.edit("history-body").text.clone();
                                s.command(
                                    "history.update",
                                    json!({"id":id,"title":title,"text":text}),
                                );
                            },
                        ))
                        .child(btn(
                            "history-edit-cancel",
                            "Cancel editing",
                            p,
                            false,
                            true,
                            |s, _| s.selected_tab.clear(),
                        )),
                );
        } else {
            pane = pane.child(
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
            if !playback_ready && !detail.is_empty() {
                pane = pane.child(label(
                    "The original recording is unavailable on this computer.",
                    10.5,
                    400.,
                    p.muted,
                ));
            }
        }
        pane = pane.child(confirm_delete(s, p, "history"));
        workspace = if compact {
            col().gap(20.).child(pane.w_full()).child(workspace)
        } else {
            workspace.child(pane.grow(1.).min_w(0.))
        };
    }
    col()
        .gap(23.)
        .child(page_header(
            "A home for your train of thought.",
            "Search every transcript. Return to the words that matter.",
            p,
        ))
        .child(search_bar)
        .child(
            row()
                .gap(8.)
                .center_y()
                .flex_wrap()
                .child(
                    label(
                        "Search includes the complete transcript, original words, and titles.",
                        10.5,
                        400.,
                        p.muted,
                    )
                    .grow(1.),
                )
                .child(link("open-history-calendar", "Calendar", p, |s, _| {
                    calendar::open(s)
                }))
                .child(link(
                    "export-history-library",
                    "Export library",
                    p,
                    |s, tx| export_item(s, tx, "history", String::new(), "txt"),
                )),
        )
        .child(workspace)
}
