use super::{edit, editable, list, string};
use crate::state::State;
use crate::ui::{Palette, btn, label};
use rune::{El, Key, TextEdit, col, el, row};
use rune_kit::{Select, SelectAppearance, Switch, TextArea, TextInput};
use serde_json::{Value, json};

pub(super) fn matches(field: &Value, query: &str) -> bool {
    ["key", "label", "description"]
        .iter()
        .any(|k| string(field, k).to_lowercase().contains(query))
}
pub(super) fn render_value(value: &Value) -> String {
    match value {
        Value::Null => String::new(),
        Value::String(s) => s.clone(),
        Value::Array(_) | Value::Object(_) => {
            serde_json::to_string_pretty(value).unwrap_or_default()
        }
        other => other.to_string(),
    }
}
pub(super) fn render_field(field: &Value, value: &Value) -> String {
    if string(field, "editor") == "lines" {
        return list(value)
            .iter()
            .filter_map(Value::as_str)
            .collect::<Vec<_>>()
            .join("\n");
    }
    render_value(value)
}
pub(super) fn value(s: &State, field: &Value) -> Value {
    let key = string(field, "key");
    s.data["settings.values"]["values"]
        .get(key)
        .or_else(|| s.snapshot.settings.get(key))
        .unwrap_or(&field["value"])
        .clone()
}
pub(super) fn parse(field: &Value, text: &str) -> Result<Value, String> {
    let kind = string(field, "type");
    if let Some(choice) = list(&field["choices"])
        .iter()
        .find(|choice| render_value(&choice["value"]) == text)
    {
        return Ok(choice["value"].clone());
    }
    if field["required"].as_bool() == Some(true) && text.trim().is_empty() {
        return Err("This field is required.".into());
    }
    if string(field, "editor") == "lines" {
        return Ok(Value::Array(
            text.lines()
                .map(str::trim)
                .filter(|line| !line.is_empty())
                .map(|line| Value::String(line.to_owned()))
                .collect(),
        ));
    }
    let value = match kind {
        "integer" | "int" => Value::from(
            text.trim()
                .parse::<i64>()
                .map_err(|_| "Enter a whole number.")?,
        ),
        "number" | "float" => {
            let n = text.trim().parse::<f64>().map_err(|_| "Enter a number.")?;
            if !n.is_finite() {
                return Err("Enter a finite number.".into());
            }
            Value::from(n)
        }
        "boolean" | "bool" => Value::from(text.parse::<bool>().map_err(|_| "Choose on or off.")?),
        "json" | "object" | "list" | "array" => {
            let v: Value =
                serde_json::from_str(text).map_err(|e| format!("Enter valid JSON: {e}"))?;
            if matches!(kind, "list" | "array") && !v.is_array() {
                return Err("Enter a JSON list, for example [\"first\", \"second\"].".into());
            }
            if kind == "object" && !v.is_object() {
                return Err("Enter a JSON object.".into());
            }
            v
        }
        _ => Value::String(text.to_owned()),
    };
    if let Some(number) = value.as_f64() {
        if let Some(min) = field["min"].as_f64() {
            if number < min {
                return Err(format!("Use a value of {min} or more."));
            }
        }
        if let Some(max) = field["max"].as_f64() {
            if number > max {
                return Err(format!("Use a value of {max} or less."));
            }
        }
    }
    if string(field, "format") == "url"
        && !text.is_empty()
        && !(text.starts_with("https://") || text.starts_with("http://"))
    {
        return Err("Enter a complete http:// or https:// URL.".into());
    }
    Ok(value)
}

pub(super) fn setting(s: &State, p: &Palette, field: &Value, layer: &mut Vec<El>) -> El {
    let key = string(field, "key").to_owned();
    let title = string(field, "label");
    let secret = field["secret"].as_bool().unwrap_or(false);
    let current = if secret {
        Value::String(String::new())
    } else {
        value(s, field)
    };
    let writable = field["writable"].as_bool().unwrap_or(true);
    let mut block = col().key(format!("setting-{key}")).min_w(0.).gap(8.);
    let info = col()
        .grow(1.)
        .min_w(0.)
        .gap(4.)
        .child(label(title, 12., 550., p.ink))
        .child(label(string(field, "description"), 11., 400., p.muted));
    if matches!(string(field, "type"), "boolean" | "bool") && writable {
        let enabled = editable(s);
        let key_copy = key.clone();
        return row().min_w(0.).gap(16.).center_y().child(info).child(
            Switch::new(current.as_bool().unwrap_or(false))
                .label(title)
                .hide_label()
                .disabled(!enabled)
                .on_toggle(move |s: &mut State| {
                    let current = s.data["settings.values"]["values"]
                        .get(&key_copy)
                        .or_else(|| s.snapshot.settings.get(&key_copy))
                        .and_then(Value::as_bool)
                        .unwrap_or(false);
                    s.command("settings.update", json!({"key":key_copy,"value":!current}));
                })
                .build(&p.kit)
                .key(format!("toggle-{key}")),
        );
    }
    block = block.child(info);
    if !writable {
        return block.child(
            label(
                if secret {
                    "Managed securely".into()
                } else {
                    render_value(&current)
                },
                12.,
                400.,
                p.muted,
            )
            .selectable(!secret),
        );
    }
    let edit_key = format!("settings:field:{key}");
    if field["choices"].is_array() && !list(&field["choices"]).is_empty() {
        return block.child(choice(s, p, field, &edit_key, &current, Some(&key), layer));
    }
    let initial = render_field(field, &current);
    let draft = edit(s, &edit_key, &initial);
    let parsed = parse(field, &draft.text);
    let dirty = draft.text != initial && (!secret || !draft.text.is_empty());
    let invalid = dirty && parsed.is_err();
    let mut controls = col()
        .min_w(0.)
        .gap(8.)
        .child(input(s, p, field, &edit_key, &initial, invalid));
    if dirty {
        let save_key = key.clone();
        let save_edit = edit_key.clone();
        let save_field = field.clone();
        let cancel_edit = edit_key.clone();
        controls = controls.child(
            row()
                .gap(8.)
                .flex_wrap()
                .child(
                    btn(
                        format!("save-setting-{key}"),
                        "Save",
                        p,
                        true,
                        editable(s) && parsed.is_ok(),
                        move |s, _| save_setting(s, &save_key, &save_edit, &save_field),
                    )
                    .h(31.),
                )
                .child(
                    btn(
                        format!("discard-setting-{key}"),
                        "Discard",
                        p,
                        false,
                        true,
                        move |s, _| {
                            s.edits.remove(&cancel_edit);
                        },
                    )
                    .h(31.),
                ),
        );
        if let Err(error) = parsed {
            controls = controls.child(label(error, 11., 400., p.danger));
        }
    }
    if secret {
        let configured = field["configured"].as_bool().unwrap_or(false)
            || field["has_value"].as_bool().unwrap_or(false);
        controls = controls.child(label(
            if configured {
                "A key is saved securely. Enter a new value to replace it."
            } else {
                "Keys stay on this device and are hidden from snapshots and search."
            },
            10.5,
            400.,
            p.muted,
        ));
        if configured {
            let remove_key = key.clone();
            controls = controls.child(
                btn(
                    format!("clear-secret-{key}"),
                    "Remove saved key",
                    p,
                    false,
                    editable(s),
                    move |s, _| {
                        s.confirmation =
                            Some(json!({"kind":"settings.clear_secret","key":remove_key}));
                    },
                )
                .h(29.),
            );
            if s.confirmation
                .as_ref()
                .is_some_and(|c| c["kind"] == "settings.clear_secret" && c["key"] == key)
            {
                let clear_key = key.clone();
                controls=controls.child(col().gap(8.).child(label("Remove this saved key? This provider will need a new key before it can be used.",11.,400.,p.danger))
                    .child(row().gap(8.).flex_wrap().child(btn(format!("confirm-clear-{key}"),"Remove key",p,true,editable(s),move |s,_| {
                        s.command("settings.update",json!({"key":clear_key,"value":""}));s.confirmation=None;
                    }).h(31.)).child(btn(format!("cancel-clear-{key}"),"Keep key",p,false,true,|s,_|s.confirmation=None).h(31.))));
            }
        }
    }
    block.child(controls)
}
fn save_setting(s: &mut State, key: &str, edit_key: &str, field: &Value) {
    let Some(draft) = s.edits.get(edit_key) else {
        return;
    };
    match parse(field, &draft.text) {
        Ok(value) => s.command("settings.update", json!({"key":key,"value":value})),
        Err(error) => s.error = Some(error),
    }
}

pub(super) fn input(
    s: &State,
    p: &Palette,
    field: &Value,
    key: &str,
    initial: &str,
    invalid: bool,
) -> El {
    let draft = edit(s, key, initial);
    let bind_key = key.to_owned();
    let bind_initial = initial.to_owned();
    let cancel_key = key.to_owned();
    let submit_key = key.to_owned();
    let submit_field = field.clone();
    let multiline = matches!(
        string(field, "type"),
        "text" | "multiline" | "json" | "object" | "list" | "array"
    );
    if multiline {
        let mut input = TextArea::new(&draft)
            .width(None)
            .label(string(field, "label"))
            .placeholder(string(field, "placeholder"))
            .rows(4)
            .auto_grow(3, 8)
            .wrap_anywhere()
            .invalid(invalid)
            .disabled(!editable(s))
            .on_edit(move |s: &mut State| super::edit_mut(s, &bind_key, &bind_initial))
            .on_submit(move |s: &mut State, _| {
                if let Some(key) = submit_key.strip_prefix("settings:field:") {
                    save_setting(s, key, &submit_key, &submit_field);
                }
            })
            .on_key(move |s: &mut State, event, tx| {
                if crate::ui::suppress_hotkey(s, event, tx) {
                    return true;
                }
                if event.key == Key::Escape {
                    s.edits.remove(&cancel_key);
                    true
                } else {
                    false
                }
            });
        if field["secret"].as_bool() == Some(true) {
            input = input.secure();
        }
        input.build(&p.kit).key(format!("input-{key}")).min_w(0.)
    } else {
        let placeholder = if field["secret"].as_bool() == Some(true) {
            "Enter a new secret value"
        } else {
            string(field, "placeholder")
        };
        let mut input = TextInput::new(&draft)
            .width(None)
            .height(35.)
            .label(string(field, "label"))
            .placeholder(placeholder)
            .invalid(invalid)
            .disabled(!editable(s))
            .on_edit(move |s: &mut State| super::edit_mut(s, &bind_key, &bind_initial))
            .on_submit(move |s: &mut State, _| {
                if let Some(key) = submit_key.strip_prefix("settings:field:") {
                    save_setting(s, key, &submit_key, &submit_field);
                }
            })
            .on_key(move |s: &mut State, event, tx| {
                if crate::ui::suppress_hotkey(s, event, tx) {
                    return true;
                }
                if event.key == Key::Escape {
                    s.edits.remove(&cancel_key);
                    true
                } else {
                    false
                }
            });
        if field["secret"].as_bool() == Some(true) {
            input = input.secure();
        }
        let control = input.build(&p.kit).key(format!("input-{key}")).min_w(0.);
        if string(field, "key").ends_with("hotkey") || key.starts_with("settings:item:hotkeys:") {
            let capture_key = key.to_owned();
            return col().gap(7.).min_w(0.).child(control).child(
                btn(
                    format!("record-hotkey-{key}"),
                    "Record shortcut…",
                    p,
                    false,
                    editable(s),
                    move |s, _| {
                        s.data["settings.hotkey_capture"] = json!({"key":capture_key});
                        s.data["hotkeys.capture"] = Value::Null;
                        s.command("hotkeys.capture", json!({"active":true}));
                    },
                )
                .h(30.),
            );
        }
        control
    }
}

pub(super) fn capture_dialog(s: &State, p: &Palette, layer: &mut Vec<El>) {
    let key = s.data["settings.hotkey_capture"]["key"]
        .as_str()
        .unwrap_or("");
    if key.is_empty() {
        return;
    }
    let ready = s.data["hotkeys.capture"]["active"].as_bool() == Some(true);
    let mut body = col()
        .padding(18.)
        .gap(13.)
        .child(label(
            if ready {
                "Press the shortcut you want to use."
            } else {
                "Pausing global shortcuts…"
            },
            17.,
            550.,
            p.ink,
        ))
        .child(label(
            "Hold any modifiers, then press the main key. Escape cancels.",
            12.,
            400.,
            p.muted,
        ));
    if let Some(error) = &s.error {
        body = body.child(label(error, 12., 400., p.danger));
    }
    body = body.child(
        btn(
            "cancel-hotkey-capture",
            "Cancel recording",
            p,
            false,
            true,
            |s, _| cancel_capture(s),
        )
        .h(32.),
    );
    // Keep the key handler on the modal itself: Rune stops event bubbling at
    // the modal boundary, so a handler on its outer positioning layer misses it.
    let dialog = col()
        .key("shortcut-recorder-dialog")
        .role("dialog")
        .label("Record a keyboard shortcut")
        .w_full()
        .max_w(460.)
        .radius(12.)
        .fill(p.surface)
        .modal(true, cancel_capture)
        .focusable(true)
        .child(
            row()
                .padding(18.)
                .gap(12.)
                .center_y()
                .child(
                    label("Record a keyboard shortcut", 14., 550., p.ink)
                        .grow(1.)
                        .min_w(0.),
                )
                .child(
                    btn("close-hotkey-capture", "Close", p, false, true, |s, _| {
                        cancel_capture(s)
                    })
                    .h(30.),
                ),
        )
        .child(body)
        .on_window_focus(|s: &mut State, focused, _| {
            if !focused {
                cancel_capture(s);
            }
        })
        .on_key(|s: &mut State, event, tx| {
            let modified = event.mods.ctrl
                || event.mods.cmd
                || event.mods.alt
                || event.mods.shift
                || event.mods.logo;
            if event.key == Key::Escape && !modified {
                cancel_capture(s);
                return true;
            }
            if event.key == Key::Tab && !modified {
                return false;
            }
            if s.data["hotkeys.capture"]["active"].as_bool() != Some(true) {
                return true;
            }
            if let Some(chord) =
                captured_chord(event, tx.physical_key().map(|key| format!("{key:?}")))
            {
                if let Some(key) = s.data["settings.hotkey_capture"]["key"]
                    .as_str()
                    .map(str::to_owned)
                {
                    s.edits.insert(key, TextEdit::new(chord));
                }
                cancel_capture(s);
            }
            true
        });
    layer.push(
        el().key("shortcut-recorder-scrim")
            .absolute(0., 0.)
            .w_full()
            .h_full()
            .fill(p.kit.c.scrim)
            .a11y_hidden(),
    );
    layer.push(
        el().key("shortcut-recorder-layer")
            .absolute(0., 0.)
            .w_full()
            .h_full()
            .padding(16.)
            .center()
            .child(dialog),
    );
}
pub(super) fn cancel_capture(s: &mut State) {
    if s.data["settings.hotkey_capture"]["key"].is_string() {
        s.data["settings.hotkey_capture"] = Value::Null;
        s.command("hotkeys.capture", json!({"active":false}));
    }
}
fn captured_chord(event: &rune::KeyEvent, physical: Option<String>) -> Option<String> {
    let main = if let Some(code) = physical.as_deref().and_then(|v| v.strip_prefix("Numpad")) {
        match code {
            "Multiply" => "kp *".into(),
            "Add" => "kp +".into(),
            "Subtract" => "kp -".into(),
            "Divide" => "kp /".into(),
            "Decimal" => "kp .".into(),
            "Enter" => "kp enter".into(),
            n if n.len() == 1 => format!("kp {n}"),
            _ => return None,
        }
    } else {
        match &event.key {
            Key::Text(text) if text.chars().count() == 1 => {
                if text == " " {
                    "space".into()
                } else {
                    text.to_lowercase()
                }
            }
            Key::Command(c) => {
                if *c == ' ' {
                    "space".into()
                } else {
                    c.to_lowercase().to_string()
                }
            }
            Key::Function(n) => format!("f{n}"),
            Key::Enter => "enter".into(),
            Key::Backspace => "backspace".into(),
            Key::Delete => "delete".into(),
            Key::Insert => "insert".into(),
            Key::Home => "home".into(),
            Key::End => "end".into(),
            Key::Left => "left".into(),
            Key::Right => "right".into(),
            Key::Up => "up".into(),
            Key::Down => "down".into(),
            Key::PageUp => "page_up".into(),
            Key::PageDown => "page_down".into(),
            Key::PrintScreen => "print_screen".into(),
            Key::ScrollLock => "scroll_lock".into(),
            Key::Pause => "pause".into(),
            Key::Escape => "esc".into(),
            Key::Tab => "tab".into(),
            Key::ContextMenu => "menu".into(),
            _ => return None,
        }
    };
    let mut parts = Vec::new();
    if event.mods.ctrl
        || (event.mods.cmd && !cfg!(target_os = "macos"))
        || (matches!(event.key, Key::Command(_)) && !cfg!(target_os = "macos"))
    {
        parts.push("ctrl".to_owned());
    }
    if event.mods.alt {
        parts.push("alt".to_owned());
    }
    if event.mods.shift {
        parts.push("shift".to_owned());
    }
    if event.mods.logo
        || (event.mods.cmd && cfg!(target_os = "macos"))
        || (matches!(event.key, Key::Command(_)) && cfg!(target_os = "macos"))
    {
        parts.push(if cfg!(target_os = "windows") {
            "win".into()
        } else {
            "cmd".into()
        });
    }
    parts.push(main);
    Some(parts.join("+"))
}
pub(super) fn choice(
    s: &State,
    p: &Palette,
    field: &Value,
    key: &str,
    current: &Value,
    setting_key: Option<&str>,
    layer: &mut Vec<El>,
) -> El {
    let options = list(&field["choices"]);
    let titles: Vec<String> = options
        .iter()
        .map(|v| {
            if v.is_object() {
                string(v, "label").to_owned()
            } else {
                render_value(v)
            }
        })
        .collect();
    let selected = options
        .iter()
        .position(|v| {
            if v.is_object() {
                &v["value"] == current
            } else {
                v == current
            }
        })
        .map(|i| titles[i].as_str());
    let open_key = key.to_owned();
    let toggle_key = key.to_owned();
    let edit_key = key.to_owned();
    let setting_key = setting_key.map(str::to_owned);
    let choices: Vec<_> = options
        .iter()
        .zip(titles.iter())
        .map(|(v, title)| {
            (
                title.clone(),
                if v.is_object() {
                    v["value"].clone()
                } else {
                    v.clone()
                },
            )
        })
        .collect();
    Select::new(format!("select-{key}"), &titles)
        .label(string(field, "label"))
        .appearance(SelectAppearance::Field)
        .width(300.)
        .placeholder("Choose an option")
        .current(selected)
        .open(s.open_select.as_deref() == Some(&open_key))
        .disabled(!editable(s))
        .on_toggle(move |s: &mut State| {
            s.open_select = if s.open_select.as_deref() == Some(&toggle_key) {
                None
            } else {
                Some(toggle_key.clone())
            };
        })
        .on_close(|s: &mut State| s.open_select = None)
        .on_choose(move |s: &mut State, title, _| {
            if let Some((_, value)) = choices.iter().find(|(label, _)| label == title) {
                if let Some(key) = &setting_key {
                    s.command("settings.update", json!({"key":key,"value":value}));
                } else {
                    s.edits
                        .insert(edit_key.clone(), TextEdit::new(render_value(value)));
                }
            }
            s.open_select = None;
        })
        .build(&p.kit, layer)
        .max_w(300.)
        .min_w(0.)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn invalid_numbers_and_ranges_cannot_be_saved() {
        let f = json!({"type":"integer","min":1,"max":20});
        assert!(parse(&f, "1.2").is_err());
        assert!(parse(&f, "0").is_err());
        assert!(parse(&f, "21").is_err());
        assert_eq!(parse(&f, "12").unwrap(), json!(12));
        assert!(parse(&json!({"type":"number"}), "NaN").is_err());
    }
    #[test]
    fn structured_settings_preserve_their_data_types() {
        assert_eq!(
            parse(&json!({"type":"list"}), "[\"one\", \"two\"]").unwrap(),
            json!(["one", "two"])
        );
        assert!(parse(&json!({"type":"list"}), "{}").is_err());
        assert!(parse(&json!({"type":"object"}), "[]").is_err());
        assert!(parse(&json!({"required":true}), " ").is_err());
        assert_eq!(
            parse(&json!({"type":"string"}), "hello").unwrap(),
            json!("hello")
        );
    }
    #[test]
    fn search_matches_descriptions_and_setting_keys() {
        let f = json!({"key":"record_hotkey","label":"Record shortcut","description":"Works across applications"});
        assert!(matches(&f, "hotkey"));
        assert!(matches(&f, "applications"));
        assert!(!matches(&f, "no match"));
    }
}
