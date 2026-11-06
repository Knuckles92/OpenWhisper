//! Native preferences. The service owns the schema so new settings cannot silently
//! disappear behind a second, hand-maintained list of native controls.
use crate::state::State;
use crate::ui::{Palette, btn, card, divider, label, page_header};
use rune::{El, TextEdit, col, row};
use rune_kit::{Select, SelectAppearance, TextInput};
use serde_json::{Value, json};

mod collections;
mod credentials;
mod demo;
mod fields;
mod models;
mod system;

pub use demo::{command as settings_demo_command, seed as settings_demo};

/// Reconcile service updates without overwriting an unsaved local edit.
/// Called before State stores the new named result.
pub fn settings_response(s: &mut State, method: &str, result: &Value) {
    if method == "settings.values" {
        for category in list(&s.data["settings.schema"]["categories"]) {
            for field in list(&category["fields"]) {
                let key = string(field, "key");
                let edit_key = format!("settings:field:{key}");
                let old = s.data["settings.values"]["values"]
                    .get(key)
                    .unwrap_or(&field["value"]);
                if let Some(next) = result["values"].get(key) {
                    let old = fields::render_field(field, old);
                    let next = fields::render_field(field, next);
                    if old != next {
                        if let Some(edit) = s.edits.get_mut(&edit_key) {
                            if edit.text == old {
                                edit.replace_text(next);
                            }
                        }
                    }
                }
            }
        }
    } else if method == "credentials.verify" {
        if let (Some(name), Some(operation)) = (
            s.data["settings.credential_request"]
                .as_str()
                .map(str::to_owned),
            result["operation"].as_str(),
        ) {
            if !s.data["settings.credential_operations"].is_object() {
                s.data["settings.credential_operations"] = json!({});
            }
            s.data["settings.credential_operations"][&name] = json!(operation);
        }
    } else if matches!(method, "text_models.catalog" | "speech_servers.catalog") {
        if let (Some(id), Some(operation)) = (
            s.data["settings.catalog_request"][method]
                .as_str()
                .map(str::to_owned),
            result["operation"].as_str(),
        ) {
            if !s.data["settings.catalog_operations"].is_object() {
                s.data["settings.catalog_operations"] = json!({});
            }
            s.data["settings.catalog_operations"][&format!("{method}:{id}")] = json!(operation);
        }
    } else if method == "credentials.set" {
        if let Some(name) = result["name"].as_str() {
            s.edits.remove(&format!("settings:credential:{name}"));
        }
    } else if matches!(method, "collections.secret.copy" | "mcp.copy")
        && result["ok"].as_bool() == Some(true)
    {
        s.notice = Some("Copied to clipboard".into());
    }
}

#[cfg(test)]
mod tests;

fn string<'a>(value: &'a Value, key: &str) -> &'a str {
    value[key].as_str().unwrap_or("")
}
fn list(value: &Value) -> &[Value] {
    value.as_array().map(Vec::as_slice).unwrap_or(&[])
}
fn edit(s: &State, key: &str, initial: &str) -> TextEdit {
    s.edits
        .get(key)
        .cloned()
        .unwrap_or_else(|| TextEdit::new(initial))
}
fn edit_mut<'a>(s: &'a mut State, key: &str, initial: &str) -> &'a mut TextEdit {
    s.edits
        .entry(key.to_owned())
        .or_insert_with(|| TextEdit::new(initial))
}
fn editable(s: &State) -> bool {
    s.available() && !s.working()
}
fn select_tab(s: &mut State, tab: String) {
    s.selected_tab = tab.clone();
    s.selected_item = None;
    s.confirmation = None;
    s.open_select = None;
    if let Some(id) = tab.strip_prefix("settings:collection:") {
        s.command("collections.list", json!({"collection":id}));
        s.command("collections.tests", json!({}));
        s.command("operations.list", json!({}));
    } else if tab == "settings:models" {
        s.command("models.list", json!({}));
        s.command("components.list", json!({}));
    } else if tab == "settings:credentials" {
        s.command("operations.list", json!({}));
    } else if let Some(id) = tab.strip_prefix("settings:system:") {
        if let Some((_, _, method)) = system::SECTIONS.iter().find(|(key, _, _)| *key == id) {
            s.command(method, json!({}));
        }
    }
}

pub fn settings(s: &State, p: &Palette, layer: &mut Vec<El>) -> El {
    let schema = &s.data["settings.schema"];
    let categories = list(&schema["categories"]);
    let collections = if schema["collections"].is_array() {
        list(&schema["collections"])
    } else {
        list(&s.data["collections.schema"]["collections"])
    };
    let search = edit(s, "settings:search", "");
    let query = search.text.trim().to_lowercase();
    let tab = if s.selected_tab.starts_with("settings:") {
        s.selected_tab.as_str()
    } else {
        "settings:overview"
    };
    let mut tabs = row()
        .gap(6.)
        .flex_wrap()
        .key("settings-tabs")
        .role("group")
        .label("Settings sections");
    for (id, title) in [
        ("overview", "Overview"),
        ("models", "Models"),
        ("credentials", "Provider keys"),
    ] {
        let key = format!("settings:{id}");
        let action_key = key.clone();
        tabs = tabs.child(
            btn(
                format!("tab-{key}"),
                title,
                p,
                tab == key,
                true,
                move |s, _| select_tab(s, action_key.clone()),
            )
            .h(31.)
            .px(11.),
        );
    }
    if !categories.is_empty() || !collections.is_empty() {
        let options: Vec<_> = system::SECTIONS
            .iter()
            .map(|(_, title, _)| ("Workspace", *title))
            .chain(
                categories
                    .iter()
                    .map(|c| ("Preferences", string(c, "label")))
                    .chain(
                        collections
                            .iter()
                            .map(|c| ("Libraries & automation", string(c, "label"))),
                    ),
            )
            .collect();
        let destinations: Vec<_> = system::SECTIONS
            .iter()
            .map(|(id, title, _)| ((*title).to_owned(), format!("settings:system:{id}")))
            .chain(
                categories
                    .iter()
                    .map(|c| {
                        (
                            string(c, "label").to_owned(),
                            format!("settings:category:{}", string(c, "id")),
                        )
                    })
                    .chain(collections.iter().map(|c| {
                        (
                            string(c, "label").to_owned(),
                            format!("settings:collection:{}", string(c, "id")),
                        )
                    })),
            )
            .collect();
        let current = destinations
            .iter()
            .find(|(_, key)| key == tab)
            .map(|(title, _)| title.as_str());
        tabs = tabs.child(
            Select::grouped("settings-section-picker", &options)
                .label("All settings and libraries")
                .placeholder("All settings & libraries…")
                .appearance(SelectAppearance::Field)
                .width(260.)
                .current(current)
                .open(s.open_select.as_deref() == Some("settings:sections"))
                .on_toggle(|s: &mut State| {
                    s.open_select = if s.open_select.as_deref() == Some("settings:sections") {
                        None
                    } else {
                        Some("settings:sections".into())
                    };
                })
                .on_close(|s: &mut State| s.open_select = None)
                .on_choose(move |s: &mut State, title, _| {
                    if let Some((_, destination)) =
                        destinations.iter().find(|(label, _)| label == title)
                    {
                        select_tab(s, destination.clone());
                    }
                })
                .build(&p.kit, layer),
        );
    }
    let mut body = col()
        .key("native-settings")
        .min_w(0.)
        .gap(20.)
        .child(page_header(
            "Make it yours.",
            "Your voice, your tools, your way of working.",
            p,
        ))
        .child(
            row()
                .gap(10.)
                .center_y()
                .child(
                    TextInput::new(&search)
                        .width(None)
                        .height(38.)
                        .label("Search all settings")
                        .placeholder("Search settings, providers, and shortcuts…")
                        .on_key(|s: &mut State, e, tx| crate::ui::suppress_hotkey(s, e, tx))
                        .on_edit(|s: &mut State| s.edit("settings:search"))
                        .build(&p.kit)
                        .key("settings-search")
                        .grow(1.)
                        .min_w(0.),
                )
                .child(
                    btn(
                        "settings-refresh",
                        "Refresh",
                        p,
                        false,
                        s.available(),
                        |s, _| {
                            s.command("settings.schema", json!({}));
                            s.command("settings.values", json!({}));
                            s.command("models.list", json!({}));
                            s.command("devices.list", json!({}));
                        },
                    )
                    .h(38.),
                ),
        )
        .child(tabs);
    if !query.is_empty() {
        let mut found = 0;
        for category in categories {
            let matches: Vec<_> = list(&category["fields"])
                .iter()
                .filter(|f| fields::matches(f, &query))
                .collect();
            if matches.is_empty() {
                continue;
            }
            let mut group = card(p)
                .key(format!("search-category-{}", string(category, "id")))
                .min_w(0.)
                .child(label(string(category, "label"), 16., 550., p.ink));
            for (i, field) in matches.iter().enumerate() {
                if i > 0 {
                    group = group.child(divider(p));
                }
                group = group.child(fields::setting(s, p, field, layer));
                found += 1;
            }
            body = body.child(group);
        }
        if found == 0 {
            body = body.child(
                card(p)
                    .child(label("No settings found", 16., 550., p.ink))
                    .child(label(
                        "Try a provider name, “hotkey”, “language”, or a setting name.",
                        12.,
                        400.,
                        p.muted,
                    ))
                    .child(btn(
                        "settings-clear-search",
                        "Clear search",
                        p,
                        false,
                        true,
                        |s, _| {
                            s.edits.remove("settings:search");
                        },
                    )),
            );
        } else {
            body = body.child(label(
                format!("{found} matching settings"),
                11.,
                400.,
                p.muted,
            ));
        }
    } else if tab == "settings:models" {
        body = body.child(models::catalog(s, p));
    } else if tab == "settings:credentials" {
        body = body.child(credentials::credentials(s, p));
    } else if let Some(id) = tab.strip_prefix("settings:system:") {
        body = body.child(system::panel(s, p, id, layer));
    } else if let Some(id) = tab.strip_prefix("settings:collection:") {
        if let Some(schema) = collections.iter().find(|v| string(v, "id") == id) {
            body = body.child(collections::collection(s, p, schema, layer));
        }
    } else if let Some(id) = tab.strip_prefix("settings:category:") {
        if let Some(category) = categories.iter().find(|v| string(v, "id") == id) {
            body = body.child(category_panel(s, p, category, layer));
        } else {
            body = body.child(loading(s, p));
        }
    } else {
        body = body.child(overview(s, p, categories, layer));
    }
    fields::capture_dialog(s, p, layer);
    body.child(label(
        format!("OpenWhisper {}  ·  Rune native desktop", s.snapshot.version),
        10.5,
        400.,
        p.muted,
    ))
}

fn loading(s: &State, p: &Palette) -> El {
    card(p)
        .child(label(
            if s.available() {
                "Loading your preferences…"
            } else {
                "Connecting to your workspace…"
            },
            16.,
            550.,
            p.ink,
        ))
        .child(label(
            "Your preferences will appear when the local audio service is ready.",
            12.,
            400.,
            p.muted,
        ))
        .child(btn(
            "settings-load",
            "Load preferences",
            p,
            false,
            s.available(),
            |s, _| {
                s.command("settings.schema", json!({}));
                s.command("settings.values", json!({}));
            },
        ))
}
fn category_panel(s: &State, p: &Palette, category: &Value, layer: &mut Vec<El>) -> El {
    let mut group = card(p)
        .key(format!("settings-category-{}", string(category, "id")))
        .min_w(0.)
        .child(label(string(category, "label"), 17., 550., p.ink));
    if !string(category, "description").is_empty() {
        group = group.child(label(string(category, "description"), 12., 400., p.muted));
    }
    if string(category, "id").contains("microphone") || string(category, "id") == "audio" {
        group = group.child(device_list(s, p));
    }
    for (i, field) in list(&category["fields"]).iter().enumerate() {
        if i > 0 {
            group = group.child(divider(p));
        }
        group = group.child(fields::setting(s, p, field, layer));
    }
    group
}
fn device_list(s: &State, p: &Palette) -> El {
    let devices = list(&s.data["devices.list"]["items"]);
    let mut group = col()
        .gap(8.)
        .min_w(0.)
        .child(label("Available microphones", 12., 550., p.ink));
    let priority = list(&s.data["devices.list"]["priority"]);
    for device in devices {
        let id = device["id"].clone();
        let selected = device["id"] == s.data["devices.list"]["selected"]
            && (device["key"].is_object() || priority.is_empty());
        let preferred = device["key"].clone();
        let mut preferred_priority = priority.to_vec();
        preferred_priority.retain(|v| v != &preferred);
        if preferred.is_object() {
            preferred_priority.insert(0, preferred.clone());
        }
        let mut actions = row().gap(6.).flex_wrap().child(
            btn(
                format!("select-device-{id}"),
                if selected {
                    "Selected"
                } else {
                    "Use microphone"
                },
                p,
                false,
                editable(s) && !selected,
                move |s, _| {
                    if preferred.is_object() {
                        s.command("devices.priority", json!({"priority":preferred_priority}));
                    } else {
                        s.command("devices.select", json!({"id":id}));
                    }
                },
            )
            .h(30.),
        );
        let backup = device["key"].clone();
        if backup.is_object() && !priority.is_empty() && !priority.contains(&backup) {
            let mut backup_priority = priority.to_vec();
            backup_priority.push(backup);
            actions = actions.child(
                btn(
                    format!("backup-device-{}", device["id"]),
                    "Add backup",
                    p,
                    false,
                    editable(s),
                    move |s, _| s.command("devices.priority", json!({"priority":backup_priority})),
                )
                .h(30.),
            );
        }
        group = group.child(
            row()
                .min_w(0.)
                .gap(10.)
                .center_y()
                .child(
                    label(
                        format!(
                            "{}{}",
                            string(device, "name"),
                            if device["default"].as_bool() == Some(true)
                                && !string(device, "name").to_lowercase().contains("default")
                            {
                                " · System default"
                            } else {
                                ""
                            }
                        ),
                        11.,
                        400.,
                        p.muted,
                    )
                    .grow(1.)
                    .min_w(0.),
                )
                .child(actions),
        );
    }
    if !priority.is_empty() {
        group=group.child(divider(p)).child(label("Microphone fallback order",12.,550.,p.ink))
            .child(label("Your preferred microphone is tried first, then your backups, then the system default. Unplugged devices stay in this list for next time.",11.,400.,p.muted));
        for (index, entry) in priority.iter().enumerate() {
            let available = devices.iter().any(|v| v["key"] == *entry);
            let mut controls = row().gap(6.).flex_wrap();
            if index > 1 {
                let mut moved = priority.to_vec();
                moved.swap(index, index - 1);
                controls = controls.child(
                    btn(
                        format!("microphone-backup-up-{index}"),
                        "Move up",
                        p,
                        false,
                        editable(s),
                        move |s, _| s.command("devices.priority", json!({"priority":moved})),
                    )
                    .h(28.),
                );
            }
            if index > 0 && index + 1 < priority.len() {
                let mut moved = priority.to_vec();
                moved.swap(index, index + 1);
                controls = controls.child(
                    btn(
                        format!("microphone-backup-down-{index}"),
                        "Move down",
                        p,
                        false,
                        editable(s),
                        move |s, _| s.command("devices.priority", json!({"priority":moved})),
                    )
                    .h(28.),
                );
            }
            if index > 0 {
                let mut remaining = priority.to_vec();
                remaining.remove(index);
                controls = controls.child(
                    btn(
                        format!("microphone-backup-remove-{index}"),
                        "Remove backup",
                        p,
                        false,
                        editable(s),
                        move |s, _| s.command("devices.priority", json!({"priority":remaining})),
                    )
                    .h(28.),
                );
            }
            group = group.child(
                row()
                    .gap(10.)
                    .center_y()
                    .flex_wrap()
                    .child(
                        label(
                            format!(
                                "{}. {}{}{}",
                                index + 1,
                                string(entry, "name"),
                                if index == 0 { " · Preferred" } else { "" },
                                if available { "" } else { " · Disconnected" }
                            ),
                            11.,
                            400.,
                            p.muted,
                        )
                        .grow(1.)
                        .min_w(0.),
                    )
                    .child(controls),
            );
        }
        group = group.child(label(
            format!("{}. System default", priority.len() + 1),
            11.,
            400.,
            p.muted,
        ));
    }
    group.child(
        btn(
            "refresh-input-devices",
            "Scan microphones",
            p,
            false,
            s.available(),
            |s, _| s.command("devices.list", json!({})),
        )
        .h(31.),
    )
}
fn overview(s: &State, p: &Palette, categories: &[Value], layer: &mut Vec<El>) -> El {
    let mut content = col().gap(20.).min_w(0.)
        .child(card(p).key("settings-appearance").child(label("A quieter kind of workspace",16.,550.,p.ink))
            .child(label("Pick the light that helps you think.",12.,400.,p.muted))
            .child(row().gap(8.).flex_wrap()
                .child(btn("appearance-light","Daylight",p,!s.dark,true,|s,_|s.set_dark(false)))
                .child(btn("appearance-dark","After hours",p,s.dark,true,|s,_|s.set_dark(true)))))
        .child(card(p).key("settings-model-summary")
            .child(label("A voice that feels familiar",16.,550.,p.ink))
            .child(label(format!("Current voice engine: {}",crate::ui::engine(&s.snapshot.engine)),12.,400.,p.muted))
            .child(label("Choose local speech models, manage downloads, and connect your preferred providers.",12.,400.,p.muted))
            .child(btn("settings-browse-models","Explore voice models →",p,false,true,|s,_|select_tab(s,"settings:models".into()))));
    content = content.child(card(p).key("settings-microphones").child(device_list(s, p)));
    if categories.is_empty() {
        return content.child(loading(s, p));
    }
    let preferred = [
        "auto_paste",
        "copy_clipboard",
        "transcript_cleanup_enabled",
        "local_asr_language",
    ];
    let mut quick = card(p).key("settings-output").min_w(0.).child(label(
        "Everyday essentials",
        16.,
        550.,
        p.ink,
    ));
    let mut count = 0;
    for key in preferred {
        if let Some(field) = categories
            .iter()
            .flat_map(|c| list(&c["fields"]))
            .find(|f| string(f, "key") == key)
        {
            if count > 0 {
                quick = quick.child(divider(p));
            }
            quick = quick.child(fields::setting(s, p, field, layer));
            count += 1;
        }
    }
    if count > 0 {
        content = content.child(quick);
    }
    content.child(label("All preferences are available in the sections above. Changes are saved to your existing OpenWhisper configuration.",11.,400.,p.muted))
}
