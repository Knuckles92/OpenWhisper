use super::{edit, editable, fields, list, string};
use crate::state::State;
use crate::ui::{Palette, btn, card, divider, label};
use rune::{El, FileDialogOptions, TextEdit, col, row};
use rune_kit::{Switch, TextInput};
use serde_json::{Value, json};

pub(super) fn collection(s: &State, p: &Palette, schema: &Value, layer: &mut Vec<El>) -> El {
    let collection = string(schema, "id");
    let response = &s.data["collections.list"];
    let ready = string(response, "collection") == collection;
    let items = if ready { list(&response["items"]) } else { &[] };
    let fields = if response["fields"].is_array() && ready {
        list(&response["fields"])
    } else {
        list(&schema["fields"])
    };
    let prefix = format!("collection:{collection}:");
    let selected = s
        .selected_item
        .as_deref()
        .and_then(|v| v.strip_prefix(&prefix));
    let search_key = format!("settings:collectionsearch:{collection}");
    let search = edit(s, &search_key, "");
    let query = search.text.trim().to_lowercase();
    let reload_id = collection.to_owned();
    let new_id = collection.to_owned();
    let mut actions = row().gap(8.).flex_wrap();
    if schema["can_add"]
        .as_bool()
        .unwrap_or(collection != "hotkeys")
    {
        actions = actions.child(
            btn(
                format!("new-{collection}"),
                "Add new",
                p,
                true,
                editable(s) && ready,
                move |s, _| select_item(s, &new_id, "__new__"),
            )
            .h(34.),
        );
    }
    actions = actions.child(
        btn(
            format!("refresh-{collection}"),
            "Refresh",
            p,
            false,
            s.available(),
            move |s, _| s.command("collections.list", json!({"collection":reload_id})),
        )
        .h(34.),
    );
    let mut content = col().gap(18.).min_w(0.).child(
        card(p)
            .min_w(0.)
            .child(label(string(schema, "label"), 17., 550., p.ink))
            .child(label(string(schema, "description"), 12., 400., p.muted))
            .child(actions),
    );
    if !ready {
        return content.child(card(p).child(label("Loading your library…", 13., 400., p.muted)));
    }
    if let Some(id) = selected {
        let item = items
            .iter()
            .find(|item| string(item, "id") == id)
            .cloned()
            .unwrap_or(json!({}));
        content = content.child(editor(
            s,
            p,
            collection,
            id,
            &item,
            fields,
            schema["can_delete"].as_bool().unwrap_or(true),
            layer,
        ));
    }
    let bind_key = search_key.clone();
    content = content.child(
        TextInput::new(&search)
            .width(None)
            .height(35.)
            .label(format!("Search {}", string(schema, "label")))
            .placeholder("Filter this library…")
            .on_key(|s: &mut State, e, tx| crate::ui::suppress_hotkey(s, e, tx))
            .on_edit(move |s: &mut State| s.edit(&bind_key))
            .build(&p.kit)
            .key(format!("search-{collection}")),
    );
    let mut shown = 0;
    for item in items
        .iter()
        .filter(|item| query.is_empty() || searchable(item).contains(&query))
    {
        let id = string(item, "id");
        let item_id = id.to_owned();
        let collection_id = collection.to_owned();
        let title = title(item);
        let mut card = card(p)
            .key(format!("item-{collection}-{id}"))
            .min_w(0.)
            .gap(10.)
            .child(label(&title, 13., 550., p.ink));
        if let Some(detail) = summary(item, fields) {
            card = card.child(label(detail, 11., 400., p.muted));
        }
        card = card.child(
            btn(
                format!("edit-{collection}-{id}"),
                "Edit",
                p,
                selected == Some(id),
                true,
                move |s, _| select_item(s, &collection_id, &item_id),
            )
            .h(30.),
        );
        content = content.child(card);
        shown += 1;
    }
    if shown == 0 {
        content = content.child(
            card(p)
                .child(label(
                    if query.is_empty() {
                        "A little personal touch starts here."
                    } else {
                        "No matching items"
                    },
                    14.,
                    550.,
                    p.ink,
                ))
                .child(label(
                    if query.is_empty() {
                        "Add an item to adapt OpenWhisper to your vocabulary and workflow."
                    } else {
                        "Try a shorter search or clear the filter."
                    },
                    12.,
                    400.,
                    p.muted,
                )),
        );
    }
    content.child(label(
        format!("{shown} of {} items", items.len()),
        10.5,
        400.,
        p.muted,
    ))
}
fn select_item(s: &mut State, collection: &str, id: &str) {
    let prefix = format!("settings:item:{collection}:");
    s.edits.retain(|key, _| !key.starts_with(&prefix));
    s.selected_item = Some(format!("collection:{collection}:{id}"));
    s.confirmation = None;
    s.open_select = None;
}
fn title(item: &Value) -> String {
    for key in [
        "name", "label", "phrase", "trigger", "word", "source", "from", "title", "id",
    ] {
        if let Some(value) = item[key].as_str().filter(|s| !s.is_empty()) {
            return value.chars().take(160).collect();
        }
    }
    "Untitled item".into()
}
fn searchable(item: &Value) -> String {
    item.as_object()
        .map(|obj| {
            obj.iter()
                .filter(|(key, _)| {
                    !key.contains("secret") && !key.contains("token") && !key.contains("api_key")
                })
                .map(|(_, v)| fields::render_value(v))
                .collect::<Vec<_>>()
                .join(" ")
                .to_lowercase()
        })
        .unwrap_or_default()
}
fn summary(item: &Value, schema: &[Value]) -> Option<String> {
    for field in schema {
        if field["secret"].as_bool() == Some(true) {
            continue;
        }
        let key = string(field, "key");
        if matches!(key, "id" | "name" | "label" | "title" | "enabled") {
            continue;
        }
        let value = fields::render_value(&item[key]);
        if !value.is_empty() && value != title(item) {
            return Some(value.chars().take(180).collect());
        }
    }
    None
}
fn editor(
    s: &State,
    p: &Palette,
    collection: &str,
    id: &str,
    item: &Value,
    schema: &[Value],
    can_delete: bool,
    layer: &mut Vec<El>,
) -> El {
    let is_new = id == "__new__";
    let mut content = card(p)
        .key(format!("editor-{collection}-{id}"))
        .min_w(0.)
        .gap(18.)
        .child(label(
            if is_new {
                "Add a personal touch".into()
            } else {
                format!("Edit {}", title(item))
            },
            16.,
            550.,
            p.ink,
        ));
    let mut valid = true;
    for field in schema {
        let key = string(field, "key");
        if key == "id" || field["writable"].as_bool() == Some(false) {
            continue;
        }
        let edit_key = format!("settings:item:{collection}:{key}");
        let initial_value = initial(item, field);
        let initial = fields::render_field(field, &initial_value);
        let draft = edit(s, &edit_key, &initial);
        let parsed = fields::parse(field, &draft.text);
        let invalid = parsed.is_err() && s.edits.contains_key(&edit_key);
        valid &= parsed.is_ok();
        let mut control =
            col()
                .min_w(0.)
                .gap(6.)
                .child(label(string(field, "label"), 12., 550., p.ink));
        if !string(field, "description").is_empty() {
            control = control.child(label(string(field, "description"), 11., 400., p.muted));
        }
        if matches!(string(field, "type"), "boolean" | "bool") {
            let on = draft.text == "true";
            control = control.child(
                Switch::new(on)
                    .label(string(field, "label"))
                    .hide_label()
                    .disabled(!editable(s))
                    .on_toggle(move |s: &mut State| {
                        s.edits
                            .insert(edit_key.clone(), TextEdit::new((!on).to_string()));
                    })
                    .build(&p.kit)
                    .key(format!("draft-toggle-{collection}-{key}")),
            );
        } else if !list(&field["choices"]).is_empty() {
            let current = parsed.unwrap_or(initial_value);
            control = control.child(fields::choice(
                s, p, field, &edit_key, &current, None, layer,
            ));
        } else {
            control = control.child(fields::input(s, p, field, &edit_key, &initial, invalid));
            if collection == "speech_models" && key == "source" {
                let folder_key = edit_key.clone();
                control = control.child(
                    btn(
                        "choose-custom-model-folder",
                        "Choose model folder…",
                        p,
                        false,
                        editable(s),
                        move |_, tx| {
                            let key = folder_key.clone();
                            tx.open_folder(
                                FileDialogOptions {
                                    title: "Choose a local Whisper model folder".into(),
                                    ..Default::default()
                                },
                                move |s: &mut State, result, _| match result {
                                    Ok(Some(path)) => {
                                        s.edits.insert(
                                            key,
                                            TextEdit::new(path.to_string_lossy().into_owned()),
                                        );
                                    }
                                    Ok(None) => {}
                                    Err(error) => s.error = Some(error.to_string()),
                                },
                            );
                        },
                    )
                    .h(31.),
                );
            }
            if invalid {
                control = control.child(label(parsed.unwrap_err(), 11., 400., p.danger));
            }
        }
        content = content.child(control);
    }
    let save_collection = collection.to_owned();
    let save_id = id.to_owned();
    let save_item = item.clone();
    let save_schema = schema.to_vec();
    let mut actions = row()
        .gap(8.)
        .flex_wrap()
        .child(
            btn(
                format!("save-item-{collection}"),
                if is_new { "Add item" } else { "Save changes" },
                p,
                true,
                valid && editable(s),
                move |s, _| save(s, &save_collection, &save_id, &save_item, &save_schema),
            )
            .h(34.),
        )
        .child(
            btn(
                format!("cancel-item-{collection}"),
                "Cancel",
                p,
                false,
                true,
                |s, _| {
                    s.selected_item = None;
                    s.confirmation = None;
                    s.open_select = None;
                },
            )
            .h(34.),
        );
    if !is_new && can_delete {
        let collection = collection.to_owned();
        let id = id.to_owned();
        actions = actions.child(
            btn(
                format!("delete-item-{collection}"),
                "Delete item",
                p,
                false,
                editable(s),
                move |s, _| {
                    s.confirmation =
                        Some(json!({"kind":"collections.delete","collection":collection,"id":id}));
                },
            )
            .h(34.),
        );
    }
    content = content.child(divider(p)).child(actions);
    if s.confirmation.as_ref().is_some_and(|c| {
        c["kind"] == "collections.delete" && c["collection"] == collection && c["id"] == id
    }) {
        let collection = collection.to_owned();
        let id = id.to_owned();
        content = content
            .child(label(
                "Delete this item? This cannot be undone.",
                12.,
                400.,
                p.danger,
            ))
            .child(
                row()
                    .gap(8.)
                    .flex_wrap()
                    .child(
                        btn(
                            "confirm-collection-delete",
                            "Delete permanently",
                            p,
                            true,
                            editable(s),
                            move |s, _| {
                                s.command(
                                    "collections.delete",
                                    json!({"collection":collection,"id":id}),
                                );
                                s.selected_item = None;
                                s.confirmation = None;
                            },
                        )
                        .h(32.),
                    )
                    .child(
                        btn(
                            "cancel-collection-delete",
                            "Keep item",
                            p,
                            false,
                            true,
                            |s, _| s.confirmation = None,
                        )
                        .h(32.),
                    ),
            );
    }
    if !is_new {
        content = content.child(extra_tools(s, p, collection, id, item));
    }
    content
}
fn extra_tools(s: &State, p: &Palette, collection: &str, id: &str, item: &Value) -> El {
    let mut tools = col().min_w(0.).gap(10.);
    if matches!(collection, "actions" | "webhooks") {
        let mut actions = row().gap(7.).flex_wrap();
        let collection_id = collection.to_owned();
        let item_id = id.to_owned();
        actions = actions.child(
            btn(
                format!("test-collection-{id}"),
                "Try saved configuration",
                p,
                false,
                editable(s),
                move |s, _| {
                    s.command(
                        "collections.test",
                        json!({"collection":collection_id,"id":item_id}),
                    )
                },
            )
            .h(31.),
        );
        if collection == "webhooks" || string(item, "kind") == "webhook" {
            let copy_collection = collection.to_owned();
            let copy_id = id.to_owned();
            let rotate_collection = collection.to_owned();
            let rotate_id = id.to_owned();
            actions = actions
                .child(
                    btn(
                        format!("copy-collection-secret-{id}"),
                        "Copy signing key",
                        p,
                        false,
                        editable(s),
                        move |s, _| {
                            s.command(
                                "collections.secret.copy",
                                json!({"collection":copy_collection,"id":copy_id}),
                            )
                        },
                    )
                    .h(31.),
                )
                .child(
                    btn(
                        format!("rotate-collection-secret-{id}"),
                        "Replace signing key…",
                        p,
                        false,
                        editable(s),
                        move |s, _| {
                            s.command(
                                "collections.secret.copy",
                                json!({"collection":rotate_collection,"id":rotate_id,"renew":true}),
                            )
                        },
                    )
                    .h(31.),
                );
        }
        tools = tools
            .child(label(
                "Test the saved item after saving any changes above.",
                11.,
                400.,
                p.muted,
            ))
            .child(actions);
        if let Some(result) = list(&s.data["collections.tests"]["items"])
            .iter()
            .find(|v| v["collection"] == collection && v["id"] == id)
        {
            let details = &result["result"];
            tools = tools
                .child(label(
                    if string(result, "status") == "running" {
                        "Test in progress…"
                    } else if details["ok"].as_bool() == Some(true) {
                        "Test completed"
                    } else {
                        "Test needs attention"
                    },
                    12.,
                    550.,
                    if details["ok"].as_bool() == Some(false) {
                        p.danger
                    } else {
                        p.accent
                    },
                ))
                .child(label(
                    details["message"]
                        .as_str()
                        .or_else(|| details["error"].as_str())
                        .unwrap_or(""),
                    11.,
                    400.,
                    p.muted,
                ));
            if let Some(output) = details["output"].as_str() {
                tools = tools.child(label(output, 12., 400., p.ink).selectable(true));
            }
        }
    }
    if matches!(
        collection,
        "text_models" | "text_servers" | "speech_servers"
    ) {
        let method = if collection == "speech_servers" {
            "speech_servers.catalog"
        } else {
            "text_models.catalog"
        };
        let provider = id.to_owned();
        tools = tools.child(
            btn(
                format!("fetch-provider-models-{id}"),
                "Fetch available models",
                p,
                false,
                editable(s),
                move |s, _| {
                    if !s.data["settings.catalog_request"].is_object() {
                        s.data["settings.catalog_request"] = json!({});
                    }
                    s.data["settings.catalog_request"][method] = json!(provider);
                    s.command(method, json!({"id":provider}));
                },
            )
            .h(31.),
        );
        for operation in list(&s.data["operations.list"]["items"])
            .iter()
            .filter(|o| o["kind"] == method)
        {
            let result = &operation["result"];
            if result["profile_id"] != id
                && result["endpoint_id"] != id
                && operation["id"]
                    != s.data["settings.catalog_operations"][&format!("{method}:{id}")]
            {
                continue;
            }
            if string(operation, "status") == "running" {
                tools = tools.child(label(
                    "Checking the provider’s model catalog…",
                    11.,
                    400.,
                    p.muted,
                ));
            }
            if !string(operation, "error").is_empty() {
                tools = tools.child(label(string(operation, "error"), 11., 400., p.danger));
            }
            if result["profile_id"] == id || result["endpoint_id"] == id {
                let found = list(&result["items"]);
                tools = tools.child(label(
                    format!("{} models available", found.len()),
                    11.,
                    550.,
                    p.muted,
                ));
                let mut models = col()
                    .gap(5.)
                    .max_h(250.)
                    .min_h(0.)
                    .scroll(rune::ScrollSpec {
                        stick_bottom: false,
                    });
                for model in found {
                    let name = string(model, "id").to_owned();
                    let chosen = name.clone();
                    let edit_key = format!("settings:item:{collection}:model");
                    let mut option = row()
                        .gap(8.)
                        .min_w(0.)
                        .center_y()
                        .child(label(&name, 11., 400., p.ink).grow(1.).min_w(0.));
                    if collection == "speech_servers" {
                        option = option.child(
                            btn(
                                format!("choose-provider-model-{name}"),
                                "Choose",
                                p,
                                false,
                                true,
                                move |s, _| {
                                    s.edits
                                        .insert(edit_key.clone(), TextEdit::new(chosen.clone()));
                                },
                            )
                            .h(28.),
                        );
                    } else {
                        option = option.child(
                            btn(
                                format!("copy-provider-model-{name}"),
                                "Copy model name",
                                p,
                                false,
                                true,
                                move |s, tx| {
                                    tx.copy(chosen.clone());
                                    s.notice = Some("Model name copied".into());
                                },
                            )
                            .h(28.),
                        );
                    }
                    models = models.child(option);
                }
                tools = tools.child(models);
            }
        }
    }
    tools
}
fn initial(item: &Value, field: &Value) -> Value {
    item.get(string(field, "key"))
        .or_else(|| field.get("default"))
        .or_else(|| field.get("value"))
        .cloned()
        .unwrap_or_else(|| match string(field, "type") {
            "boolean" | "bool" => json!(false),
            "json" | "object" => json!({}),
            "list" | "array" => json!([]),
            "integer" | "int" | "number" | "float" => json!(0),
            _ => json!(""),
        })
}
fn save(s: &mut State, collection: &str, id: &str, original: &Value, schema: &[Value]) {
    let mut item = original.as_object().cloned().unwrap_or_default();
    if id != "__new__" {
        item.insert("id".into(), json!(id));
    }
    for field in schema {
        let key = string(field, "key");
        if key == "id" || field["writable"].as_bool() == Some(false) {
            continue;
        }
        let edit_key = format!("settings:item:{collection}:{key}");
        let initial = fields::render_field(field, &initial(original, field));
        let text = s
            .edits
            .get(&edit_key)
            .map(|e| e.text.as_str())
            .unwrap_or(&initial);
        match fields::parse(field, text) {
            Ok(value) => {
                item.insert(key.into(), value);
            }
            Err(error) => {
                s.error = Some(format!("{}: {error}", string(field, "label")));
                return;
            }
        }
    }
    s.command(
        "collections.save",
        json!({"collection":collection,"item":item}),
    );
}
