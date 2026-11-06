//! Native operational panels for preferences that involve a workflow rather than a scalar.
use super::{edit, editable, fields, list, string};
use crate::state::State;
use crate::ui::{Palette, btn, card, divider, label, pill};
use rune::{El, FileDialogOptions, TextEdit, col, el, row};
use rune_kit::{Switch, TextInput};
use serde_json::{Value, json};

pub(super) const SECTIONS: [(&str, &str, &str); 7] = [
    ("remote", "Pair computers", "remote.state"),
    ("backup", "Backups & restore", "backup.state"),
    ("updates", "App updates", "updates.state"),
    ("stats", "Your dictation stats", "stats.get"),
    ("setup", "Setup checklist", "setup.state"),
    ("mcp", "Connect AI assistants", "mcp.state"),
    ("components", "Optional runtimes", "components.list"),
];
pub(super) fn panel(s: &State, p: &Palette, id: &str, layer: &mut Vec<El>) -> El {
    let title = SECTIONS
        .iter()
        .find(|(key, _, _)| *key == id)
        .map(|(_, title, _)| *title)
        .unwrap_or("Workspace");
    let method = SECTIONS
        .iter()
        .find(|(key, _, _)| *key == id)
        .map(|(_, _, method)| *method)
        .unwrap_or("snapshot");
    let mut content = col().gap(18.).min_w(0.).child(
        row()
            .gap(10.)
            .center_y()
            .flex_wrap()
            .child(label(title, 18., 550., p.ink).grow(1.).min_w(0.))
            .child(
                btn(
                    format!("refresh-system-{id}"),
                    "Refresh",
                    p,
                    false,
                    s.available(),
                    move |s, _| s.command(method, json!({})),
                )
                .h(32.),
            ),
    );
    content = content.child(match id {
        "remote" => remote(s, p),
        "backup" => backup(s, p, layer),
        "updates" => updates(s, p),
        "stats" => stats(s, p),
        "setup" => setup(s, p),
        "mcp" => mcp(s, p),
        "components" => super::models::components(s, p),
        _ => el(),
    });
    content
}
fn data<'a>(s: &'a State, method: &str) -> &'a Value {
    &s.data[method]
}
fn input(
    s: &State,
    p: &Palette,
    key: &str,
    title: &str,
    initial: &str,
    placeholder: &str,
    secure: bool,
) -> El {
    let model = edit(s, key, initial);
    let key = key.to_owned();
    let initial = initial.to_owned();
    let mut field = TextInput::new(&model)
        .width(None)
        .height(36.)
        .label(title)
        .placeholder(placeholder)
        .disabled(!s.available())
        .on_key(|s: &mut State, e, tx| crate::ui::suppress_hotkey(s, e, tx))
        .on_edit(move |s: &mut State| super::edit_mut(s, &key, &initial));
    if secure {
        field = field.secure();
    }
    col()
        .gap(6.)
        .min_w(0.)
        .child(label(title, 11.5, 550., p.ink))
        .child(field.build(&p.kit))
}
fn draft(s: &State, key: &str, initial: &str) -> String {
    s.edits
        .get(key)
        .map(|v| v.text.clone())
        .unwrap_or_else(|| initial.into())
}
fn toggle(
    s: &State,
    p: &Palette,
    key: &str,
    title: &str,
    description: &str,
    on: bool,
    method: &'static str,
    param: &str,
) -> El {
    let param = param.to_owned();
    row()
        .min_w(0.)
        .gap(14.)
        .center_y()
        .child(
            col()
                .grow(1.)
                .min_w(0.)
                .gap(4.)
                .child(label(title, 12., 550., p.ink))
                .child(label(description, 11., 400., p.muted)),
        )
        .child(
            Switch::new(on)
                .label(title)
                .hide_label()
                .disabled(!editable(s))
                .on_toggle(move |s: &mut State| {
                    s.command(
                        method,
                        if method == "settings.update" {
                            json!({"key":param,"value":!on})
                        } else {
                            json!({param.clone():!on})
                        },
                    )
                })
                .build(&p.kit)
                .key(key),
        )
}
fn error(value: &Value, p: &Palette) -> El {
    let text = value.as_str().unwrap_or("");
    if text.is_empty() {
        el()
    } else {
        label(text, 12., 400., p.danger)
    }
}
fn progress(value: &Value, p: &Palette) -> El {
    if value.is_null() {
        return el();
    }
    let phase = value["phase"]
        .as_str()
        .or_else(|| value[0].as_str())
        .unwrap_or("Working");
    let done = value["done"]
        .as_f64()
        .or_else(|| value[1].as_f64())
        .unwrap_or(0.);
    let total = value["total"]
        .as_f64()
        .or_else(|| value[2].as_f64())
        .unwrap_or(0.);
    let text = if total > 0. {
        format!("{phase} · {:.0}%", 100. * done / total)
    } else {
        phase.to_owned()
    };
    label(text, 12., 550., p.accent)
}
fn confirm(
    s: &State,
    p: &Palette,
    kind: &str,
    message: &str,
    button: &str,
    method: &'static str,
    params: Value,
) -> El {
    if s.confirmation.as_ref().and_then(|c| c["kind"].as_str()) != Some(kind) {
        return el();
    }
    col()
        .gap(10.)
        .child(label(message, 12., 400., p.danger))
        .child(
            row()
                .gap(8.)
                .flex_wrap()
                .child(
                    btn(
                        format!("confirm-{kind}"),
                        button,
                        p,
                        true,
                        editable(s),
                        move |s, _| {
                            s.command(method, params.clone());
                            s.confirmation = None;
                        },
                    )
                    .h(32.),
                )
                .child(
                    btn(
                        format!("cancel-{kind}"),
                        "Cancel",
                        p,
                        false,
                        true,
                        |s, _| s.confirmation = None,
                    )
                    .h(32.),
                ),
        )
}
fn remote(s: &State, p: &Palette) -> El {
    let state = data(s, "remote.state");
    let host = &state["host"];
    let client = &state["client"];
    let running = host["running"].as_bool().unwrap_or(false);
    let enabled = host["enabled"].as_bool().unwrap_or(false);
    let mut content=col().min_w(0.).gap(18.)
        .child(card(p).child(label("Your computers, working together",16.,550.,p.ink))
            .child(label("Use a faster computer’s speech engine, or share this one with computers you trust.",12.,400.,p.muted))
            .child(toggle(s,p,"remote-sharing","Share this computer’s speech engines","Paired computers can request transcription while sharing is enabled.",enabled,"remote.configure","enabled"))
            .child(error(&host["error"],p)));
    let operation = &state["operation"];
    if !string(operation, "error").is_empty() {
        content = content.child(card(p).child(error(&operation["error"], p)));
    }
    if !string(client, "code").is_empty() {
        content = content.child(
            card(p)
                .child(label("Check the pairing code", 15., 550., p.ink))
                .child(label(string(client, "code"), 30., 550., p.ink).selectable(true))
                .child(label(
                    "Confirm this code matches the request shown on the host computer.",
                    12.,
                    400.,
                    p.muted,
                ))
                .child(
                    btn(
                        "remote-cancel-pending-pair",
                        "Cancel pairing",
                        p,
                        false,
                        true,
                        |s, _| s.command("remote.pair.cancel", json!({})),
                    )
                    .h(31.),
                ),
        );
    }
    if !string(client, "pending_name").is_empty() {
        content=content.child(card(p).child(label("Finish saving this pairing",15.,550.,p.ink))
            .child(label("The host approved the connection, but this computer still needs to save its credentials.",12.,400.,p.muted))
            .child(btn("remote-retry-pair-save","Try saving again",p,true,editable(s),|s,_|s.command("remote.pair.retry_save",json!({}))).h(32.)));
    }
    if running {
        let address = host["address"]
            .as_str()
            .unwrap_or("Discovering network address…");
        let port = host["port"].as_u64().unwrap_or(8767);
        let mut sharing = card(p)
            .child(
                row()
                    .gap(8.)
                    .center_y()
                    .child(label("This computer is sharing", 15., 550., p.ink).grow(1.))
                    .child(pill("Online", p)),
            )
            .child(label(format!("{address}:{port}"), 13., 550., p.accent).selectable(true))
            .child(
                btn(
                    "remote-open-pairing",
                    "Show a pairing code",
                    p,
                    true,
                    editable(s),
                    |s, _| s.command("remote.pair.open", json!({})),
                )
                .h(33.),
            );
        let pairing = &host["pairing"];
        let code = pairing["code"]
            .as_str()
            .or_else(|| pairing[0].as_str())
            .unwrap_or("");
        if !code.is_empty() {
            sharing = sharing
                .child(label(code, 30., 550., p.ink).selectable(true))
                .child(label(
                    "Enter this temporary code on your other computer.",
                    11.,
                    400.,
                    p.muted,
                ))
                .child(
                    btn(
                        "remote-close-pairing",
                        "Close pairing",
                        p,
                        false,
                        true,
                        |s, _| s.command("remote.pair.close", json!({})),
                    )
                    .h(31.),
                );
        }
        let request = &host["pair_request"];
        if request.is_object() {
            let id = request["id"].clone();
            let reject_id = id.clone();
            sharing = sharing
                .child(divider(p))
                .child(label(
                    format!("{} wants to pair", string(request, "name")),
                    14.,
                    550.,
                    p.ink,
                ))
                .child(label(string(request, "address"), 11., 400., p.muted))
                .child(
                    row()
                        .gap(8.)
                        .flex_wrap()
                        .child(
                            btn(
                                "remote-approve-pair",
                                "Allow pairing",
                                p,
                                true,
                                editable(s),
                                move |s, _| {
                                    s.command("remote.pair.answer", json!({"id":id,"allow":true}))
                                },
                            )
                            .h(33.),
                        )
                        .child(
                            btn(
                                "remote-reject-pair",
                                "Decline",
                                p,
                                false,
                                editable(s),
                                move |s, _| {
                                    s.command(
                                        "remote.pair.answer",
                                        json!({"id":reject_id,"allow":false}),
                                    )
                                },
                            )
                            .h(33.),
                        ),
                );
        }
        content = content.child(sharing);
    }
    let paired = client["paired"].as_bool().unwrap_or(false);
    if paired {
        content = content.child(
            card(p)
                .child(label("Connected speech host", 16., 550., p.ink))
                .child(label(
                    client["name"]
                        .as_str()
                        .or_else(|| client["host_name"].as_str())
                        .unwrap_or("Paired computer"),
                    13.,
                    550.,
                    p.ink,
                ))
                .child(
                    label(
                        client["address"]
                            .as_str()
                            .or_else(|| client["host"].as_str())
                            .unwrap_or(""),
                        12.,
                        400.,
                        p.muted,
                    )
                    .selectable(true),
                )
                .child(error(&client["error"], p))
                .child(
                    row()
                        .gap(8.)
                        .flex_wrap()
                        .child(
                            btn(
                                "remote-disconnect",
                                "Disconnect",
                                p,
                                false,
                                editable(s),
                                |s, _| s.command("remote.disconnect", json!({})),
                            )
                            .h(32.),
                        )
                        .child(
                            btn(
                                "remote-forget",
                                "Forget this computer",
                                p,
                                false,
                                editable(s),
                                |s, _| s.confirmation = Some(json!({"kind":"remote.forget"})),
                            )
                            .h(32.),
                        ),
                )
                .child(confirm(
                    s,
                    p,
                    "remote.forget",
                    "Forget this pairing? You will need to pair again to use this host.",
                    "Forget computer",
                    "remote.forget",
                    json!({}),
                )),
        );
    } else {
        let address = draft(s, "settings:remote:address", "");
        let code = draft(s, "settings:remote:code", "");
        let mut pair=card(p).min_w(0.).child(label("Use another computer’s engine",16.,550.,p.ink))
            .child(input(s,p,"settings:remote:address","Host address","","Computer name or address, with optional port",false))
            .child(input(s,p,"settings:remote:code","Pairing code","","Temporary code shown on the host",true))
            .child(row().gap(8.).flex_wrap()
                .child(btn("remote-pair-code","Pair with code",p,true,editable(s)&&!address.trim().is_empty()&&!code.trim().is_empty(),|s,_| {
                    s.command("remote.pair",json!({"address":draft(s,"settings:remote:address",""),"code":draft(s,"settings:remote:code","")}));
                }).h(33.))
                .child(btn("remote-request-pair","Ask host to approve",p,false,editable(s)&&!address.trim().is_empty(),|s,_|s.command("remote.pair.request",json!({"address":draft(s,"settings:remote:address","")}))).h(33.))
                .child(btn("remote-pair-tailscale","Pair over Tailscale",p,false,editable(s)&&!address.trim().is_empty(),|s,_|s.command("remote.pair",json!({"address":draft(s,"settings:remote:address",""),"tailscale":true}))).h(33.)))
            .child(label("You can also find nearby hosts. Both computers must be reachable on your network or VPN.",11.,400.,p.muted));
        let mut scan = row().gap(8.).flex_wrap();
        for (scope, title) in [
            ("nearby", "Find nearby"),
            ("lan", "Search local network"),
            ("tailnet", "Search Tailscale"),
        ] {
            scan = scan.child(
                btn(
                    format!("remote-scan-{scope}"),
                    title,
                    p,
                    false,
                    editable(s),
                    move |s, _| s.command("remote.scan", json!({"scope":scope})),
                )
                .h(31.),
            );
        }
        pair = pair.child(scan);
        let operation = &state["operation"];
        if !operation.is_null() {
            pair = pair
                .child(label(string(operation, "status"), 11., 550., p.accent))
                .child(error(&operation["error"], p));
            if matches!(string(operation, "status"), "running" | "pending") {
                pair = pair.child(
                    btn(
                        "remote-cancel-pair",
                        "Cancel request",
                        p,
                        false,
                        true,
                        |s, _| s.command("remote.pair.cancel", json!({})),
                    )
                    .h(31.),
                );
            }
            let result = &operation["result"];
            let hosts = if result.is_array() {
                list(result)
            } else {
                list(&result["hosts"])
            };
            for found in hosts {
                let address = found["address"]
                    .as_str()
                    .or_else(|| found["host"].as_str())
                    .unwrap_or("")
                    .to_owned();
                if address.is_empty() {
                    continue;
                }
                let found_address = address.clone();
                pair = pair.child(
                    row()
                        .min_w(0.)
                        .gap(8.)
                        .center_y()
                        .child(
                            label(
                                format!(
                                    "{} · {}",
                                    found["name"].as_str().unwrap_or("Available host"),
                                    address
                                ),
                                12.,
                                400.,
                                p.ink,
                            )
                            .grow(1.)
                            .min_w(0.),
                        )
                        .child(
                            btn(
                                format!("remote-discovered-{address}"),
                                "Choose",
                                p,
                                false,
                                true,
                                move |s, _| {
                                    s.edits.insert(
                                        "settings:remote:address".into(),
                                        TextEdit::new(found_address.clone()),
                                    );
                                },
                            )
                            .h(30.),
                        ),
                );
            }
        }
        content = content.child(pair);
    }
    let mut permissions = card(p)
        .min_w(0.)
        .child(label("Sharing preferences", 15., 550., p.ink));
    for (key, title, description) in [
        (
            "model_management",
            "Allow paired computers to manage models",
            "Remote computers can download and choose speech models on this host.",
        ),
        (
            "keep_records",
            "Keep transcripts from paired computers",
            "Save a local copy of remote sessions.",
        ),
        (
            "manage_mcp",
            "Allow remote MCP management",
            "Paired computers can configure MCP on this computer.",
        ),
        (
            "tailscale_trust",
            "Trust your own Tailscale computers",
            "Allow your own Tailscale devices to pair without a code.",
        ),
    ] {
        permissions = permissions.child(toggle(
            s,
            p,
            &format!("remote-{key}"),
            title,
            description,
            host[key].as_bool().unwrap_or(false),
            "remote.configure",
            key,
        ));
    }
    let initial_port = host["port"].as_u64().unwrap_or(8767).to_string();
    permissions = permissions
        .child(input(
            s,
            p,
            "settings:remote:port",
            "Sharing port",
            &initial_port,
            "8767",
            false,
        ))
        .child(
            btn(
                "remote-save-port",
                "Save port",
                p,
                false,
                editable(s),
                move |s, _| match draft(s, "settings:remote:port", &initial_port).parse::<u16>() {
                    Ok(port) if port >= 1024 => s.command("remote.configure", json!({"port":port})),
                    _ => s.error = Some("Use a port between 1024 and 65535.".into()),
                },
            )
            .h(31.),
        );
    content = content.child(permissions);
    let mut devices = card(p)
        .min_w(0.)
        .child(label("Paired devices", 15., 550., p.ink));
    for device in list(&host["devices"]) {
        let id = string(device, "id").to_owned();
        let name = string(device, "name");
        let key = format!("settings:remote:device:{id}");
        let rename_id = id.clone();
        let rename_key = key.clone();
        let initial_name = name.to_owned();
        let remove_id = id.clone();
        devices=devices.child(divider(p)).child(input(s,p,&key,"Device name",name,"Device name",false))
            .child(row().gap(8.).flex_wrap()
                .child(btn(format!("rename-device-{id}"),"Rename",p,false,editable(s),move |s,_|s.command("remote.devices.rename",json!({"id":rename_id,"name":draft(s,&rename_key,&initial_name)}))).h(30.))
                .child(btn(format!("remove-device-{id}"),"Remove pairing",p,false,editable(s),move |s,_|s.confirmation=Some(json!({"kind":"remote.devices.remove","id":remove_id}))).h(30.)));
        if s.confirmation
            .as_ref()
            .is_some_and(|v| v["kind"] == "remote.devices.remove" && v["id"] == id)
        {
            devices = devices.child(confirm(
                s,
                p,
                "remote.devices.remove",
                "Remove this device’s access to the shared engine?",
                "Remove device",
                "remote.devices.remove",
                json!({"id":id}),
            ));
        }
    }
    if list(&host["devices"]).is_empty() {
        devices = devices.child(label("No devices paired yet.", 12., 400., p.muted));
    }
    content = content.child(devices);
    if paired {
        let sync = &state["sync"];
        let mut storage = card(p)
            .min_w(0.)
            .child(label(
                "Keep your records where they belong",
                15.,
                550.,
                p.ink,
            ))
            .child(label(
                "Choose where transcripts and meetings from this computer are stored.",
                12.,
                400.,
                p.muted,
            ));
        let mut location = row().gap(7.).flex_wrap();
        for (key, title) in [
            ("local", "This computer"),
            ("host", "Paired host"),
            ("both", "Both computers"),
        ] {
            location = location.child(
                btn(
                    format!("remote-storage-{key}"),
                    title,
                    p,
                    string(sync, "location") == key,
                    editable(s),
                    move |s, _| s.command("remote.configure", json!({"records_location":key})),
                )
                .h(31.),
            );
        }
        storage=storage.child(location).child(label(format!("{} records waiting to sync",sync["pending"].as_u64().unwrap_or(0)),11.,400.,p.muted))
            .child(label(string(sync,"waiting"),11.,400.,p.muted)).child(label(string(sync,"active"),11.,550.,p.accent)).child(error(&sync["error"],p))
            .child(row().gap(7.).flex_wrap()
                .child(btn("remote-sync-retry","Retry sync",p,false,editable(s),|s,_|s.command("remote.sync",json!({"action":"retry"}))).h(31.))
                .child(btn("remote-sync-send","Send existing records…",p,false,editable(s),|s,_|s.confirmation=Some(json!({"kind":"remote.sync.send"}))).h(31.))
                .child(btn("remote-sync-bring-back","Bring records back…",p,false,editable(s),|s,_|s.confirmation=Some(json!({"kind":"remote.sync.back"}))).h(31.)))
            .child(confirm(s,p,"remote.sync.send","Send this computer’s existing records to the paired host using your selected storage policy?","Send records","remote.sync",json!({"action":"send_existing"})))
            .child(confirm(s,p,"remote.sync.back","Bring records from the paired host back onto this computer?","Bring records back","remote.sync",json!({"action":"bring_back"})));
        content = content.child(storage);
    }
    content
}

fn backup(s: &State, p: &Palette, layer: &mut Vec<El>) -> El {
    let state = data(s, "backup.state");
    let busy = state["busy"].as_bool().unwrap_or(false);
    let available = editable(s) && !busy;
    let include = draft(s, "settings:backup:include", "true") == "true";
    let mut create=card(p).key("backup-create").min_w(0.)
        .child(label("Keep a copy of your workspace",16.,550.,p.ink))
        .child(label("Back up settings, transcripts, meetings, and walkthroughs to a local archive.",12.,400.,p.muted))
        .child(Switch::new(include).label("Include saved recordings and captures").disabled(!available)
            .on_toggle(move |s:&mut State|{s.edits.insert("settings:backup:include".into(),TextEdit::new((!include).to_string()));})
            .build(&p.kit).key("backup-include-recordings"))
        .child(label("Speech models, API keys, and paired devices are excluded. Archives are not encrypted; choose a private location.",11.,400.,p.muted))
        .child(btn("backup-choose-destination","Create backup…",p,true,available,|_,tx| {
            tx.save_file(FileDialogOptions {title:"Save an OpenWhisper backup".into(),file_name:Some("OpenWhisper.owbackup".into()),..Default::default()}.filter("OpenWhisper backup",&["owbackup"]),|s:&mut State,result,_| {
                match result {Ok(Some(path))=>{s.command("backup.create",json!({"path":path.to_string_lossy(),"include_recordings":draft(s,"settings:backup:include","true")=="true"}));},Ok(None)=>{},Err(e)=>s.error=Some(e.to_string())}
            });
        }).h(34.));
    if state["last_backup"].is_object() {
        let last = &state["last_backup"];
        create = create
            .child(label(
                format!(
                    "Last backup: {}",
                    last["created_at"].as_str().unwrap_or("Saved locally")
                ),
                11.,
                400.,
                p.muted,
            ))
            .child(label(string(last, "path"), 11., 400., p.muted).selectable(true));
    }
    create = create
        .child(error(&state["last_error"], p))
        .child(progress(&state["progress"], p));
    if busy {
        create = create.child(label(
            format!(
                "{} in progress…",
                state["operation"].as_str().unwrap_or("Backup operation")
            ),
            12.,
            550.,
            p.accent,
        ));
        if string(state, "operation") == "backup" {
            create = create.child(
                btn("backup-cancel", "Cancel backup", p, false, true, |s, _| {
                    s.command("backup.cancel", json!({}))
                })
                .h(31.),
            );
        }
    }
    let restore_path = draft(s, "settings:backup:restore_path", "");
    let inspection = &state["inspection"];
    let inspected_path = inspection["path"]
        .as_str()
        .or_else(|| inspection["info"]["path"].as_str())
        .unwrap_or("");
    let inspected = !restore_path.is_empty()
        && inspection.is_object()
        && inspected_path == restore_path
        && inspection["error"].as_str().unwrap_or("").is_empty()
        && inspection["valid"].as_bool() != Some(false);
    let mut restore = card(p)
        .key("backup-restore")
        .min_w(0.)
        .child(label("Inspect and restore", 16., 550., p.ink))
        .child(label(
            "Review a backup before restoring it. Your current workspace is retained for recovery.",
            12.,
            400.,
            p.muted,
        ))
        .child(
            row()
                .gap(8.)
                .flex_wrap()
                .child(
                    btn(
                        "backup-inspect",
                        "Choose backup…",
                        p,
                        false,
                        available,
                        |_, tx| {
                            tx.open_file(
                                FileDialogOptions {
                                    title: "Inspect an OpenWhisper backup".into(),
                                    ..Default::default()
                                }
                                .filter("OpenWhisper backup", &["owbackup"]),
                                |s: &mut State, result, _| match result {
                                    Ok(Some(path)) => {
                                        let path = path.to_string_lossy().into_owned();
                                        s.edits.insert(
                                            "settings:backup:restore_path".into(),
                                            TextEdit::new(path.clone()),
                                        );
                                        s.command("backup.validate", json!({"path":path}));
                                    }
                                    Ok(None) => {}
                                    Err(e) => s.error = Some(e.to_string()),
                                },
                            );
                        },
                    )
                    .h(34.),
                )
                .child(
                    btn(
                        "backup-restore-confirmation",
                        "Restore and restart",
                        p,
                        true,
                        available && inspected,
                        |s, _| s.confirmation = Some(json!({"kind":"backup.restore"})),
                    )
                    .h(34.),
                ),
        );
    if !restore_path.is_empty() {
        restore = restore.child(label(&restore_path, 11., 400., p.muted).selectable(true));
    }
    if inspected {
        let info = if inspection["info"].is_object() {
            &inspection["info"]
        } else {
            inspection
        };
        restore = restore.child(pill("Backup verified", p));
        if let Some(date) = info["created_at"].as_str() {
            restore = restore.child(label(format!("Created {date}"), 11., 400., p.muted));
        }
        if let Some(counts) = info["counts"].as_object() {
            for (kind, count) in counts {
                restore = restore.child(label(
                    format!(
                        "{}: {}",
                        kind.replace('_', " "),
                        fields::render_value(count)
                    ),
                    11.,
                    400.,
                    p.muted,
                ));
            }
        }
    } else if !restore_path.is_empty() {
        restore = restore.child(label("Inspecting the selected backup…", 12., 400., p.muted));
    }
    restore=restore.child(error(&inspection["error"],p))
        .child(confirm(s,p,"backup.restore","Restore this verified backup and restart OpenWhisper? The current workspace will be retained in a recovery folder.","Restore and restart","backup.restore",json!({"path":restore_path,"confirmed":true})));
    if !string(state, "previous_data_dir").is_empty() {
        restore = restore.child(
            label(
                format!("Recovery folder: {}", string(state, "previous_data_dir")),
                11.,
                400.,
                p.muted,
            )
            .selectable(true),
        );
    }
    let initial_frequency = state["frequency"].as_str().unwrap_or("off").to_owned();
    let current_frequency = draft(s, "settings:backup:frequency", &initial_frequency);
    let frequency_schema = json!({"label":"Automatic backup frequency","type":"string","choices":[{"value":"off","label":"Off"},{"value":"daily","label":"Daily"},{"value":"weekly","label":"Weekly"}]});
    let initial_retention = state["retention_count"].as_u64().unwrap_or(5).to_string();
    let initial_destination = string(state, "destination").to_owned();
    let retention = draft(s, "settings:backup:retention", &initial_retention)
        .parse::<u32>()
        .ok();
    let mut schedule=card(p).key("backup-schedule").min_w(0.).child(label("A little peace of mind, automatically",16.,550.,p.ink))
        .child(label("Scheduled backups run while OpenWhisper is open and idle. Older automatic archives are removed after your chosen limit.",12.,400.,p.muted))
        .child(fields::choice(s,p,&frequency_schema,"settings:backup:frequency",&json!(current_frequency),None,layer))
        .child(input(s,p,"settings:backup:destination","Backup folder",&initial_destination,"Choose an existing folder",false))
        .child(btn("backup-schedule-folder","Choose folder…",p,false,available,|_,tx| {
            tx.open_folder(FileDialogOptions {title:"Choose an automatic backup folder".into(),..Default::default()},|s:&mut State,result,_|match result {
                Ok(Some(path))=>{s.edits.insert("settings:backup:destination".into(),TextEdit::new(path.to_string_lossy().into_owned()));},Ok(None)=>{},Err(e)=>s.error=Some(e.to_string())
            });
        }).h(31.))
        .child(input(s,p,"settings:backup:retention","Number of backups to keep",&initial_retention,"1–100",false));
    if retention.is_none_or(|count| !(1..=100).contains(&count)) {
        schedule = schedule.child(label(
            "Keep between 1 and 100 automatic backups.",
            11.,
            400.,
            p.danger,
        ));
    }
    schedule=schedule.child(btn("backup-save-schedule","Save schedule",p,true,available&&retention.is_some_and(|n|(1..=100).contains(&n)),move |s,_| {
        s.command("backup.configure",json!({"frequency":draft(s,"settings:backup:frequency",&initial_frequency),"retention_count":draft(s,"settings:backup:retention",&initial_retention).parse::<u32>().unwrap_or(5),"destination":draft(s,"settings:backup:destination",&initial_destination)}));
    }).h(33.));
    col()
        .gap(18.)
        .min_w(0.)
        .child(create)
        .child(restore)
        .child(schedule)
}

fn updates(s: &State, p: &Palette) -> El {
    let state = data(s, "updates.state");
    let result = &state["result"];
    let release = &result["release"];
    let available = string(result, "status") == "update_available";
    let ready = state["download"]["handoff"].is_object()
        || state["download"]["handoff"]
            .as_str()
            .is_some_and(|s| !s.is_empty());
    let downloading = !string(&state["progress"], "phase").is_empty()
        && !ready
        && string(&state["download"], "error").is_empty();
    let mut overview = card(p)
        .key("updates-overview")
        .min_w(0.)
        .child(label(
            match string(result, "status") {
                "update_available" => "Something new is ready",
                "up_to_date" => "You’re up to date",
                "development" => "You’re running a development version",
                _ => "Keep your workspace up to date",
            },
            17.,
            550.,
            p.ink,
        ))
        .child(label(
            format!(
                "Current version: {}",
                if string(result, "current_version").is_empty() {
                    &s.snapshot.version
                } else {
                    string(result, "current_version")
                }
            ),
            12.,
            400.,
            p.muted,
        ))
        .child(label(string(state, "source_hint"), 12., 400., p.muted))
        .child(error(&state["error"], p))
        .child(error(&state["download"]["error"], p))
        .child(progress(&state["progress"], p))
        .child(
            btn(
                "updates-check",
                "Check for updates",
                p,
                true,
                editable(s) && !downloading,
                |s, _| s.command("updates.check", json!({})),
            )
            .h(34.),
        );
    if !string(result, "git_hint").is_empty() {
        overview =
            overview.child(label(string(result, "git_hint"), 12., 400., p.ink).selectable(true));
    }
    if release.is_object() {
        overview = overview.child(divider(p)).child(label(
            format!("OpenWhisper {}", string(release, "version")),
            16.,
            550.,
            p.ink,
        ));
        if !string(release, "notes").is_empty() {
            overview = overview
                .child(label(string(release, "notes"), 12., 400., p.muted).selectable(true));
        }
        let url = release["url"]
            .as_str()
            .or_else(|| release["html_url"].as_str())
            .unwrap_or("")
            .to_owned();
        if url.starts_with("https://") {
            overview = overview.child(
                btn(
                    "updates-release-page",
                    "View release notes ↗",
                    p,
                    false,
                    true,
                    move |s, tx| {
                        if let Err(e) = tx.open_url(&url) {
                            s.error = Some(e.to_string());
                        }
                    },
                )
                .h(31.),
            );
        }
    }
    if available && result["can_apply"].as_bool() == Some(true) && !ready {
        overview = overview.child(
            btn(
                "updates-download",
                "Download update",
                p,
                true,
                editable(s) && !downloading,
                |s, _| s.command("updates.download", json!({})),
            )
            .h(34.),
        );
    }
    if downloading {
        overview = overview.child(
            btn(
                "updates-cancel",
                "Cancel download",
                p,
                false,
                true,
                |s, _| s.command("updates.cancel", json!({})),
            )
            .h(31.),
        );
    }
    if ready {
        overview = overview
            .child(pill("Download verified", p))
            .child(
                btn(
                    "updates-apply-confirmation",
                    "Install and restart",
                    p,
                    true,
                    editable(s),
                    |s, _| s.confirmation = Some(json!({"kind":"updates.apply"})),
                )
                .h(34.),
            )
            .child(confirm(
                s,
                p,
                "updates.apply",
                "Install the verified update and restart OpenWhisper?",
                "Install and restart",
                "updates.apply",
                json!({"confirmed":true}),
            ));
    }
    overview
}

fn stats(s: &State, p: &Palette) -> El {
    let stats = data(s, "stats.get");
    let periods = list(&stats["periods"]);
    let selected = draft(s, "settings:stats:period", "week");
    let period = periods
        .iter()
        .find(|v| string(v, "key") == selected)
        .unwrap_or(&Value::Null);
    let mut tabs = row().gap(7.).flex_wrap();
    for (key, title) in [
        ("week", "This week"),
        ("month", "This month"),
        ("all", "All time"),
    ] {
        tabs = tabs.child(
            btn(
                format!("stats-period-{key}"),
                title,
                p,
                key == selected,
                true,
                move |s, _| {
                    s.edits
                        .insert("settings:stats:period".into(), TextEdit::new(key));
                },
            )
            .h(31.),
        );
    }
    let words = period["words"].as_u64().unwrap_or(0);
    let pace = period["average_wpm"].as_f64().unwrap_or(0.);
    let saved = if pace > 40. {
        words as f64 / 40. - words as f64 / pace
    } else {
        0.
    };
    let mut cards = row().gap(12.).flex_wrap();
    for (value, title) in [
        (format!("{words}"), "Words spoken"),
        (format!("{pace:.0}"), "Words per minute"),
        (format!("{saved:.0} min"), "Typing time saved"),
    ] {
        cards = cards.child(
            card(p)
                .grow(1.)
                .min_w(130.)
                .child(label(value, 25., 550., p.ink))
                .child(label(title, 11., 400., p.muted)),
        );
    }
    let mut content = col().min_w(0.).gap(18.).child(tabs).child(cards).child(
        card(p)
            .child(label(
                format!("A {} day streak", stats["day_streak"].as_u64().unwrap_or(0)),
                16.,
                550.,
                p.ink,
            ))
            .child(label(
                format!(
                    "{} words spoken across {} captures. Your longest streak is {} days.",
                    stats["words_all_time"].as_u64().unwrap_or(0),
                    stats["entries"].as_u64().unwrap_or(0),
                    stats["longest_streak"].as_u64().unwrap_or(0)
                ),
                12.,
                400.,
                p.muted,
            )),
    );
    let captures = period["dictations"].as_u64().unwrap_or(0);
    let rewrites = period["rewrites"].as_u64().unwrap_or(0);
    let cleaned = period["words_cleaned_up"].as_u64().unwrap_or(0);
    content = content.child(label(
        format!(
            "{captures} dictations · {rewrites} requested rewrites · {cleaned} words cleaned up"
        ),
        11.,
        400.,
        p.muted,
    ));
    if let Some(previous) = period["previous_words"].as_u64().filter(|n| *n > 0) {
        let change = (words as f64 - previous as f64) / previous as f64 * 100.;
        content = content.child(label(
            format!("{change:+.0}% words compared with the preceding period"),
            11.,
            400.,
            p.muted,
        ));
    }
    let bars = list(&period["bars"]);
    if !bars.is_empty() {
        let max = bars.iter().filter_map(|v| v[1].as_f64()).fold(1., f64::max);
        let mut chart = row()
            .key("stats-bars")
            .gap(6.)
            .h(130.)
            .items_end()
            .min_w(0.);
        for (i, bar) in bars.iter().enumerate() {
            let count = bar[1].as_u64().unwrap_or(0);
            let date = bar[0].as_str().unwrap_or("");
            chart = chart.child(
                el().key(format!("stats-bar-{i}"))
                    .grow(1.)
                    .min_w(1.)
                    .h((count as f64 / max * 120.).max(3.) as f32)
                    .radius(3.)
                    .fill(p.accent.alpha(if count == 0 { 0.12 } else { 0.75 }))
                    .help(format!("{date}: {count} words")),
            );
        }
        content = content.child(
            card(p)
                .min_w(0.)
                .child(label("A rhythm of your own", 15., 550., p.ink))
                .child(chart),
        );
    }
    let activity = list(&stats["activity"]);
    if !activity.is_empty() {
        let max = activity
            .iter()
            .filter_map(|v| v[1].as_f64())
            .fold(1., f64::max);
        let mut map = row().gap(3.).min_w(0.).flex_wrap();
        for (week_i, week) in activity.chunks(7).enumerate() {
            let mut column = col().gap(3.);
            for (day_i, day) in week.iter().enumerate() {
                let count = day[1].as_u64().unwrap_or(0);
                let date = day[0].as_str().unwrap_or("");
                column = column.child(
                    el().key(format!("activity-{week_i}-{day_i}"))
                        .size(6., 6.)
                        .radius(1.5)
                        .fill(p.accent.alpha(if count == 0 {
                            0.1
                        } else {
                            0.25 + 0.75 * (count as f64 / max) as f32
                        }))
                        .help(format!("{date}: {count} words")),
                );
            }
            map = map.child(column);
        }
        content = content.child(
            card(p)
                .min_w(0.)
                .child(label("A year in words", 15., 550., p.ink))
                .child(map),
        );
    }
    let apps = list(&period["top_apps"]);
    if !apps.is_empty() {
        let mut group = card(p).child(label("Where your words go", 15., 550., p.ink));
        for app in apps {
            group = group.child(
                row()
                    .gap(12.)
                    .child(
                        label(app[0].as_str().unwrap_or("Other"), 12., 400., p.ink)
                            .grow(1.)
                            .min_w(0.),
                    )
                    .child(label(
                        format!("{} words", app[1].as_u64().unwrap_or(0)),
                        11.,
                        400.,
                        p.muted,
                    )),
            );
        }
        content = content.child(group);
    }
    for (key, title) in [
        ("hours", "When your words arrive"),
        ("weekdays", "Your weekly rhythm"),
    ] {
        let counts = list(&period[key]);
        let max = counts.iter().filter_map(Value::as_f64).fold(0., f64::max);
        if max <= 0. {
            continue;
        }
        let mut chart = row().gap(5.).h(105.).items_end().min_w(0.);
        for (index, count) in counts.iter().enumerate() {
            let count = count.as_f64().unwrap_or(0.);
            let name = if key == "hours" {
                format!("{index:02}:00")
            } else {
                [
                    "Monday",
                    "Tuesday",
                    "Wednesday",
                    "Thursday",
                    "Friday",
                    "Saturday",
                    "Sunday",
                ]
                .get(index)
                .unwrap_or(&"Day")
                .to_string()
            };
            chart = chart.child(
                el().grow(1.)
                    .min_w(1.)
                    .h((count / max * 100.).max(3.) as f32)
                    .radius(3.)
                    .fill(p.accent.alpha(if count > 0. { 0.7 } else { 0.12 }))
                    .help(format!("{name}: {count:.0} words")),
            );
        }
        content = content.child(
            card(p)
                .min_w(0.)
                .child(label(title, 15., 550., p.ink))
                .child(chart)
                .child(label(
                    if key == "hours" {
                        "Midnight → 11 PM, in your local time"
                    } else {
                        "Monday → Sunday"
                    },
                    10.5,
                    400.,
                    p.muted,
                )),
        );
    }
    content.child(label(
        "Statistics stay on your computer. Uploaded audio is excluded from dictation counts.",
        11.,
        400.,
        p.muted,
    )).child(btn("reset-stats-confirmation","Reset statistics…",p,false,editable(s)&&stats["entries"].as_u64().unwrap_or(0)>0,|s,_|s.confirmation=Some(json!({"kind":"stats.reset"}))).h(31.))
        .child(confirm(s,p,"stats.reset","Reset your word counts, activity, and streaks? Your saved history will stay available.","Reset statistics","stats.reset",json!({"confirmed":true})))
}
fn setup(s: &State, p: &Palette) -> El {
    let state = data(s, "setup.state");
    let mut content = card(p)
        .key("setup-checklist")
        .min_w(0.)
        .child(label("A few steps to feel at home", 17., 550., p.ink))
        .child(label(
            "Choose your microphone, set up a speech engine, then make a short test recording.",
            12.,
            400.,
            p.muted,
        ));
    let steps = list(&state["steps"]);
    if steps.is_empty() {
        for (ready, title, description) in [
            (
                state["engine_ready"].as_bool().unwrap_or(false),
                "Speech engine",
                state["engine"]
                    .as_str()
                    .unwrap_or("Choose a local model or connect your speech provider."),
            ),
            (
                !state["hotkeys"].is_null(),
                "Keyboard shortcuts",
                "Start a recording from any application.",
            ),
        ] {
            content = content.child(
                row()
                    .gap(10.)
                    .min_w(0.)
                    .center_y()
                    .child(pill(if ready { "Ready" } else { "To do" }, p))
                    .child(
                        col()
                            .grow(1.)
                            .min_w(0.)
                            .gap(4.)
                            .child(label(title, 13., 550., p.ink))
                            .child(label(description, 11., 400., p.muted)),
                    ),
            );
        }
        content = content.child(label(string(state, "message"), 12., 400., p.muted));
    }
    for step in steps {
        content = content.child(
            row()
                .min_w(0.)
                .gap(10.)
                .center_y()
                .child(pill(
                    if step["complete"].as_bool() == Some(true) {
                        "Done"
                    } else {
                        "To do"
                    },
                    p,
                ))
                .child(
                    col()
                        .min_w(0.)
                        .grow(1.)
                        .gap(4.)
                        .child(label(
                            step["title"]
                                .as_str()
                                .or_else(|| step["label"].as_str())
                                .unwrap_or("Setup step"),
                            13.,
                            550.,
                            p.ink,
                        ))
                        .child(label(string(step, "description"), 11., 400., p.muted)),
                ),
        );
    }
    content = content
        .child(
            row()
                .gap(8.)
                .flex_wrap()
                .child(
                    btn(
                        "setup-microphone",
                        "Choose microphone",
                        p,
                        false,
                        true,
                        |s, _| {
                            s.selected_tab = "settings:overview".into();
                            s.command("devices.list", json!({}));
                        },
                    )
                    .h(32.),
                )
                .child(
                    btn(
                        "setup-model",
                        "Choose speech model",
                        p,
                        false,
                        true,
                        |s, _| {
                            s.selected_tab = "settings:models".into();
                            s.command("models.list", json!({}));
                        },
                    )
                    .h(32.),
                )
                .child(
                    btn("setup-record", "Try a recording", p, false, true, |s, _| {
                        s.navigate(crate::state::Page::Record)
                    })
                    .h(32.),
                ),
        )
        .child(divider(p))
        .child(
            row()
                .gap(8.)
                .flex_wrap()
                .child(
                    btn(
                        "setup-complete",
                        "Finish setup",
                        p,
                        true,
                        editable(s),
                        |s, _| s.command("setup.complete", json!({})),
                    )
                    .h(33.),
                )
                .child(
                    btn(
                        "setup-skip",
                        "Do this later",
                        p,
                        false,
                        editable(s),
                        |s, _| s.command("setup.skip", json!({})),
                    )
                    .h(33.),
                ),
        );
    content
}

fn mcp(s: &State, p: &Palette) -> El {
    let state = data(s, "mcp.state");
    let running = string(state, "state") == "running";
    let enabled = s.data["settings.values"]["values"]["mcp_enabled"]
        .as_bool()
        .unwrap_or(false);
    let port = state["port"].as_u64().unwrap_or(8767).to_string();
    let mut content=card(p).key("mcp-onboarding").min_w(0.)
        .child(row().gap(10.).center_y().flex_wrap().child(label("Let your tools listen",17.,550.,p.ink).grow(1.).min_w(0.)).child(pill(if running {"Running"}else{"Offline"},p)))
        .child(label("Connect compatible AI assistants to your local OpenWhisper workspace through MCP. Your access token stays hidden until you choose to copy a configuration.",12.,400.,p.muted))
        .child(toggle(s,p,"mcp-server-enabled","Enable the local MCP server","Allow authenticated assistants to access the capabilities you enable.",enabled,"settings.update","mcp_enabled"))
        .child(label(string(state,"message"),12.,400.,p.muted))
        .child(label(string(state,"url"),12.,550.,p.accent).selectable(true));
    if !string(state, "remote_url").is_empty() {
        content = content.child(
            label(
                format!("Tailscale: {}", string(state, "remote_url")),
                11.,
                400.,
                p.muted,
            )
            .selectable(true),
        );
    }
    let mut copy = row().gap(8.).flex_wrap();
    for (format, title) in [
        ("claude", "Copy Claude setup"),
        ("chatgpt", "Copy ChatGPT setup"),
        ("config", "Copy MCP configuration"),
        ("prompt", "Copy agent instructions"),
        ("token", "Copy access token"),
    ] {
        copy = copy.child(
            btn(
                format!("mcp-copy-{format}"),
                title,
                p,
                false,
                s.available() && running,
                move |s, _| s.command("mcp.copy", json!({"format":format})),
            )
            .h(32.),
        );
    }
    content = content
        .child(copy)
        .child(divider(p))
        .child(input(
            s,
            p,
            "settings:mcp:port",
            "Local server port",
            &port,
            "8767",
            false,
        ))
        .child(
            btn(
                "mcp-save-port",
                "Save port",
                p,
                false,
                editable(s),
                move |s, _| match draft(s, "settings:mcp:port", &port).parse::<u16>() {
                    Ok(value) if value >= 1024 => {
                        s.command("settings.update", json!({"key":"mcp_port","value":value}))
                    }
                    _ => s.error = Some("Use a port between 1024 and 65535.".into()),
                },
            )
            .h(31.),
        )
        .child(
            btn(
                "mcp-access-preferences",
                "Configure API access & permissions →",
                p,
                false,
                true,
                |s, _| {
                    s.selected_tab = "settings:category:automation".into();
                    s.open_select = None;
                },
            )
            .h(32.),
        );
    content
}
