use super::*;
use crate::state::Page;
use rune::{FileDialogOptions, TextEdit, Tx};
use rune_kit::TextArea;
use serde_json::Value;

fn selector(s: &State, id: &str) -> Value {
    let selection = &s.data["walkthrough.selection"];
    if selection["id"].as_str() == Some(id) {
        json!({"id":id,"spoken":selection["spoken"].as_bool().unwrap_or(false),"version":selection["version"]})
    } else {
        let d = &s.data["walkthroughs.detail"];
        json!({"id":id,"spoken":d["spoken"].as_bool().unwrap_or(!d["written_up"].as_bool().unwrap_or(false)),"version":d["version"]})
    }
}
fn edit_key(id: &str, version: &Value) -> String {
    format!(
        "walkthrough:{id}:write-up:{}",
        version
            .as_u64()
            .map(|v| v.to_string())
            .unwrap_or_else(|| "latest".into())
    )
}
fn writeup_editing(s: &State, id: &str) -> bool {
    s.data["walkthrough.writeup.editor"]["id"].as_str() == Some(id)
}
pub(super) fn dirty(s: &State, id: &str) -> bool {
    if writeup_editing(s, id) {
        let editor = &s.data["walkthrough.writeup.editor"];
        let key = edit_key(id, &editor["version"]);
        if s.edits
            .get(&key)
            .is_some_and(|edit| edit.text != value(editor, "original", ""))
        {
            return true;
        }
    }
    if s.data["walkthrough.review.draft"]["id"].as_str() != Some(id) {
        return false;
    }
    let draft = &s.data["walkthrough.review.draft"];
    s.data["walkthroughs.detail"]["steps"]
        .as_array()
        .into_iter()
        .flatten()
        .enumerate()
        .any(|(i, step)| {
            draft["order"][i].as_u64() != Some(i as u64)
                || draft["keep"][i].as_bool().unwrap_or(true)
                    != step["included"].as_bool().unwrap_or(true)
                || s.edits
                    .get(&format!("walkthrough-edit-step-{i}"))
                    .is_some_and(|edit| edit.text != value(step, "text", ""))
        })
}
fn clear_drafts(s: &mut State, id: &str) {
    let prefix = format!("walkthrough:{id}:write-up:");
    s.edits.retain(|key, _| !key.starts_with(&prefix));
    s.data["walkthrough.writeup.editor"] = Value::Null;
    s.data["walkthrough.review.draft"] = Value::Null;
}
fn apply_action(s: &mut State, action: &Value) {
    match value(action, "kind", "") {
        "close" => {
            s.selected_item = None;
            s.selected_tab.clear();
        }
        "tab" => s.selected_tab = value(action, "tab", "walkthrough-steps").into(),
        "select" => {
            s.data["walkthrough.gallery.page"] = json!(0);
            s.data["walkthrough.selection"] = action["selection"].clone();
            s.command("walkthroughs.detail", action["selection"].clone());
        }
        "open" => {
            s.navigate(Page::Walkthrough);
            s.data["walkthrough.gallery.page"] = json!(0);
            s.selected_item = action["id"].as_str().map(str::to_owned);
            s.selected_tab = "walkthrough-document".into();
            s.command("walkthroughs.detail", json!({"id":action["id"]}));
        }
        "navigate" => {
            let page = match value(action, "page", "") {
                "record" => Page::Record,
                "files" => Page::Files,
                "meetings" => Page::Meetings,
                "history" => Page::History,
                "settings" => Page::Settings,
                "scratchpad" => Page::Scratchpad,
                _ => Page::Walkthrough,
            };
            s.navigate(page);
        }
        _ => {}
    }
}
pub(super) fn request_action(s: &mut State, action: Value) {
    if s.selected_item.clone().is_some_and(|id| dirty(s, &id)) {
        s.data["walkthrough.unsaved.action"] = action;
        s.confirmation = Some(json!({"scope":"walkthrough-unsaved"}));
    } else {
        if let Some(id) = s.selected_item.clone() {
            clear_drafts(s, &id);
        }
        apply_action(s, &action);
    }
}
pub fn walkthrough_block_navigation(s: &mut State, target: Page) -> bool {
    if s.page == Page::Walkthrough
        && target != Page::Walkthrough
        && s.selected_item.clone().is_some_and(|id| dirty(s, &id))
    {
        request_action(s, json!({"kind":"navigate","page":target.key()}));
        true
    } else {
        false
    }
}
pub(super) fn unsaved_confirmation(s: &State, p: &Palette) -> El {
    if s.confirmation
        .as_ref()
        .is_none_or(|c| c["scope"] != "walkthrough-unsaved")
    {
        return el();
    }
    card(p)
        .key("walkthrough-unsaved-confirmation")
        .fill(p.accent_soft)
        .child(label("Keep the changes you made?", 16., 550., p.ink))
        .child(label(
            "Your edits are still here. Save them before leaving, or discard this draft.",
            12.,
            400.,
            p.muted,
        ))
        .child(
            row()
                .gap(8.)
                .flex_wrap()
                .child(btn(
                    "walkthrough-keep-editing",
                    "Keep editing",
                    p,
                    true,
                    true,
                    |s, _| {
                        s.confirmation = None;
                        s.data["walkthrough.unsaved.action"] = Value::Null;
                    },
                ))
                .child(btn(
                    "walkthrough-discard-changes",
                    "Discard changes",
                    p,
                    false,
                    !s.pending,
                    |s, _| {
                        let action = s.data["walkthrough.unsaved.action"].clone();
                        if let Some(id) = s.selected_item.clone() {
                            clear_drafts(s, &id);
                        }
                        s.confirmation = None;
                        s.data["walkthrough.unsaved.action"] = Value::Null;
                        apply_action(s, &action);
                    },
                )),
        )
}
fn operation<'a>(s: &'a State, id: &str) -> &'a Value {
    let job = &s.data["walkthrough.operations"][id];
    if job.is_object() {
        job
    } else {
        &s.data["walkthroughs.detail"]["operation"]
    }
}
pub(super) fn busy(s: &State, id: &str) -> bool {
    library_busy(s)
        || matches!(
            value(operation(s, id), "status", ""),
            "running" | "canceling"
        )
}
pub(super) fn library_busy(s: &State) -> bool {
    s.data["walkthroughs.busy"]["busy"]
        .as_bool()
        .unwrap_or_else(|| {
            s.data["snapshot"]["walkthrough_library_busy"]
                .as_bool()
                .unwrap_or(false)
        })
}
pub(super) fn job_status(s: &State, p: &Palette, id: &str) -> El {
    let job = operation(s, id);
    let status = value(job, "status", "");
    if status.is_empty() {
        return el();
    }
    let kind = value(job, "kind", "operation");
    let description = match (kind, status) {
        (_, "failed") => value(
            job,
            "error",
            "This operation could not finish. Your saved documents and edits are still available.",
        ),
        (_, "canceled") => "Stopped safely. Your saved documents are unchanged.",
        ("retry", _) | ("write_up.retry", _) => {
            "Writing a new version. Your earlier write-ups stay available."
        }
        ("recover", _) => "Recovering the saved audio and screenshots…",
        ("retention", _) => "Removing older completed walkthroughs…",
        (_, "running") => "Saving and rebuilding the documents…",
        (_, "canceling") => "Stopping safely…",
        _ => return el(),
    };
    let mut panel = card(p)
        .key("walkthrough-operation")
        .fill(p.soft)
        .child(label(
            description,
            12.,
            450.,
            if status == "failed" { p.danger } else { p.ink },
        ));
    if status == "running"
        && (job["cancelable"] == true || ["retry", "write_up.retry"].contains(&kind))
    {
        let id = id.to_string();
        panel = panel.child(btn(
            "walkthrough-cancel-writeup",
            if kind == "recover" {
                "Cancel recovery"
            } else {
                "Cancel write-up"
            },
            p,
            false,
            !s.pending,
            move |s, _| s.command("walkthroughs.write_up.cancel", json!({"id":id})),
        ));
    }
    panel
}
pub(super) fn has_job_status(s: &State, id: &str) -> bool {
    matches!(
        value(operation(s, id), "status", ""),
        "running" | "canceling" | "failed" | "canceled"
    )
}
pub(super) fn version_picker(s: &State, p: &Palette, id: &str, detail: &Value) -> El {
    let selection = selector(s, id);
    let editing = dirty(s, id) || busy(s, id) || s.pending;
    let mut choices = row().gap(7.).flex_wrap();
    if let Some(versions) = detail["versions"].as_array() {
        for (i, version) in versions.iter().enumerate() {
            let mut selected = json!({"id":id,"spoken":version["spoken"].as_bool().unwrap_or(false),"version":version["version"]});
            let active = selected["spoken"] == selection["spoken"]
                && selected["version"] == selection["version"];
            // Selectors always name an explicit source; a missing version means latest.
            selected["version"] = version["version"].clone();
            choices = choices.child(btn(
                format!("walkthrough-version-{i}"),
                value(version, "label", "Write-up"),
                p,
                active,
                !editing,
                move |s, _| request_action(s, json!({"kind":"select","selection":selected})),
            ));
        }
    } else {
        choices = choices.child(label(
            if selection["spoken"] == true {
                "As spoken"
            } else {
                "Latest write-up"
            },
            11.,
            500.,
            p.muted,
        ));
    }
    col()
        .gap(8.)
        .child(eyebrow("DOCUMENT VERSION", p))
        .child(choices)
}
pub(super) fn export_version(s: &mut State, tx: &mut Tx, id: String, format: &'static str) {
    let mut params = selector(s, &id);
    let options = FileDialogOptions {
        title: "Export walkthrough".into(),
        file_name: Some(format!("openwhisper.{format}")),
        ..Default::default()
    }
    .filter("Walkthrough document", &[format]);
    tx.save_file(options, move |s: &mut State, result, _| match result {
        Ok(Some(path)) => {
            params["format"] = json!(format);
            params["path"] = json!(path.to_string_lossy());
            s.command("walkthroughs.export", params.clone());
        }
        Ok(None) => {}
        Err(e) => s.error = Some(e.to_string()),
    });
}
pub(super) fn document_actions(s: &State, p: &Palette, id: &str, detail: &Value) -> El {
    let expanded = s.data["walkthrough.document.actions.open"] == true;
    let expander = btn(
        "walkthrough-more-document-actions",
        if expanded {
            "Fewer document actions"
        } else {
            "More document actions…"
        },
        p,
        false,
        true,
        move |s, _| s.data["walkthrough.document.actions.open"] = json!(!expanded),
    );
    if !expanded {
        return row().child(expander);
    }
    let enabled = !s.pending && !busy(s, id) && !dirty(s, id) && detail["status"] != "draft";
    let mut actions = row().gap(7.).flex_wrap();
    for (format, title) in [
        ("pdf", "Copy PDF"),
        ("md", "Copy Markdown file"),
        ("docx", "Copy Word"),
    ] {
        let id = id.to_string();
        actions = actions.child(btn(
            format!("walkthrough-copy-{format}"),
            title,
            p,
            false,
            enabled && (format == "md" || detail[format!("{format}_path")].is_string()),
            move |s, _| {
                let mut params = selector(s, &id);
                params["format"] = json!(format);
                s.command("walkthroughs.copy", params);
            },
        ));
    }
    for (field, title) in [
        ("pdf_path", "Open PDF"),
        ("docx_path", "Open Word"),
        ("folder_path", "Show folder"),
    ] {
        let path = value(detail, field, "").to_string();
        actions = actions.child(btn(
            format!("walkthrough-open-{field}"),
            title,
            p,
            false,
            !path.is_empty(),
            move |s, _| {
                if let Err(error) = crate::platform::open_path(&path) {
                    s.error = Some(error);
                }
            },
        ));
    }
    col().gap(10.).child(row().child(expander)).child(actions)
}
pub(super) fn edit_writeup(s: &mut State, id: &str) {
    let selection = selector(s, id);
    let key = edit_key(id, &selection["version"]);
    let markdown = value(&s.data["walkthroughs.detail"], "markdown", "").to_string();
    if !writeup_editing(s, id) {
        s.edits.insert(key, TextEdit::new(markdown.clone()));
        s.data["walkthrough.writeup.editor"] =
            json!({"id":id,"version":selection["version"],"original":markdown});
    }
    s.selected_tab = "walkthrough-writeup-edit".into();
}
fn text_editor(s: &State, p: &Palette, key: &str, title: &str) -> El {
    let edit = s.edits.get(key).cloned().unwrap_or_default();
    let bind = key.to_string();
    TextArea::new(&edit)
        .width(None)
        .label(title)
        .auto_grow(8, 20)
        .wrap_anywhere()
        .disabled(s.pending || s.selected_item.as_deref().is_some_and(|id| busy(s, id)))
        .on_key(|s: &mut State, e, tx| ui::suppress_hotkey(s, e, tx))
        .on_edit(move |s: &mut State| s.edit(&bind))
        .build(&p.kit)
        .key(key)
        .min_w(0.)
}
pub(super) fn writeup_editor(s: &State, p: &Palette, id: &str) -> El {
    let editor = &s.data["walkthrough.writeup.editor"];
    let key = edit_key(id, &editor["version"]);
    let save_id = id.to_string();
    let save_key = key.clone();
    let version = editor["version"].clone();
    col().key("walkthrough-writeup-editor").gap(14.)
        .child(label("Edit this write-up",17.,550.,p.ink))
        .child(label("Edit the Markdown, including screenshot lines. Saving rebuilds the PDF and Word document for this version.",12.,400.,p.muted))
        .child(text_editor(s,p,&key,"Write-up Markdown"))
        .child(row().gap(8.).flex_wrap()
            .child(btn("walkthrough-save-writeup","Save write-up",p,true,!s.pending&&!busy(s,id)&&dirty(s,id),move|s,_|{
                let markdown=s.edit(&save_key).text.clone();
                s.command("walkthroughs.write_up.save",json!({"id":save_id,"spoken":false,"version":version,"markdown":markdown}));
            }))
            .child(btn("walkthrough-cancel-writeup-edit","Cancel editing",p,false,!s.pending&&!busy(s,id),|s,_|request_action(s,json!({"kind":"tab","tab":"walkthrough-document"})))))
}
pub(super) fn retry_controls(s: &State, p: &Palette, id: &str, detail: &Value) -> El {
    if detail["status"] == "draft" {
        return el();
    }
    let enabled = !s.pending
        && !busy(s, id)
        && !dirty(s, id)
        && detail["can_retry"].as_bool().unwrap_or(true);
    let written = detail["written_up"].as_bool().unwrap_or(false);
    let mut panel = col().key("walkthrough-retry-controls").gap(12.);
    let id_default = id.to_string();
    panel = panel.child(
        row()
            .gap(8.)
            .flex_wrap()
            .child(btn(
                "walkthrough-retry-default",
                if written {
                    "Redo AI write-up"
                } else {
                    "Write up with AI"
                },
                p,
                false,
                enabled,
                move |s, _| s.command("walkthroughs.write_up.retry", json!({"id":id_default})),
            ))
            .child(btn(
                "walkthrough-retry-options",
                "Choose style or prompt…",
                p,
                false,
                enabled,
                |s, _| {
                    s.data["walkthrough.retry.open"] = json!(true);
                    let configured = value(
                        &s.data["settings.values"]["values"],
                        "walkthrough_write_up_instructions",
                        "",
                    )
                    .to_string();
                    let prompt = if configured.trim().is_empty() {
                        value(
                            &s.data["walkthroughs.detail"]["styles"][0],
                            "instructions",
                            "",
                        )
                        .to_string()
                    } else {
                        configured
                    };
                    s.edits
                        .entry("walkthrough-retry-prompt".into())
                        .or_insert_with(|| TextEdit::new(prompt));
                    s.command("walkthroughs.styles", json!({}));
                },
            )),
    );
    if dirty(s, id) {
        panel = panel.child(label(
            "Save or discard your edits before copying, exporting, or writing a new version.",
            11.,
            400.,
            p.muted,
        ));
    }
    if s.data["walkthrough.retry.open"] != true {
        return panel;
    }
    let styles = if s.data["walkthroughs.styles"]["items"].is_array() {
        &s.data["walkthroughs.styles"]["items"]
    } else {
        &detail["styles"]
    };
    let mut choices = row().gap(7.).flex_wrap();
    for (index, style) in styles.as_array().into_iter().flatten().enumerate() {
        let instructions = value(style, "instructions", "").to_string();
        choices = choices.child(btn(
            format!("walkthrough-retry-style-{index}"),
            value(style, "name", "Style"),
            p,
            false,
            enabled,
            move |s, _| {
                s.edit("walkthrough-retry-prompt").text = instructions.clone();
            },
        ));
    }
    let id = id.to_string();
    panel.child(card(p).key("walkthrough-retry-options-panel").fill(p.soft)
        .child(label("Shape the next write-up",16.,550.,p.ink)).child(choices)
        .child(link("walkthrough-manage-styles","Manage saved styles",p,|s,_|{
            s.navigate(Page::Settings);
            if s.page==Page::Settings{s.selected_tab="settings:collection:write_up_styles".into();s.command("collections.list",json!({"collection":"write_up_styles"}));}
        }))
        .child(text_editor(s,p,"walkthrough-retry-prompt","Instructions for this write-up"))
        .child(label("This creates a new version from the spoken walkthrough. Your earlier write-ups remain available.",11.,400.,p.muted))
        .child(row().gap(8.).flex_wrap()
            .child(btn("walkthrough-retry-run","Write a new version",p,true,enabled&&s.edits.get("walkthrough-retry-prompt").is_some_and(|e|!e.text.trim().is_empty()),move|s,_|{
                let instructions=s.edit("walkthrough-retry-prompt").text.clone();
                s.command("walkthroughs.write_up.retry",json!({"id":id,"instructions":instructions}));
            }))
            .child(btn("walkthrough-retry-close","Cancel",p,false,true,|s,_|s.data["walkthrough.retry.open"]=json!(false)))))
}
pub(super) fn draft_recovery(s: &State, p: &Palette, id: &str, detail: &Value) -> El {
    if detail["status"] != "draft" && detail["is_draft"] != true && detail["can_recover"] != true {
        return if !value(detail, "error", "").is_empty() {
            card(p)
                .fill(p.accent_soft)
                .child(label("This walkthrough needs attention", 16., 550., p.ink))
                .child(label(value(detail, "error", ""), 12., 400., p.danger))
        } else {
            el()
        };
    }
    let can = detail["can_recover"].as_bool().unwrap_or(false);
    let id = id.to_string();
    card(p).key("walkthrough-draft-recovery").fill(p.accent_soft)
        .child(label("This walkthrough was interrupted",17.,550.,p.ink))
        .child(label(if can{"Your audio and screenshots were saved. Recover them into a document."}else{"Your screenshots are kept. The audio is not available yet; restarting OpenWhisper finishes audio recovery."},12.,400.,p.muted))
        .child(btn("walkthrough-recover","Recover walkthrough",p,true,can&&!s.pending&&!s.working(),move|s,_|s.command("walkthroughs.recover",json!({"id":id}))))
}
pub(super) fn retention(s: &State, p: &Palette) -> El {
    let open = s.data["walkthrough.retention.open"] == true;
    let mut panel = col().gap(12.).child(btn(
        "walkthrough-retention",
        "Keep walkthroughs…",
        p,
        false,
        !s.pending,
        |s, _| {
            s.data["walkthrough.retention.open"] = json!(true);
            s.command("walkthroughs.retention", json!({}));
        },
    ));
    if !open {
        return panel;
    }
    let days = s.data["walkthroughs.retention"]["days"]
        .as_u64()
        .unwrap_or(0);
    let mut choices = row().gap(7.).flex_wrap();
    for (n, title) in [
        (0, "Forever"),
        (7, "7 days"),
        (30, "30 days"),
        (90, "90 days"),
        (365, "One year"),
    ] {
        choices = choices.child(btn(
            format!("walkthrough-keep-{n}"),
            title,
            p,
            days == n,
            !s.pending,
            move |s, _| {
                if n == 0 {
                    s.command("walkthroughs.retention", json!({"days":0,"prune":false}));
                } else {
                    s.confirmation = Some(json!({"scope":"walkthrough-retention","days":n}));
                }
            },
        ));
    }
    panel = panel.child(
        card(p)
            .key("walkthrough-retention-options")
            .fill(p.soft)
            .child(label("Keep completed walkthroughs", 16., 550., p.ink))
            .child(choices)
            .child(label(
                "Interrupted drafts and standalone screenshots are always kept.",
                11.,
                400.,
                p.muted,
            ))
            .child(btn(
                "walkthrough-retention-close",
                "Done",
                p,
                false,
                true,
                |s, _| s.data["walkthrough.retention.open"] = json!(false),
            )),
    );
    if let Some(c) = s
        .confirmation
        .as_ref()
        .filter(|c| c["scope"] == "walkthrough-retention")
    {
        let days = c["days"].as_u64().unwrap_or(0);
        panel=panel.child(card(p).key("walkthrough-retention-confirmation").fill(p.accent_soft)
            .child(label(format!("Delete completed walkthroughs older than {days} days?"),16.,550.,p.ink))
            .child(label("This applies now and at each startup. Interrupted drafts and standalone screenshots are kept.",12.,400.,p.muted))
            .child(row().gap(8.).flex_wrap()
                .child(btn("walkthrough-retention-apply","Apply and delete older walkthroughs",p,false,!s.pending,move|s,_|s.command("walkthroughs.retention",json!({"days":days,"prune":true,"confirmed":true}))))
                .child(btn("walkthrough-retention-cancel","Keep current setting",p,true,true,|s,_|s.confirmation=None))));
    }
    panel.child(job_status(s, p, "retention"))
}

pub fn walkthrough_response(s: &mut State, method: &str, result: &Value) {
    if !method.starts_with("walkthroughs.") {
        return;
    }
    if method == "walkthroughs.detail" {
        s.data["walkthrough.selection"] = json!({"id":result["id"],"spoken":result["spoken"].as_bool().unwrap_or(!result["written_up"].as_bool().unwrap_or(false)),"version":result["version"]});
    }
    if result["operation"].is_object() {
        if let Some(id) = result["operation"]["id"].as_str() {
            let previous = &s.data["walkthrough.operations"][id];
            let terminal = previous["job_id"] == result["operation"]["job_id"]
                && matches!(
                    value(previous, "status", ""),
                    "done" | "failed" | "canceled"
                );
            if !terminal {
                s.data["walkthrough.operations"][id] = result["operation"].clone();
            }
        }
    }
    if method == "walkthroughs.retention" && result["days"].is_number() {
        if result["requires_confirmation"] == true {
            s.confirmation =
                Some(json!({"scope":"walkthrough-retention","days":result["requested_days"]}));
            return;
        }
        s.confirmation = None;
        if result["ok"] == true && result["days"] == 0 {
            s.notice = Some("Walkthroughs will be kept until you delete them.".into());
        }
        if result["pruned"].is_number() {
            s.command("walkthroughs.list", json!({}));
            s.notice = Some(format!(
                "Retention saved. {} older walkthroughs removed.",
                result["pruned"]
            ));
        }
    }
    if matches!(method, "walkthroughs.copy" | "walkthroughs.screenshot.copy")
        && result["ok"] == true
    {
        s.notice = Some("Copied to clipboard".into());
    }
}
pub fn walkthrough_event(s: &mut State, event: &str, data: &Value) {
    if event == "navigate" && data["page"] == "walkthroughs" {
        let id = value(data, "folder", "");
        let id = std::path::Path::new(id)
            .file_name()
            .and_then(|p| p.to_str())
            .unwrap_or("");
        if !id.is_empty() {
            request_action(s, json!({"kind":"open","id":id}));
        }
    }
    if event == "walkthroughs.operation" {
        let Some(id) = data["id"].as_str() else {
            return;
        };
        let previous = &s.data["walkthrough.operations"][id];
        if previous["job_id"].is_string()
            && previous["job_id"] != data["job_id"]
            && data["status"] != "running"
        {
            return;
        }
        s.data["walkthrough.operations"][id] = data.clone();
        if data["status"] == "failed" {
            s.error = Some(
                value(
                    data,
                    "error",
                    "The walkthrough could not be updated. Your edits are still here.",
                )
                .into(),
            );
            return;
        }
        if data["status"] != "done" {
            return;
        }
        let selected = s.selected_item.as_deref() == Some(id);
        if selected {
            match value(data, "kind", "") {
                "delete" => {
                    s.selected_item = None;
                    s.confirmation = None;
                    clear_drafts(s, id);
                }
                "review" => {
                    clear_drafts(s, id);
                    s.selected_tab = "walkthrough-steps".into();
                }
                "save" | "write_up.save" => {
                    clear_drafts(s, id);
                    s.selected_tab = "walkthrough-document".into();
                }
                "retry" | "write_up.retry" => {
                    clear_drafts(s, id);
                    s.data["walkthrough.retry.open"] = json!(false);
                    s.selected_tab = "walkthrough-document".into();
                }
                _ => {}
            }
            if data["item"].is_object() {
                s.data["walkthroughs.detail"] = data["item"].clone();
                walkthrough_response(s, "walkthroughs.detail", &data["item"]);
                reload_gallery(s);
            }
        }
        s.command("walkthroughs.list", json!({}));
        if data["kind"] == "retention" {
            s.data["walkthroughs.retention"] = data["result"].clone();
            let removed = data["result"]["removed"].as_array().map_or(0, Vec::len);
            s.notice = Some(format!(
                "Retention saved. {removed} older walkthroughs removed."
            ));
        } else {
            s.notice = Some("Walkthrough saved".into());
        }
    }
    if event == "walkthrough.ready" {
        s.command("walkthroughs.list", json!({}));
        let result = if data["result"].is_object() {
            &data["result"]
        } else {
            data
        };
        let folder = value(result, "folder", "");
        let id = std::path::Path::new(folder)
            .file_name()
            .and_then(|p| p.to_str())
            .unwrap_or("");
        let lost = result["lost_shots"].as_u64().unwrap_or(0);
        let error = value(result, "write_up_error", "");
        s.data["walkthrough.ready"] = json!({"id":id,"result":result});
        let safe = !id.is_empty()
            && !s.selected_item.clone().is_some_and(|id| dirty(s, &id))
            && matches!(s.page, Page::Walkthrough | Page::Record);
        if safe {
            s.navigate(Page::Walkthrough);
            s.selected_item = Some(id.into());
            s.selected_tab = "walkthrough-document".into();
            s.command("walkthroughs.detail", json!({"id":id}));
        }
        let mut messages = vec!["Walkthrough saved and ready to review.".to_string()];
        if !error.is_empty() {
            messages.push(format!("Saved as spoken; write-up failed: {error}"));
        }
        if lost > 0 {
            messages.push(format!("{lost} screenshots could not be recovered."));
        }
        if data["copied"] == false {
            messages
                .push("Automatic copying did not finish; use a copy button to try again.".into());
        }
        s.notice = Some(messages.join(" "));
    }
}

pub(super) fn ready_offer(s: &State, p: &Palette) -> El {
    let ready = &s.data["walkthrough.ready"];
    let id = value(ready, "id", "").to_string();
    if id.is_empty() || s.selected_item.as_deref() == Some(&id) {
        return el();
    }
    card(p)
        .key("walkthrough-ready-offer")
        .fill(p.accent_soft)
        .child(label("Your walkthrough is ready", 16., 550., p.ink))
        .child(label(
            "The audio and screenshots have been turned into a saved document.",
            12.,
            400.,
            p.muted,
        ))
        .child(btn(
            "walkthrough-open-ready",
            "Review saved walkthrough",
            p,
            true,
            true,
            move |s, _| request_action(s, json!({"kind":"open","id":id})),
        ))
}
pub(super) fn has_ready_offer(s: &State) -> bool {
    s.data["walkthrough.ready"]["id"]
        .as_str()
        .is_some_and(|id| !id.is_empty() && s.selected_item.as_deref() != Some(id))
}
