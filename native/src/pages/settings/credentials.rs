use super::{edit, editable, list, string};
use crate::state::State;
use crate::ui::{Palette, btn, card, label, pill};
use rune::{El, col, row};
use rune_kit::TextInput;
use serde_json::json;

pub(super) fn credentials(s: &State, p: &Palette) -> El {
    let mut content=col().gap(16.).min_w(0.)
        .child(card(p).child(label("Connect your providers",17.,550.,p.ink))
            .child(label("Use your own speech and text providers. Keys are stored locally and stay hidden from the interface and diagnostic snapshots.",12.,400.,p.muted))
            .child(btn("credentials-custom-endpoints","Custom text endpoints →",p,false,true,|s,_| {
                s.selected_tab="settings:collection:text_models".into();s.selected_item=None;
                s.command("collections.list",json!({"collection":"text_models"}));
            }).h(32.)));
    let credentials = list(&s.data["settings.schema"]["credentials"]);
    for credential in credentials {
        let name = string(credential, "name").to_owned();
        let configured = credential["configured"].as_bool().unwrap_or(false);
        let edit_key = format!("settings:credential:{name}");
        let draft = edit(s, &edit_key, "");
        let bind_key = edit_key.clone();
        let save_name = name.clone();
        let save_key = edit_key.clone();
        let mut group = card(p)
            .key(format!("credential-{name}"))
            .min_w(0.)
            .child(
                row()
                    .gap(8.)
                    .flex_wrap()
                    .center_y()
                    .child(
                        label(
                            provider_title(&name, string(credential, "label")),
                            15.,
                            550.,
                            p.ink,
                        )
                        .grow(1.)
                        .min_w(0.),
                    )
                    .child(pill(
                        if configured {
                            "Key saved"
                        } else {
                            "Key needed"
                        },
                        p,
                    )),
            )
            .child(
                TextInput::new(&draft)
                    .secure()
                    .width(None)
                    .height(36.)
                    .label(format!(
                        "{} API key",
                        provider_title(&name, string(credential, "label"))
                    ))
                    .placeholder(if configured {
                        "Enter a new key to replace the saved key"
                    } else {
                        "Paste your API key"
                    })
                    .disabled(!editable(s))
                    .on_key(|s: &mut State, e, tx| crate::ui::suppress_hotkey(s, e, tx))
                    .on_edit(move |s: &mut State| s.edit(&bind_key))
                    .on_submit(move |s: &mut State, _| write(s, &save_name, &save_key))
                    .build(&p.kit)
                    .key(format!("credential-input-{name}")),
            )
            .child(label(
                if configured {
                    "Your saved key is hidden. Entering a new key replaces it."
                } else {
                    "A key is required only when you use this provider."
                },
                11.,
                400.,
                p.muted,
            ));
        let save_name = name.clone();
        let save_key = edit_key.clone();
        let discard = edit_key.clone();
        let mut actions = row().gap(8.).flex_wrap().child(
            btn(
                format!("save-credential-{name}"),
                if configured {
                    "Replace key"
                } else {
                    "Save key"
                },
                p,
                true,
                editable(s) && !draft.text.trim().is_empty(),
                move |s, _| write(s, &save_name, &save_key),
            )
            .h(32.),
        );
        if !draft.text.is_empty() {
            actions = actions.child(
                btn(
                    format!("discard-credential-{name}"),
                    "Discard",
                    p,
                    false,
                    true,
                    move |s, _| {
                        s.edits.remove(&discard);
                    },
                )
                .h(32.),
            );
        }
        if configured {
            if credential["can_verify"].as_bool() == Some(true) {
                let verify_name = name.clone();
                actions = actions.child(
                    btn(
                        format!("verify-credential-{name}"),
                        "Verify connection",
                        p,
                        false,
                        editable(s),
                        move |s, _| {
                            s.data["settings.credential_request"] = json!(verify_name);
                            s.command("credentials.verify", json!({"name":verify_name}));
                        },
                    )
                    .h(32.),
                );
            }
            let remove_name = name.clone();
            actions = actions.child(
                btn(
                    format!("remove-credential-{name}"),
                    "Remove key",
                    p,
                    false,
                    editable(s),
                    move |s, _| {
                        s.confirmation =
                            Some(json!({"kind":"credentials.remove","name":remove_name}))
                    },
                )
                .h(32.),
            );
        }
        group = group.child(actions);
        for operation in list(&s.data["operations.list"]["items"])
            .iter()
            .filter(|o| o["kind"] == "credentials.verify")
        {
            if operation["result"]["name"] == name
                || (operation["id"].is_string()
                    && operation["id"] == s.data["settings.credential_operations"][&name])
            {
                let status = string(operation, "status");
                let message = if status == "running" {
                    "Checking the saved key…"
                } else if !string(operation, "error").is_empty() {
                    string(operation, "error")
                } else {
                    string(&operation["result"], "message")
                };
                group = group.child(label(
                    message,
                    11.,
                    400.,
                    if status == "failed" || operation["result"]["ok"].as_bool() == Some(false) {
                        p.danger
                    } else {
                        p.accent
                    },
                ));
            }
        }
        if s.confirmation
            .as_ref()
            .is_some_and(|c| c["kind"] == "credentials.remove" && c["name"] == name)
        {
            let remove_name = name.clone();
            let remove_draft = edit_key.clone();
            group=group.child(label("Remove the saved key? This provider will need a new key before you can use it.",11.,400.,p.danger))
                .child(row().gap(8.).flex_wrap()
                    .child(btn(format!("confirm-remove-credential-{name}"),"Remove saved key",p,true,editable(s),move |s,_| {
                        s.command("credentials.set",json!({"name":remove_name,"value":""}));s.edits.remove(&remove_draft);s.confirmation=None;
                    }).h(32.))
                    .child(btn(format!("keep-credential-{name}"),"Keep key",p,false,true,|s,_|s.confirmation=None).h(32.)));
        }
        content = content.child(group);
    }
    if credentials.is_empty() {
        content = content.child(card(p).child(label(
            "Provider details are loading…",
            13.,
            400.,
            p.muted,
        )));
    }
    content
}
fn provider_title<'a>(name: &str, fallback: &'a str) -> &'a str {
    match name {
        "OPENAI_API_KEY" => "OpenAI",
        "OPENROUTER_API_KEY" => "OpenRouter",
        "GROQ_API_KEY" => "Groq",
        "TYPESAFE_API_KEY" => "TypeSafe",
        "HF_TOKEN" => "Hugging Face",
        _ => fallback,
    }
}
fn write(s: &mut State, name: &str, edit_key: &str) {
    let value = s
        .edits
        .get(edit_key)
        .map(|e| e.text.trim().to_owned())
        .unwrap_or_default();
    if value.is_empty() {
        return;
    }
    if value.len() > 512 || value.chars().any(char::is_control) {
        s.error = Some("Enter a valid key without line breaks (512 characters maximum).".into());
        return;
    }
    s.command("credentials.set", json!({"name":name,"value":value}));
}
