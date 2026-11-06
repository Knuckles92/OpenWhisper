use super::{edit, editable, list, string};
use crate::state::State;
use crate::ui::{Palette, btn, card, label, pill};
use rune::{El, col, el, row};
use rune_kit::TextInput;
use serde_json::json;

pub(super) fn catalog(s: &State, p: &Palette) -> El {
    let response = &s.data["models.list"];
    let items = list(&response["items"]);
    let search = edit(s, "settings:model-search", "");
    let query = search.text.trim().to_lowercase();
    let installed = items
        .iter()
        .filter(|i| i["installed"].as_bool() == Some(true))
        .count();
    let mut content=col().gap(16.).min_w(0.)
        .child(card(p).key("models-heading").min_w(0.)
            .child(label("Find your voice engine",17.,550.,p.ink))
            .child(label("Local models keep audio on your computer. Cloud engines use your configured provider.",12.,400.,p.muted))
            .child(label(format!("{installed} installed  ·  {} available",items.len()),11.,400.,p.muted))
            .child(row().gap(8.).flex_wrap()
                .child(btn("refresh-models","Refresh catalog",p,false,s.available(),|s,_|s.command("models.list",json!({}))).h(32.))
                .child(btn("configure-providers","Configure provider keys →",p,false,true,|s,_| {s.selected_tab="settings:credentials".into();s.selected_item=None;}).h(32.))
                .child(btn("configure-runtimes","Optional runtimes →",p,false,true,|s,_|{s.selected_tab="settings:system:components".into();s.command("components.list",json!({}));}).h(32.))
                .child(btn("configure-custom-models","Custom Whisper models →",p,false,true,|s,_|super::select_tab(s,"settings:collection:speech_models".into())).h(32.))))
        .child(TextInput::new(&search).width(None).height(36.).label("Find a voice model")
            .placeholder("Search models and engines…").on_key(|s:&mut State,e,tx|crate::ui::suppress_hotkey(s,e,tx)).on_edit(|s:&mut State|s.edit("settings:model-search"))
            .build(&p.kit).key("model-search"));
    if items.is_empty() {
        return content.child(
            card(p)
                .child(label("Loading the model catalog…", 14., 550., p.ink))
                .child(label(
                    "Connect to the local service, then refresh to see available speech engines.",
                    12.,
                    400.,
                    p.muted,
                )),
        );
    }
    let mut shown = 0;
    for item in items.iter().filter(|i| {
        query.is_empty()
            || ["id", "name", "family", "description"]
                .iter()
                .any(|k| string(i, k).to_lowercase().contains(&query))
    }) {
        let id = string(item, "id");
        let selected = item["selected"].as_bool().unwrap_or(false);
        let installed = item["installed"].as_bool().unwrap_or(false);
        let status = string(item, "status");
        let downloading = matches!(
            status,
            "downloading" | "queued" | "verifying" | "extracting"
        );
        let mut model = card(p).key(format!("model-{id}")).min_w(0.).gap(13.);
        let mut heading = row().gap(10.).center_y().flex_wrap().child(
            label(string(item, "name"), 15., 550., p.ink)
                .grow(1.)
                .min_w(0.),
        );
        if selected {
            heading = heading.child(pill("Active", p));
        } else if installed {
            heading = heading.child(pill("Installed", p));
        }
        model = model.child(heading);
        if !string(item, "family").is_empty() {
            let family = string(item, "family");
            let family_label = if !string(item, "family_label").is_empty() {
                string(item, "family_label").to_owned()
            } else {
                match family {
                    "local_whisper" => "Local Whisper".into(),
                    "parakeet" => "Parakeet".into(),
                    "qwen_asr" => "Qwen ASR".into(),
                    "nemotron" => "Nemotron".into(),
                    "moonshine" => "Moonshine".into(),
                    "parakeet_mlx" => "Parakeet for Apple silicon".into(),
                    "parakeet_unified" => "Parakeet · Cross-platform".into(),
                    "remote" => "Paired computer".into(),
                    "api" => "Cloud speech providers".into(),
                    other => other.replace('_', " "),
                }
            };
            model = model.child(label(family_label, 10.5, 550., p.muted));
        }
        if !string(item, "description").is_empty() {
            model = model.child(label(string(item, "description"), 12., 400., p.muted));
        }
        let size = if item["size"].is_string() {
            string(item, "size").to_owned()
        } else if item["size"].is_number() {
            item["size"].to_string()
        } else {
            String::new()
        };
        if !size.is_empty() {
            model = model.child(label(format!("Download size: {size}"), 11., 400., p.muted));
        }
        if downloading {
            let raw = item["progress"].as_f64().unwrap_or(0.);
            let percent = if raw <= 1. { raw * 100. } else { raw }.clamp(0., 100.);
            model = model.child(
                col()
                    .gap(6.)
                    .child(label(
                        format!(
                            "{} · {percent:.0}%",
                            if status == "verifying" {
                                "Verifying download"
                            } else if status == "extracting" {
                                "Preparing model"
                            } else {
                                "Downloading"
                            }
                        ),
                        11.,
                        550.,
                        p.accent,
                    ))
                    .child(
                        el().h(5.).w_full().radius(4.).fill(p.soft).child(
                            el().h(5.)
                                .w_frac(percent as f32 / 100.)
                                .radius(4.)
                                .fill(p.accent),
                        ),
                    ),
            );
        } else if !status.is_empty()
            && !matches!(status, "available" | "installed" | "ready" | "selected")
        {
            model = model.child(label(
                status,
                11.,
                400.,
                if status.contains("error") || status.contains("failed") {
                    p.danger
                } else {
                    p.muted
                },
            ));
        }
        let select_id = id.to_owned();
        let download_id = id.to_owned();
        let remove_id = id.to_owned();
        let mut actions = row().gap(8.).flex_wrap();
        if installed || item["can_download"].as_bool() == Some(false) {
            actions = actions.child(
                btn(
                    format!("select-model-{id}"),
                    if selected {
                        "Selected"
                    } else {
                        "Use this engine"
                    },
                    p,
                    !selected,
                    editable(s) && !selected && !downloading,
                    move |s, _| s.command("models.select", json!({"id":select_id})),
                )
                .h(32.),
            );
        }
        if item["can_download"].as_bool() == Some(true) && !installed && !downloading {
            actions = actions.child(
                btn(
                    format!("download-model-{id}"),
                    "Download",
                    p,
                    true,
                    editable(s),
                    move |s, _| s.command("models.download", json!({"id":download_id})),
                )
                .h(32.),
            );
        }
        if downloading {
            actions = actions.child(
                btn(
                    format!("cancel-download-{id}"),
                    "Cancel download",
                    p,
                    false,
                    s.available(),
                    |s, _| s.command("models.cancel", json!({})),
                )
                .h(32.),
            );
        }
        if item["can_remove"].as_bool() == Some(true) && installed && !downloading {
            actions = actions.child(
                btn(
                    format!("remove-model-{id}"),
                    "Remove download",
                    p,
                    false,
                    editable(s),
                    move |s, _| {
                        s.confirmation = Some(json!({"kind":"models.remove","id":remove_id}))
                    },
                )
                .h(32.),
            );
        }
        model = model.child(actions);
        if s.confirmation
            .as_ref()
            .is_some_and(|c| c["kind"] == "models.remove" && c["id"] == id)
        {
            let remove_id = id.to_owned();
            model=model.child(label("Remove these model files from this computer? You can download them again later.",11.,400.,p.danger))
                .child(row().gap(8.).flex_wrap()
                    .child(btn(format!("confirm-remove-model-{id}"),"Remove files",p,true,editable(s),move |s,_| {s.command("models.remove",json!({"id":remove_id}));s.confirmation=None;}).h(31.))
                    .child(btn(format!("keep-model-{id}"),"Keep download",p,false,true,|s,_|s.confirmation=None).h(31.)));
        }
        content = content.child(model);
        shown += 1;
    }
    if shown == 0 {
        content =
            content.child(card(p).child(label("No models match that search.", 13., 400., p.muted)));
    }
    content
}

pub(super) fn components(s: &State, p: &Palette) -> El {
    let mut content=col().gap(16.).min_w(0.).child(card(p).child(label("Add the tools you need",17.,550.,p.ink))
        .child(label("Optional speech engines, GPU support, and meeting agents are installed only when you need them.",12.,400.,p.muted)));
    let items = list(&s.data["components.list"]["items"]);
    for component in items {
        let id = string(component, "component_id");
        let status = string(component, "state");
        let progress = &s.data["snapshot"]["components"][id];
        let working = !string(progress, "phase").is_empty();
        let mut group = card(p)
            .key(format!("component-{id}"))
            .min_w(0.)
            .child(
                row()
                    .gap(8.)
                    .center_y()
                    .flex_wrap()
                    .child(
                        label(string(component, "display_name"), 15., 550., p.ink)
                            .grow(1.)
                            .min_w(0.),
                    )
                    .child(pill(
                        match status {
                            "installed" => "Installed",
                            "external" => "Available locally",
                            "update_available" => "Update ready",
                            "incompatible" => "Unavailable",
                            "broken" => "Needs repair",
                            _ => "Optional",
                        },
                        p,
                    )),
            )
            .child(label(string(component, "summary"), 12., 400., p.muted));
        if !string(component, "reason").is_empty() {
            group = group.child(label(string(component, "reason"), 11., 400., p.muted));
        }
        if let Some(version) = component["installed_version"].as_str() {
            group = group.child(label(
                format!("Installed version: {version}"),
                10.5,
                400.,
                p.muted,
            ));
        }
        if working {
            let done = progress["done"].as_f64().unwrap_or(0.);
            let total = progress["total"].as_f64().unwrap_or(0.);
            group = group.child(label(
                if total > 0. {
                    format!(
                        "{} · {:.0}%",
                        string(progress, "phase"),
                        100. * done / total
                    )
                } else {
                    string(progress, "phase").to_owned()
                },
                11.,
                550.,
                p.accent,
            ));
            let cancel = id.to_owned();
            group = group.child(
                btn(
                    format!("cancel-component-{id}"),
                    "Cancel installation",
                    p,
                    false,
                    s.available(),
                    move |s, _| s.command("components.cancel", json!({"id":cancel})),
                )
                .h(31.),
            );
        } else {
            if !string(progress, "message").is_empty() {
                group = group.child(label(
                    string(progress, "message"),
                    11.,
                    400.,
                    if progress["success"].as_bool() == Some(false) {
                        p.danger
                    } else {
                        p.accent
                    },
                ));
            }
            let mut actions = row().gap(7.).flex_wrap();
            if matches!(status, "not_installed" | "broken" | "update_available") {
                let install = id.to_owned();
                actions = actions.child(
                    btn(
                        format!("install-component-{id}"),
                        match status {
                            "broken" => "Repair",
                            "update_available" => "Update runtime",
                            _ => "Install runtime",
                        },
                        p,
                        true,
                        editable(s),
                        move |s, _| s.command("components.install", json!({"id":install})),
                    )
                    .h(32.),
                );
            }
            if matches!(status, "installed" | "broken" | "update_available") {
                let remove = id.to_owned();
                actions = actions.child(
                    btn(
                        format!("remove-component-{id}"),
                        "Uninstall…",
                        p,
                        false,
                        editable(s),
                        move |s, _| {
                            s.confirmation = Some(json!({"kind":"components.remove","id":remove}))
                        },
                    )
                    .h(32.),
                );
            }
            group = group.child(actions);
        }
        if s.confirmation
            .as_ref()
            .is_some_and(|v| v["kind"] == "components.remove" && v["id"] == id)
        {
            let remove = id.to_owned();
            group=group.child(label("Uninstall this optional runtime? Features that depend on it will need it installed again.",11.,400.,p.danger))
                .child(row().gap(7.).flex_wrap().child(btn(format!("confirm-remove-component-{id}"),"Uninstall runtime",p,true,editable(s),move |s,_|{s.command("components.remove",json!({"id":remove}));s.confirmation=None;}).h(31.))
                    .child(btn(format!("keep-component-{id}"),"Keep runtime",p,false,true,|s,_|s.confirmation=None).h(31.)));
        }
        content = content.child(group);
    }
    if items.is_empty() {
        content = content.child(label(
            "Runtime availability is loading…",
            12.,
            400.,
            p.muted,
        ));
    }
    content
}
