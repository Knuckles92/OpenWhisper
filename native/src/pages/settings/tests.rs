use super::*;
use crate::state::Page;
use rune::{Env, Key, KeyEvent, Mods, Runtime};

fn workspace(tab: &str) -> Runtime<State> {
    let mut s = State::new();
    s.demo = true;
    s.snapshot.ready = true;
    s.page = Page::Settings;
    s.selected_tab = tab.into();
    demo::seed(&mut s);
    let mut rt = Runtime::new(
        s,
        crate::view,
        (1220., 1400.),
        Env {
            system_dark: false,
            scale: 1.,
        },
    );
    settle(&mut rt);
    rt
}
fn settle(rt: &mut Runtime<State>) {
    for _ in 0..30 {
        rt.frame(1. / 60.);
    }
}
fn tap(rt: &mut Runtime<State>, key: &str) {
    let (x, y) = rt.find(key).unwrap_or_else(|| panic!("missing {key}"));
    rt.pointer_down(x, y);
    rt.pointer_up(x, y);
    settle(rt);
}
fn key(rt: &mut Runtime<State>, key: Key) {
    rt.key(KeyEvent {
        key,
        mods: Mods::default(),
    });
    settle(rt);
}

#[test]
fn section_picker_opens_a_keyboard_accessible_settings_category() {
    let mut rt = workspace("settings:overview");
    tap(&mut rt, "settings-section-picker-trigger");
    assert_eq!(rt.state.open_select.as_deref(), Some("settings:sections"));
    key(&mut rt, Key::Text("Dictation".into()));
    key(&mut rt, Key::Enter);
    assert_eq!(rt.state.selected_tab, "settings:category:dictation");
    assert!(rt.find("toggle-auto_paste").is_some());
    assert!(rt.state.open_select.is_none());
}
#[test]
fn setting_switch_saves_and_search_finds_schema_descriptions() {
    let mut rt = workspace("settings:category:dictation");
    tap(&mut rt, "toggle-auto_paste");
    assert_eq!(
        rt.state.data["settings.values"]["values"]["auto_paste"],
        json!(false)
    );
    rt.update(|s, _| {
        s.edits
            .insert("settings:search".into(), TextEdit::new("punctuation"));
    });
    settle(&mut rt);
    assert!(rt.find("toggle-transcript_cleanup_enabled").is_some());
    assert!(rt.find("toggle-auto_paste").is_none());
}
#[test]
fn seeded_field_can_be_replaced_and_saved_with_the_keyboard() {
    let mut rt = workspace("settings:category:history");
    tap(&mut rt, "input-settings:field:max_saved_recordings");
    key(&mut rt, Key::Command('a'));
    key(&mut rt, Key::Text("120".into()));
    key(&mut rt, Key::Enter);
    assert_eq!(
        rt.state.data["settings.values"]["values"]["max_saved_recordings"],
        json!(120)
    );
}
#[test]
fn invalid_port_stays_unsaved_and_escape_discards_the_draft() {
    let mut rt = workspace("settings:category:automation");
    rt.update(|s, _| {
        s.edits
            .insert("settings:field:mcp_port".into(), TextEdit::new("70000"));
    });
    settle(&mut rt);
    tap(&mut rt, "save-setting-mcp_port");
    assert_eq!(
        rt.state.data["settings.values"]["values"]["mcp_port"],
        json!(8766)
    );
    tap(&mut rt, "input-settings:field:mcp_port");
    key(&mut rt, Key::Escape);
    assert_eq!(
        rt.state
            .edits
            .get("settings:field:mcp_port")
            .map(|e| e.text.as_str()),
        Some("8766")
    );
}
#[test]
fn provider_secret_is_masked_and_clears_after_successful_save() {
    let mut rt = workspace("settings:credentials");
    rt.update(|s, _| {
        s.edits.insert(
            "settings:credential:OPENAI_API_KEY".into(),
            TextEdit::new("sk-do-not-display-this-key"),
        );
    });
    settle(&mut rt);
    assert!(
        rt.elements()
            .iter()
            .filter_map(|e| e.text.as_deref())
            .all(|t| !t.contains("sk-do-not-display-this-key"))
    );
    tap(&mut rt, "save-credential-OPENAI_API_KEY");
    assert!(
        rt.state
            .edits
            .get("settings:credential:OPENAI_API_KEY")
            .is_none_or(|e| e.text.is_empty())
    );
    assert_eq!(
        rt.state.data["settings.schema"]["credentials"][0]["configured"],
        json!(true)
    );
}
#[test]
fn dictionary_editor_creates_an_item_and_requires_delete_confirmation() {
    let mut rt = workspace("settings:collection:dictionary");
    rt.update(|s, _| {
        demo::command(s, "collections.list", &json!({"collection":"dictionary"}));
    });
    settle(&mut rt);
    tap(&mut rt, "new-dictionary");
    rt.update(|s, _| {
        s.edits.insert(
            "settings:item:dictionary:term".into(),
            TextEdit::new("Anomaly"),
        );
    });
    settle(&mut rt);
    tap(&mut rt, "save-item-dictionary");
    assert_eq!(
        rt.state.data["collections.list"]["items"]
            .as_array()
            .unwrap()
            .len(),
        3
    );
    tap(&mut rt, "edit-dictionary-demo-2");
    tap(&mut rt, "delete-item-dictionary");
    assert_eq!(
        rt.state.data["collections.list"]["items"]
            .as_array()
            .unwrap()
            .len(),
        3
    );
    tap(&mut rt, "confirm-collection-delete");
    assert_eq!(
        rt.state.data["collections.list"]["items"]
            .as_array()
            .unwrap()
            .len(),
        2
    );
}
#[test]
fn all_schema_sections_keep_text_inside_small_and_large_windows() {
    for width in [720., 1220.] {
        let mut rt = workspace("settings:overview");
        let mut tabs = vec![
            "settings:overview".to_string(),
            "settings:models".into(),
            "settings:credentials".into(),
        ];
        tabs.extend(
            list(&rt.state.data["settings.schema"]["categories"])
                .iter()
                .map(|c| format!("settings:category:{}", string(c, "id"))),
        );
        tabs.extend(
            system::SECTIONS
                .iter()
                .map(|(id, _, _)| format!("settings:system:{id}")),
        );
        tabs.extend(
            list(&rt.state.data["settings.schema"]["collections"])
                .iter()
                .map(|c| format!("settings:collection:{}", string(c, "id"))),
        );
        rt.set_viewport((width, 1400.), 1.);
        for tab in tabs {
            rt.update(|s, _| select_tab(s, tab.clone()));
            settle(&mut rt);
            for node in rt
                .elements()
                .iter()
                .filter(|e| e.text.is_some() && !e.exiting)
            {
                assert!(
                    node.rect[0] >= -0.5 && node.rect[0] + node.rect[2] <= width + 0.5,
                    "{tab} overflowed at {width}: {:?} {:?}",
                    node.text,
                    node.rect
                );
            }
        }
    }
}

#[test]
fn restore_requires_a_verified_matching_archive_and_a_final_confirmation() {
    let mut rt = workspace("settings:system:backup");
    rt.update(|s, _| {
        s.edits.insert(
            "settings:backup:restore_path".into(),
            TextEdit::new("/tmp/selected.owbackup"),
        );
    });
    settle(&mut rt);
    tap(&mut rt, "backup-restore-confirmation");
    assert!(rt.state.confirmation.is_none());
    rt.update(|s, _| {
        s.data["backup.state"]["inspection"] =
            json!({"valid":true,"path":"/tmp/other.owbackup","info":{}});
    });
    settle(&mut rt);
    tap(&mut rt, "backup-restore-confirmation");
    assert!(rt.state.confirmation.is_none());
    rt.update(|s, _| {
        s.data["backup.state"]["inspection"] =
            json!({"valid":true,"path":"/tmp/selected.owbackup","info":{}});
    });
    settle(&mut rt);
    tap(&mut rt, "backup-restore-confirmation");
    assert!(rt.state.confirmation.is_some());
    assert!(rt.state.data["_demo.last_system_command"].is_null());
    tap(&mut rt, "confirm-backup.restore");
    assert_eq!(
        rt.state.data["_demo.last_system_command"]["method"],
        json!("backup.restore")
    );
    assert_eq!(
        rt.state.data["_demo.last_system_command"]["params"]["confirmed"],
        json!(true)
    );
}
#[test]
fn update_installation_requires_download_and_confirmation() {
    let mut rt = workspace("settings:system:updates");
    assert!(rt.find("updates-apply-confirmation").is_none());
    tap(&mut rt, "updates-download");
    assert!(rt.find("updates-apply-confirmation").is_some());
    tap(&mut rt, "updates-apply-confirmation");
    assert_eq!(
        rt.state.data["_demo.last_system_command"]["method"],
        json!("updates.download")
    );
    tap(&mut rt, "confirm-updates.apply");
    assert_eq!(
        rt.state.data["_demo.last_system_command"]["method"],
        json!("updates.apply")
    );
}

#[test]
fn native_package_updates_open_releases_without_exposing_stale_installer_actions() {
    let mut rt = workspace("settings:system:updates");
    let releases = "https://github.com/Knuckles92/OpenWhisper/releases";
    rt.update(|s, _| {
        s.data["updates.state"] = json!({"manual_update":true,"releases_url":releases,
            "source_hint":"Download the native package for your platform from the release page.",
            "error":"The update check could not reach the server.","result":null,
            "download":{"handoff":{"path":"/tmp/stale-qt-installer","verified":true}},
            "progress":{"phase":"Downloading","done":10,"total":20}});
        s.confirmation = Some(json!({"kind":"updates.apply"}));
    });
    settle(&mut rt);
    assert!(rt.find("updates-open-release-page").is_some());
    for action in [
        "updates-download",
        "updates-apply-confirmation",
        "confirm-updates.apply",
        "updates-cancel",
    ] {
        assert!(rt.find(action).is_none(), "native package exposed {action}");
    }
    assert!(
        rt.elements()
            .iter()
            .filter_map(|e| e.text.as_deref())
            .all(|text| !text.contains("Download verified") && !text.contains("Downloading"))
    );
    tap(&mut rt, "updates-open-release-page");
    assert_eq!(rt.take_url_requests(), vec![releases.to_owned()]);
    assert!(rt.state.data["_demo.last_system_command"].is_null());

    // Even a stale installable result and an in-flight download cannot expose
    // legacy installer controls in a native package.
    rt.update(|s, _| {
        s.data["updates.state"]["result"] = json!({"status":"update_available","can_apply":true});
        s.data["updates.state"]["download"] = json!({});
    });
    settle(&mut rt);
    for action in [
        "updates-download",
        "updates-apply-confirmation",
        "confirm-updates.apply",
        "updates-cancel",
    ] {
        assert!(rt.find(action).is_none(), "stale state exposed {action}");
    }
    assert!(rt.find("updates-open-release-page").is_some());
}

#[test]
fn native_package_release_link_requires_https() {
    let mut rt = workspace("settings:system:updates");
    for url in [
        "file:///tmp/installer",
        "javascript:alert(1)",
        "http://example.com/releases",
    ] {
        rt.update(|s, _| {
            s.data["updates.state"] = json!({"manual_update":true,"releases_url":url});
        });
        settle(&mut rt);
        assert!(rt.find("updates-open-release-page").is_none());
        assert!(rt.take_url_requests().is_empty());
    }
}

#[test]
fn shortcut_recorder_suspends_captures_and_resumes_global_hotkeys() {
    let mut rt = workspace("settings:collection:hotkeys");
    rt.update(|s, _| {
        demo::command(s, "collections.list", &json!({"collection":"hotkeys"}));
    });
    settle(&mut rt);
    tap(&mut rt, "edit-hotkeys-hotkey");
    tap(&mut rt, "record-hotkey-settings:item:hotkeys:value");
    assert_eq!(rt.state.data["hotkeys.capture"]["active"], json!(true));
    rt.key(KeyEvent {
        key: Key::Text("r".into()),
        mods: Mods {
            ctrl: true,
            alt: true,
            ..Mods::default()
        },
    });
    settle(&mut rt);
    assert_eq!(
        rt.state.edits["settings:item:hotkeys:value"].text,
        "ctrl+alt+r"
    );
    assert_eq!(rt.state.data["hotkeys.capture"]["active"], json!(false));
    assert!(rt.state.data["settings.hotkey_capture"].is_null());
}
#[test]
fn shortcut_recorder_cancel_keeps_the_saved_draft_and_resumes_hotkeys() {
    let mut rt = workspace("settings:collection:hotkeys");
    rt.update(|s, _| {
        demo::command(s, "collections.list", &json!({"collection":"hotkeys"}));
    });
    settle(&mut rt);
    tap(&mut rt, "edit-hotkeys-hotkey");
    tap(&mut rt, "record-hotkey-settings:item:hotkeys:value");
    key(&mut rt, Key::Escape);
    assert_eq!(
        rt.state.edits["settings:item:hotkeys:value"].text,
        "ctrl+alt+space"
    );
    assert_eq!(rt.state.data["hotkeys.capture"]["active"], json!(false));
}
#[test]
fn service_refresh_updates_untouched_fields_and_preserves_unsaved_changes() {
    let mut s = State::new();
    demo::seed(&mut s);
    s.edits
        .insert("settings:field:mcp_port".into(), TextEdit::new("8766"));
    settings_response(
        &mut s,
        "settings.values",
        &json!({"values":{"mcp_port":8877}}),
    );
    assert_eq!(s.edits["settings:field:mcp_port"].text, "8877");
    s.edits.get_mut("settings:field:mcp_port").unwrap().text = "9000".into();
    settings_response(
        &mut s,
        "settings.values",
        &json!({"values":{"mcp_port":9988}}),
    );
    assert_eq!(s.edits["settings:field:mcp_port"].text, "9000");
}

#[test]
fn microphone_backups_survive_primary_switching_and_can_be_removed() {
    let mut rt = workspace("settings:overview");
    tap(&mut rt, "backup-device-1");
    assert_eq!(
        rt.state.data["devices.list"]["priority"]
            .as_array()
            .unwrap()
            .len(),
        2
    );
    tap(&mut rt, "select-device-1");
    assert_eq!(
        rt.state.data["devices.list"]["priority"][0]["name"],
        json!("Studio USB microphone")
    );
    assert_eq!(
        rt.state.data["devices.list"]["priority"][1]["name"],
        json!("Built-in microphone")
    );
    tap(&mut rt, "microphone-backup-remove-1");
    assert_eq!(
        rt.state.data["devices.list"]["priority"]
            .as_array()
            .unwrap()
            .len(),
        1
    );
}
#[test]
fn optional_runtime_removal_requires_confirmation() {
    let mut rt = workspace("settings:system:components");
    tap(&mut rt, "remove-component-parakeet");
    assert_eq!(
        rt.state.data["components.list"]["items"][0]["state"],
        json!("installed")
    );
    tap(&mut rt, "keep-component-parakeet");
    assert!(rt.state.confirmation.is_none());
    tap(&mut rt, "remove-component-parakeet");
    tap(&mut rt, "confirm-remove-component-parakeet");
    assert_eq!(
        rt.state.data["components.list"]["items"][0]["state"],
        json!("not_installed")
    );
}

#[test]
fn escape_with_modifiers_is_a_shortcut_while_bare_escape_cancels() {
    let mut rt = workspace("settings:collection:hotkeys");
    rt.update(|s, _| {
        demo::command(s, "collections.list", &json!({"collection":"hotkeys"}));
    });
    settle(&mut rt);
    tap(&mut rt, "edit-hotkeys-hotkey");
    tap(&mut rt, "record-hotkey-settings:item:hotkeys:value");
    rt.key(KeyEvent {
        key: Key::Escape,
        mods: Mods {
            ctrl: true,
            alt: true,
            ..Mods::default()
        },
    });
    settle(&mut rt);
    assert_eq!(
        rt.state.edits["settings:item:hotkeys:value"].text,
        "ctrl+alt+esc"
    );
    assert_eq!(rt.state.data["hotkeys.capture"]["active"], json!(false));
}
