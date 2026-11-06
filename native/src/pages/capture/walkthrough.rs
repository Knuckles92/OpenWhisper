use super::super::library::{
    collection_pager, collection_search, confirm_delete, empty, field, value,
};
use crate::state::State;
use crate::ui::{self, Palette, btn, card, divider, eyebrow, label, link, page_header};
use rune::{El, SurfaceFit, col, el, icon, row};
use serde_json::json;

const STEPS_PER_PAGE: usize = 6;
mod library_tools;
pub use library_tools::{walkthrough_block_navigation, walkthrough_event, walkthrough_response};

fn gallery_page(s: &State, key: &str, count: usize) -> usize {
    (s.data[key].as_u64().unwrap_or(0) as usize).min(count.saturating_sub(1) / STEPS_PER_PAGE)
}

fn load_gallery(s: &mut State, page: usize, editing: bool, live: bool) {
    let key = if live {
        "walkthrough.live.page"
    } else {
        "walkthrough.gallery.page"
    };
    s.data[key] = json!(page);
    let steps = if live {
        s.data["snapshot"]["walkthrough_live"]["shots"]
            .as_array()
            .cloned()
            .unwrap_or_default()
    } else {
        s.data["walkthroughs.detail"]["steps"]
            .as_array()
            .cloned()
            .unwrap_or_default()
    };
    let order: Vec<usize> = if editing {
        s.data["walkthrough.review.draft"]["order"]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(|v| v.as_u64().map(|i| i as usize))
            .collect()
    } else {
        (0..steps.len()).collect()
    };
    let paths: Vec<String> = order
        .iter()
        .skip(page * STEPS_PER_PAGE)
        .take(STEPS_PER_PAGE)
        .filter_map(|&index| {
            steps
                .get(index)
                .and_then(|step| step["image_path"].as_str())
                .map(str::to_string)
        })
        .collect();
    for path in paths {
        s.load_image(&path);
    }
}

pub(super) fn reload_gallery(s: &mut State) {
    let saved = s.selected_item.is_some();
    let key = if saved {
        "walkthrough.gallery.page"
    } else {
        "walkthrough.live.page"
    };
    let count = if saved {
        s.data["walkthroughs.detail"]["steps"]
            .as_array()
            .map_or(0, Vec::len)
    } else {
        s.data["snapshot"]["walkthrough_live"]["shots"]
            .as_array()
            .map_or(0, Vec::len)
    };
    let page = gallery_page(s, key, count);
    load_gallery(
        s,
        page,
        saved && s.selected_tab == "walkthrough-edit",
        !saved,
    );
}

fn gallery_navigation(s: &State, p: &Palette, count: usize, editing: bool, live: bool) -> El {
    let key = if live {
        "walkthrough.live.page"
    } else {
        "walkthrough.gallery.page"
    };
    let page = gallery_page(s, key, count);
    if count <= STEPS_PER_PAGE {
        return el();
    }
    let prefix = if live {
        "live-gallery"
    } else {
        "saved-gallery"
    };
    row()
        .gap(8.)
        .center_y()
        .flex_wrap()
        .child(
            label(
                format!(
                    "Steps {}–{} of {}",
                    page * STEPS_PER_PAGE + 1,
                    ((page + 1) * STEPS_PER_PAGE).min(count),
                    count
                ),
                11.,
                400.,
                p.muted,
            )
            .grow(1.),
        )
        .child(btn(
            format!("{prefix}-previous"),
            "Previous steps",
            p,
            false,
            page > 0,
            move |s, _| load_gallery(s, page - 1, editing, live),
        ))
        .child(btn(
            format!("{prefix}-next"),
            "Next steps",
            p,
            false,
            (page + 1) * STEPS_PER_PAGE < count,
            move |s, _| load_gallery(s, page + 1, editing, live),
        ))
}

fn screenshot(s: &State, p: &Palette, path: &str, key: &str) -> El {
    let path = path.to_string();
    let mut panel = col().gap(8.);
    if let Some(image) = s.images.get(&path) {
        panel = panel.child(
            el().key(format!("{key}-image"))
                .w_full()
                .h(235.)
                .radius(9.)
                .fill(p.soft)
                .pixels_fit(image.pixels(), SurfaceFit::Fit)
                .role("img")
                .label("Walkthrough screenshot"),
        );
        if let Some(error) = image.error() {
            panel = panel.child(label(
                format!("Could not display screenshot: {error}"),
                11.,
                400.,
                p.danger,
            ));
        }
    }
    if !path.is_empty() {
        let copy_path = path.clone();
        panel = panel.child(
            row()
                .gap(10.)
                .flex_wrap()
                .child(link(
                    format!("{key}-open"),
                    "Open screenshot",
                    p,
                    move |s, _| {
                        if let Err(error) = crate::platform::open_path(&path) {
                            s.error = Some(error);
                        }
                    },
                ))
                .child(if let Some(id) = s.selected_item.clone() {
                    let filename = std::path::Path::new(&copy_path)
                        .file_name()
                        .and_then(|p| p.to_str())
                        .unwrap_or("")
                        .to_string();
                    btn(
                        format!("{key}-copy"),
                        "Copy screenshot",
                        p,
                        false,
                        !s.pending,
                        move |s, _| {
                            s.command(
                                "walkthroughs.screenshot.copy",
                                json!({"id":id,"filename":filename}),
                            )
                        },
                    )
                } else {
                    el()
                }),
        );
    }
    panel
}

fn live_writeup_status(s: &State, p: &Palette) -> El {
    if s.data["snapshot"]["walkthrough_live"]["writing_up"] != true {
        return el();
    }
    card(p).key("walkthrough-live-writing-up").fill(p.accent_soft)
        .child(label("Writing up your walkthrough…",17.,550.,p.ink))
        .child(label("The spoken document is saved. You can keep using dictation while the write-up finishes.",12.,400.,p.muted))
        .child(btn("walkthrough-cancel-live-writeup","Cancel this write-up",p,false,!s.pending,|s,_|s.command("walkthrough.cancel",json!({}))))
}

fn step_context(step: &serde_json::Value, p: &Palette) -> El {
    let mut context = col().gap(6.);
    if let Some(seconds) = step["kept_at_s"].as_f64() {
        let seconds = seconds.max(0.) as u64;
        context = context.child(label(
            format!("Captured at {}:{:02}", seconds / 60, seconds % 60),
            10.5,
            400.,
            p.muted,
        ));
    }
    for note in step["notes"].as_array().into_iter().flatten() {
        let text = note.as_str().unwrap_or_else(|| value(note, "text", ""));
        if !text.is_empty() {
            context = context.child(
                card(p)
                    .padding(12.)
                    .fill(p.accent_soft)
                    .child(label(
                        "Note for the agent · not in the document",
                        10.5,
                        550.,
                        p.accent,
                    ))
                    .child(label(text, 12., 400., p.ink).selectable(true)),
            );
        }
    }
    context
}

pub fn walkthrough(s: &State, p: &Palette) -> El {
    let live = &s.data["snapshot"]["walkthrough_live"];
    let active = (s.snapshot.mode == "walkthrough" && s.snapshot.recording)
        || live["active"].as_bool().unwrap_or(false);
    let paused = live["paused"].as_bool().unwrap_or(false);
    let note_held = live["note_held"].as_bool().unwrap_or(false);
    if let Some(id) = &s.selected_item {
        let mut review = col().key("walkthrough-review").gap(18.);
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
                                    "Your current walkthrough is paused."
                                } else {
                                    "Your current walkthrough is still recording."
                                },
                                12.,
                                450.,
                                p.ink,
                            )
                            .grow(1.)
                            .min_w(0.),
                        )
                        .child(btn(
                            "review-walkthrough-snap",
                            "Capture screenshot",
                            p,
                            false,
                            !s.pending,
                            |s, _| s.command("walkthrough.snap", json!({})),
                        ))
                        .child(btn(
                            "review-walkthrough-finish",
                            "Finish walkthrough",
                            p,
                            true,
                            !s.pending,
                            |s, _| s.command("walkthrough.toggle", json!({})),
                        )),
                ),
            );
        }
        if live["writing_up"] == true {
            review = review.child(live_writeup_status(s, p));
        }
        if library_tools::has_ready_offer(s) {
            review = review.child(library_tools::ready_offer(s, p));
        }
        return review.child(walkthrough_detail(s, p, id));
    }
    let mut control = card(p).key("walkthrough-card").padding(26.).gap(20.).fill(p.soft)
        .child(row().gap(18.).center_y()
            .child(row().size(55., 55.).radius(16.).fill(p.accent_soft).center().child(icon(&ui::WALK, 27.).color(p.accent)))
            .child(col().grow(1.).min_w(0.).gap(7.)
                .child(label(if active { if paused { "Your walkthrough is paused." } else { "The story is taking shape." } } else { "A little context goes a long way." }, 23., 500., p.ink))
                .child(label(if active { value(live, "caption", "Speak naturally. Capture a screenshot whenever the next detail matters.") } else { "Talk through a task. Capture the moments. Keep the story together." }, 12., 400., p.muted))))
        .child(divider(p))
        .child(row().gap(9.).flex_wrap()
            .child(btn("walkthrough-toggle", if active { "Finish walkthrough" } else { "Start walkthrough" }, p, true,
                s.available() && !s.pending && (active || (!s.working()&&!library_tools::library_busy(s)&&live["writing_up"]!=true)), |s, _| s.command("walkthrough.toggle", json!({}))))
            .child(btn("walkthrough-snap", "Capture screenshot", p, false, active && !s.pending, |s, _| s.command("walkthrough.snap", json!({}))))
            .child(btn("walkthrough-pause", if paused { "Resume" } else { "Pause" }, p, false, active && !s.pending,
                move |s, _| s.command("walkthrough.pause", json!({"paused":!paused}))))
            .child(btn("walkthrough-note", if note_held { "Finish spoken note" } else { "Add spoken note" }, p, note_held,
                active && live["note_enabled"].as_bool().unwrap_or(false) && !paused && !s.pending,
                move |s, _| s.command("walkthrough.note", json!({"on":!note_held}))))
            .child(btn("walkthrough-cancel", "Cancel", p, false, active && !s.pending, |s, _| {
                s.confirmation = Some(json!({"scope":"walkthrough-cancel"}));
            })));
    if active {
        control = control.child(
            row()
                .gap(7.)
                .center_y()
                .flex_wrap()
                .child(label("Capture a", 11., 450., p.muted))
                .child(btn(
                    "snap-region",
                    "Region",
                    p,
                    false,
                    !s.pending,
                    |s, _| s.command("walkthrough.snap_mode", json!({"mode":"region"})),
                ))
                .child(btn(
                    "snap-window",
                    "Window",
                    p,
                    false,
                    !s.pending,
                    |s, _| s.command("walkthrough.snap_mode", json!({"mode":"window"})),
                ))
                .child(btn(
                    "snap-screen",
                    "Full screen",
                    p,
                    false,
                    !s.pending,
                    |s, _| s.command("walkthrough.snap_mode", json!({"mode":"screen"})),
                ))
                .child(btn(
                    "snap-delayed",
                    "In 3 seconds",
                    p,
                    false,
                    !s.pending,
                    |s, _| s.command("walkthrough.snap_mode", json!({"mode":"delayed"})),
                )),
        );
        if note_held {
            control = control.child(label(
                "Speaking a note for the write-up. Finish the note to return to the walkthrough.",
                12.,
                450.,
                p.accent,
            ));
        }
    }
    if s.confirmation.as_ref().map(|v| v["scope"].as_str()) == Some(Some("walkthrough-cancel")) {
        control = control.child(
            card(p)
                .border(1., p.danger.alpha(0.45))
                .child(label("Discard this walkthrough?", 15., 550., p.ink))
                .child(label(
                    "The current recording and screenshots will not be saved.",
                    12.,
                    400.,
                    p.muted,
                ))
                .child(
                    row()
                        .gap(8.)
                        .flex_wrap()
                        .child(btn(
                            "confirm-walkthrough-cancel",
                            "Discard walkthrough",
                            p,
                            false,
                            !s.pending,
                            |s, _| {
                                s.command("record.cancel", json!({}));
                                s.confirmation = None;
                            },
                        ))
                        .child(btn(
                            "keep-walkthrough",
                            "Keep recording",
                            p,
                            true,
                            true,
                            |s, _| s.confirmation = None,
                        )),
                ),
        );
    }
    if library_tools::library_busy(s) && !active {
        control = control.child(label(
            "A saved walkthrough is being updated. You can keep using dictation while it finishes.",
            12.,
            400.,
            p.muted,
        ));
    }
    let mut content = col().gap(24.).child(page_header(
        "Show it. Say it. Save it.",
        "Turn a spoken walkthrough and screenshots into a document worth sharing.",
        p,
    ));
    if live["writing_up"] == true {
        content = content.child(live_writeup_status(s, p));
    }
    if library_tools::has_ready_offer(s) {
        content = content.child(library_tools::ready_offer(s, p));
    }
    content = content.child(control);
    if active {
        let mut steps = card(p).key("walkthrough-live-steps").gap(17.).child(
            row()
                .gap(10.)
                .center_y()
                .flex_wrap()
                .child(eyebrow("YOUR WALKTHROUGH", p))
                .child(el().grow(1.))
                .child(link(
                    "walkthrough-undo",
                    "Undo last screenshot",
                    p,
                    |s, _| s.command("walkthrough.undo", json!({})),
                )),
        );
        if let Some(shots) = live["shots"].as_array().filter(|shots| !shots.is_empty()) {
            let page = gallery_page(s, "walkthrough.live.page", shots.len());
            steps = steps.child(gallery_navigation(s, p, shots.len(), false, true));
            for (index, shot) in shots
                .iter()
                .enumerate()
                .skip(page * STEPS_PER_PAGE)
                .take(STEPS_PER_PAGE)
            {
                let number = shot["number"]
                    .as_u64()
                    .or_else(|| shot["index"].as_u64())
                    .unwrap_or(index as u64 + 1);
                steps = steps.child(
                    col()
                        .gap(10.)
                        .child(
                            row()
                                .gap(8.)
                                .center_y()
                                .flex_wrap()
                                .child(
                                    label(
                                        format!(
                                            "{} · {}",
                                            index + 1,
                                            value(shot, "title", "Screenshot")
                                        ),
                                        13.,
                                        550.,
                                        p.ink,
                                    )
                                    .grow(1.),
                                )
                                .child(btn(
                                    format!("shot-up-{number}"),
                                    "Move up",
                                    p,
                                    false,
                                    index > 0 && !s.pending,
                                    move |s, _| {
                                        s.command(
                                            "walkthrough.move",
                                            json!({"number":number,"step":-1}),
                                        )
                                    },
                                ))
                                .child(btn(
                                    format!("shot-down-{number}"),
                                    "Move down",
                                    p,
                                    false,
                                    index + 1 < shots.len() && !s.pending,
                                    move |s, _| {
                                        s.command(
                                            "walkthrough.move",
                                            json!({"number":number,"step":1}),
                                        )
                                    },
                                ))
                                .child(link(
                                    format!("shot-remove-{number}"),
                                    "Remove",
                                    p,
                                    move |s, _| {
                                        s.command("walkthrough.remove", json!({"number":number}))
                                    },
                                )),
                        )
                        .child(screenshot(
                            s,
                            p,
                            value(shot, "image_path", ""),
                            &format!("live-shot-{number}"),
                        ))
                        .child(label(value(shot, "text", ""), 12., 400., p.ink).selectable(true)),
                );
            }
        } else {
            steps = steps.child(empty(
                p,
                "Start with the first detail.",
                "Your screenshots and spoken steps will appear here as you capture them.",
            ));
        }
        content = content.child(steps);
    }
    let mut library = card(p).key("walkthrough-library").gap(14.).child(
        row()
            .gap(12.)
            .center_y()
            .flex_wrap()
            .child(label("Saved walkthroughs", 18., 500., p.ink).grow(1.))
            .child(link("refresh-walkthroughs", "Refresh", p, |s, _| {
                s.command("walkthroughs.list", json!({}))
            })),
    );
    library = library.child(collection_search(
        s,
        p,
        "walkthroughs",
        "walkthrough-query",
        "Find a saved walkthrough…",
    ));
    if let Some(items) = s.data["walkthroughs.list"]["items"]
        .as_array()
        .filter(|items| !items.is_empty())
    {
        for item in items {
            let id = value(item, "id", "").to_string();
            library = library.child(
                row()
                    .key(format!("walkthrough-{id}"))
                    .gap(12.)
                    .padding(13.)
                    .radius(9.)
                    .fill(if s.selected_item.as_deref() == Some(&id) {
                        p.accent_soft
                    } else {
                        p.soft
                    })
                    .role("button")
                    .label(value(item, "title", "Walkthrough"))
                    .focusable(true)
                    .focus_ring(2., 1., p.accent)
                    .hover(p.accent_soft)
                    .on_press_tx(move |s: &mut State, tx| {
                        s.selected_item = Some(id.clone());
                        s.selected_tab.clear();
                        s.confirmation = None;
                        s.data["walkthrough.gallery.page"] = json!(0);
                        s.command("walkthroughs.detail", json!({"id":id}));
                        tx.scroll_key_to("walkthrough-review", 0.);
                    })
                    .child(icon(&ui::WALK, 19.).color(p.accent))
                    .child(
                        col()
                            .grow(1.)
                            .min_w(0.)
                            .gap(5.)
                            .child(
                                label(value(item, "title", "Walkthrough"), 13., 550., p.ink)
                                    .max_lines(1),
                            )
                            .child(
                                label(
                                    value(
                                        item,
                                        "preview",
                                        value(item, "started_at", "Saved walkthrough"),
                                    ),
                                    11.,
                                    400.,
                                    p.muted,
                                )
                                .max_lines(2),
                            ),
                    )
                    .child(label(
                        if item["status"] == "draft" {
                            "Interrupted draft".into()
                        } else {
                            format!("{} steps", item["step_count"].as_u64().unwrap_or(0))
                        },
                        10.,
                        450.,
                        p.muted,
                    )),
            );
        }
    } else {
        library = library.child(empty(
            p,
            "Keep the know-how.",
            "Saved walkthroughs and their screenshots will be ready to review here.",
        ));
    }
    content = content.child(library_tools::retention(s, p));
    content =
        content.child(library.child(collection_pager(s, p, "walkthroughs", "walkthrough-query")));
    content
}

fn walkthrough_detail(s: &State, p: &Palette, id: &str) -> El {
    let detail = &s.data["walkthroughs.detail"];
    if detail["id"].as_str() != Some(id) {
        return card(p).child(label("Loading your walkthrough…", 13., 400., p.muted));
    }
    let title = value(detail, "title", "Walkthrough");
    let markdown = value(detail, "markdown", "").to_string();
    let copy = markdown.clone();
    let delete_id = id.to_string();
    let delete_title = title.to_string();
    let mutable = !library_tools::dirty(s, id) && !library_tools::busy(s, id) && !s.pending;
    let clean = mutable && detail["status"] != "draft" && value(detail, "error", "").is_empty();
    let mut actions = row().gap(7.).flex_wrap().child(btn(
        "copy-walkthrough",
        "Copy text",
        p,
        true,
        !markdown.is_empty() && clean,
        move |s, tx| {
            tx.copy(copy.clone());
            s.notice = Some("Walkthrough copied".into());
        },
    ));
    for (format, title) in [
        ("md", "Markdown"),
        ("pdf", "PDF"),
        ("docx", "Word"),
        ("zip", "Document + assets"),
    ] {
        let id = id.to_string();
        actions = actions.child(btn(
            format!("export-walkthrough-{format}"),
            title,
            p,
            false,
            clean
                && (format == "zip"
                    || detail["exports"].as_array().is_some_and(|formats| {
                        formats.iter().any(|entry| entry.as_str() == Some(format))
                    })),
            move |s, tx| library_tools::export_version(s, tx, id.clone(), format),
        ));
    }
    let mut panel = card(p)
        .key("walkthrough-detail")
        .gap(18.)
        .child(
            row()
                .gap(12.)
                .center_y()
                .child(eyebrow("WALKTHROUGH REVIEW", p))
                .child(el().grow(1.))
                .child(link("close-walkthrough-detail", "Close", p, |s, _| {
                    library_tools::request_action(s, json!({"kind":"close"}))
                })),
        )
        .child(label(title, 23., 500., p.ink))
        .child(actions)
        .child(library_tools::document_actions(s, p, id, detail));
    if detail["is_draft"] == true
        || detail["status"] == "draft"
        || detail["can_recover"] == true
        || !value(detail, "error", "").is_empty()
    {
        panel = panel.child(library_tools::draft_recovery(s, p, id, detail));
    }
    if library_tools::has_job_status(s, id) {
        panel = panel.child(library_tools::job_status(s, p, id));
    }
    panel=panel.child(library_tools::version_picker(s,p,id,detail))
        .child(
            row()
                .gap(8.)
                .flex_wrap()
                .child(btn(
                    "walkthrough-steps-tab",
                    "Steps & screenshots",
                    p,
                    !matches!(s.selected_tab.as_str(),"walkthrough-document"|"walkthrough-writeup-edit"),
                    true,
                    |s, _| library_tools::request_action(s,json!({"kind":"tab","tab":"walkthrough-steps"})),
                ))
                .child(btn(
                    "walkthrough-document-tab",
                    "Document",
                    p,
                    matches!(s.selected_tab.as_str(),"walkthrough-document"|"walkthrough-writeup-edit"),
                    true,
                    |s, _| library_tools::request_action(s,json!({"kind":"tab","tab":"walkthrough-document"})),
                ))
                .child(btn(
                    "walkthrough-review-edit",
                    "Edit spoken steps",
                    p,
                    false,
                    clean&&detail["spoken"]==true
                        && detail["steps"]
                            .as_array()
                            .is_some_and(|steps| !steps.is_empty()),
                    |s, _| {
                        let steps = s.data["walkthroughs.detail"]["steps"]
                            .as_array()
                            .cloned()
                            .unwrap_or_default();
                        let keep: Vec<bool> = steps
                            .iter()
                            .map(|step| step["included"].as_bool().unwrap_or(true))
                            .collect();
                        for (index, step) in steps.iter().enumerate() {
                            s.edit(&format!("walkthrough-edit-step-{index}")).text =
                                value(step, "text", "").to_string();
                        }
                        s.data["walkthrough.review.draft"] =
                            json!({"id":s.selected_item,"order":(0..steps.len()).collect::<Vec<_>>(),"keep":keep});
                        s.data["walkthrough.gallery.page"] = json!(0);
                        s.selected_tab = "walkthrough-edit".into();
                    },
                ))
                .child(btn("walkthrough-edit-writeup","Edit write-up",p,false,clean&&detail["written_up"]==true&&detail["spoken"]!=true,{
                    let id=id.to_string();move|s,_|library_tools::edit_writeup(s,&id)
                }))
                .child(btn("delete-walkthrough", "Delete", p, false, mutable, move |s, _| {
                    s.confirmation =
                        Some(json!({"scope":"walkthroughs","id":delete_id,"title":delete_title}))
                })),
        )
        .child(divider(p))
        .child(library_tools::unsaved_confirmation(s,p));
    if s.selected_tab == "walkthrough-writeup-edit" {
        panel = panel.child(library_tools::writeup_editor(s, p, id));
    } else if s.selected_tab == "walkthrough-edit" {
        let editor = review_editor(s, p, id, detail);
        panel = panel.child(if library_tools::busy(s, id) {
            editor.inert()
        } else {
            editor
        });
    } else if s.selected_tab == "walkthrough-document" {
        panel = panel.child(if !markdown.is_empty() {
            super::super::library::markdown(&markdown, p)
        } else {
            label(
                if markdown.is_empty() {
                    "The document has not been saved for this walkthrough."
                } else {
                    &markdown
                },
                13.,
                400.,
                p.ink,
            )
            .selectable(true)
        });
    } else if let Some(steps) = detail["steps"].as_array().filter(|steps| !steps.is_empty()) {
        let page = gallery_page(s, "walkthrough.gallery.page", steps.len());
        panel = panel.child(gallery_navigation(s, p, steps.len(), false, false));
        for (index, step) in steps
            .iter()
            .enumerate()
            .skip(page * STEPS_PER_PAGE)
            .take(STEPS_PER_PAGE)
        {
            panel = panel.child(
                col()
                    .gap(12.)
                    .key(format!("walkthrough-step-{index}"))
                    .child(label(
                        format!("{} · {}", index + 1, value(step, "title", "Step")),
                        16.,
                        550.,
                        p.ink,
                    ))
                    .child(screenshot(
                        s,
                        p,
                        value(step, "image_path", ""),
                        &format!("saved-step-{index}"),
                    ))
                    .child(if step["included"] == false {
                        label(
                            "Screenshot omitted from the exported document",
                            10.5,
                            450.,
                            p.muted,
                        )
                    } else {
                        el()
                    })
                    .child(label(value(step, "text", ""), 13., 400., p.ink).selectable(true))
                    .child(step_context(step, p)),
            );
        }
    } else {
        panel = panel.child(super::super::library::markdown(&markdown, p));
    }
    panel
        .child(library_tools::retry_controls(s, p, id, detail))
        .child(if library_tools::busy(s, id) {
            el()
        } else {
            confirm_delete(s, p, "walkthroughs")
        })
}

fn review_editor(s: &State, p: &Palette, id: &str, detail: &serde_json::Value) -> El {
    let draft = &s.data["walkthrough.review.draft"];
    let steps = detail["steps"].as_array().cloned().unwrap_or_default();
    let order = draft["order"].as_array().cloned().unwrap_or_default();
    let page = gallery_page(s, "walkthrough.gallery.page", order.len());
    let mut editor = col().key("walkthrough-review-editor").gap(17.)
        .child(label("Polish the wording, rearrange steps, and choose which screenshots belong in the document.", 12., 400., p.muted))
        .child(gallery_navigation(s, p, order.len(), true, false));
    for (position, index) in order
        .iter()
        .filter_map(|v| v.as_u64())
        .map(|i| i as usize)
        .enumerate()
        .skip(page * STEPS_PER_PAGE)
        .take(STEPS_PER_PAGE)
    {
        let Some(step) = steps.get(index) else {
            continue;
        };
        let included = draft["keep"][index].as_bool().unwrap_or(true);
        editor = editor.child(
            card(p)
                .padding(14.)
                .gap(11.)
                .fill(p.soft)
                .child(
                    row()
                        .gap(7.)
                        .flex_wrap()
                        .center_y()
                        .child(label(format!("Step {}", position + 1), 14., 550., p.ink).grow(1.))
                        .child(btn(
                            format!("review-up-{index}"),
                            "Move up",
                            p,
                            false,
                            position > 0,
                            move |s, _| {
                                if let Some(order) =
                                    s.data["walkthrough.review.draft"]["order"].as_array_mut()
                                {
                                    if position > 0 {
                                        order.swap(position, position - 1);
                                    }
                                }
                                load_gallery(s, position / STEPS_PER_PAGE, true, false);
                            },
                        ))
                        .child(btn(
                            format!("review-down-{index}"),
                            "Move down",
                            p,
                            false,
                            position + 1 < order.len(),
                            move |s, _| {
                                if let Some(order) =
                                    s.data["walkthrough.review.draft"]["order"].as_array_mut()
                                {
                                    if position + 1 < order.len() {
                                        order.swap(position, position + 1);
                                    }
                                }
                                load_gallery(s, position / STEPS_PER_PAGE, true, false);
                            },
                        ))
                        .child(btn(
                            format!("review-include-{index}"),
                            if included {
                                "Omit screenshot"
                            } else {
                                "Include screenshot"
                            },
                            p,
                            false,
                            step["image_path"].is_string(),
                            move |s, _| {
                                s.data["walkthrough.review.draft"]["keep"][index] =
                                    json!(!included);
                            },
                        )),
                )
                .child(screenshot(
                    s,
                    p,
                    value(step, "image_path", ""),
                    &format!("review-step-{index}"),
                ))
                .child(step_context(step, p))
                .child(field(
                    s,
                    p,
                    &format!("walkthrough-edit-step-{index}"),
                    "Explain this step…",
                    true,
                )),
        );
    }
    let id = id.to_string();
    editor.child(
        row()
            .gap(8.)
            .flex_wrap()
            .child(btn(
                "save-walkthrough-review",
                "Save walkthrough",
                p,
                true,
                !s.pending,
                move |s, _| {
                    let draft = s.data["walkthrough.review.draft"].clone();
                    let order: Vec<_> = draft["order"]
                        .as_array()
                        .into_iter()
                        .flatten()
                        .filter_map(|index| index.as_u64().map(|index| index as usize))
                        .collect();
                    let texts: Vec<_> = order
                        .iter()
                        .map(|index| {
                            s.edit(&format!("walkthrough-edit-step-{index}"))
                                .text
                                .clone()
                        })
                        .collect();
                    let keep: Vec<_> = order
                        .iter()
                        .map(|index| draft["keep"][*index].as_bool().unwrap_or(true))
                        .collect();
                    s.command(
                        "walkthroughs.review",
                        json!({"id":id,"texts":texts,"keep":keep,"order":order}),
                    );
                },
            ))
            .child(btn(
                "cancel-walkthrough-review",
                "Cancel editing",
                p,
                false,
                true,
                |s, _| {
                    library_tools::request_action(
                        s,
                        json!({"kind":"tab","tab":"walkthrough-steps"}),
                    )
                },
            )),
    )
}
