//! Deterministic, offline samples for rendering and interaction verification.
use crate::state::State;
use serde_json::{Value, json};

pub fn seed(s: &mut State) {
    s.data["settings.schema"] = json!({
        "categories":[
            {"id":"dictation","label":"Dictation","description":"Capture thoughts with fewer interruptions.","fields":[
                {"key":"auto_paste","label":"Paste into the active app","description":"Send finished dictation to the app you’re working in.","type":"boolean","value":true},
                {"key":"copy_clipboard","label":"Keep a copy on the clipboard","description":"Make your latest dictation easy to paste again.","type":"boolean","value":true},
                {"key":"local_asr_language","label":"Dictation language","description":"Supported languages depend on your selected speech engine.","type":"string","value":"en","choices":[{"value":"","label":"Detect automatically"},{"value":"en","label":"English"},{"value":"es","label":"Spanish"},{"value":"fr","label":"French"},{"value":"de","label":"German"},{"value":"ja","label":"Japanese"}]}
            ]},
            {"id":"cleanup","label":"AI cleanup","description":"Shape your words while keeping your meaning.","fields":[
                {"key":"transcript_cleanup_enabled","label":"AI cleanup","description":"Tidy punctuation and phrasing using your preferred text model.","type":"boolean","value":false},
                {"key":"transcript_cleanup_level","label":"Cleanup strength","description":"Choose how much your transcript should be revised.","type":"string","value":"light","choices":[{"value":"light","label":"A light touch"},{"value":"medium","label":"Balanced"},{"value":"high","label":"A thorough edit"}]},
                {"key":"transcript_cleanup_instructions","label":"Custom instructions","description":"Tell your text model how you like to write.","type":"multiline","value":"Keep my tone natural. Preserve technical terms and proper names."}
            ]},
            {"id":"appearance","label":"Appearance","fields":[{"key":"ui_font_scale","label":"Interface text size","description":"A comfortable reading size for your screen.","type":"integer","value":100,"choices":[{"value":90,"label":"Small · 90%"},{"value":100,"label":"Default · 100%"},{"value":125,"label":"Large · 125%"}]}]},
            {"id":"meetings","label":"Meetings","fields":[{"key":"meeting_language","label":"Meeting language","description":"Language hint for longer conversations.","type":"string","value":"en"}]},
            {"id":"history","label":"History & storage","fields":[{"key":"max_saved_recordings","label":"Saved recordings","description":"The number of recordings to keep on this computer.","type":"integer","value":250,"min":0,"max":1000000}]},
            {"id":"automation","label":"Automation & MCP","fields":[{"key":"mcp_enabled","label":"Allow local MCP connections","description":"Connect compatible local tools to OpenWhisper.","type":"boolean","value":false},{"key":"mcp_port","label":"MCP port","type":"integer","value":8766,"min":1024,"max":65535}]},
            {"id":"remote","label":"Remote computers","fields":[{"key":"remote_host_enabled","label":"Share this computer’s speech engines","type":"boolean","value":false}]}
        ],
        "collections":[
            {"id":"dictionary","label":"Dictionary","description":"A home for names, terms, and the words that matter to you.","fields":[{"key":"term","label":"Preferred spelling","type":"string","required":true},{"key":"heard","label":"Sounds like","type":"json","default":[],"description":"Alternative spellings as a list, for example [\"Rune\", \"rune\"]."},{"key":"starred","label":"Favorite","type":"boolean","default":false}]},
            {"id":"snippets","label":"Snippets","description":"Say a short phrase to insert something longer.","fields":[{"key":"trigger","label":"Spoken phrase","type":"string","required":true},{"key":"text","label":"Expansion","type":"multiline","required":true},{"key":"formatted","label":"Formatted Markdown","type":"boolean","default":false}]},
            {"id":"profiles","label":"Cleanup profiles","description":"A different writing style for every situation.","fields":[{"key":"name","label":"Name","type":"string","required":true},{"key":"instructions","label":"Output instructions","type":"multiline"},{"key":"hotkey","label":"Shortcut","type":"string"}]},
            {"id":"transforms","label":"Text transforms","fields":[{"key":"name","label":"Name","type":"string","required":true},{"key":"instruction","label":"Rewrite instructions","type":"multiline"}]},
            {"id":"actions","label":"Actions","fields":[{"key":"name","label":"Name","type":"string","required":true},{"key":"kind","label":"Action","type":"string","default":"clipboard","choices":[{"value":"clipboard","label":"Clipboard"},{"value":"webhook","label":"Webhook"}]},{"key":"params","label":"Parameters","type":"json","default":{}}]},
            {"id":"hotkeys","label":"Keyboard shortcuts","fields":[{"key":"value","label":"Shortcut","type":"string"}]},
            {"id":"text_models","label":"Custom text endpoints","fields":[{"key":"name","label":"Name","type":"string","required":true},{"key":"base_url","label":"API base URL","type":"string","required":true}]},
            {"id":"speech_models","label":"Custom Whisper models","fields":[{"key":"source","label":"Local folder or Hugging Face owner/model[/subfolder]","type":"string","required":true}]},
            {"id":"engine_devices","label":"Speech engine devices","can_add":false,"can_delete":false,"fields":[{"key":"device","label":"Run on","type":"string","choices":[{"value":"auto","label":"Choose automatically"},{"value":"cpu","label":"CPU"},{"value":"cuda","label":"NVIDIA GPU"}]}]}
        ],
        "credentials":[{"name":"OPENAI_API_KEY","label":"OpenAI","configured":false,"secret":true},{"name":"OPENROUTER_API_KEY","label":"OpenRouter","configured":true,"secret":true},{"name":"GROQ_API_KEY","label":"Groq","configured":false,"secret":true},{"name":"HF_TOKEN","label":"Hugging Face","configured":false,"secret":true}]
    });
    let mut values = json!({});
    for category in s.data["settings.schema"]["categories"].as_array().unwrap() {
        for field in category["fields"].as_array().unwrap() {
            values[field["key"].as_str().unwrap()] = field["value"].clone();
        }
    }
    s.data["settings.values"] = json!({"values":values});
    s.data["models.list"] = json!({"items":[
        {"id":"parakeet","name":"Parakeet","family":"NVIDIA · Local speech","description":"Fast, accurate everyday dictation that stays on your computer.","installed":true,"selected":true,"size":"600 MB","status":"installed","can_download":true,"can_remove":true},
        {"id":"local_whisper","name":"Whisper Large v3 Turbo","family":"OpenAI · Local speech","description":"Multilingual recognition with a thoughtful balance of speed and accuracy.","installed":false,"selected":false,"size":"1.6 GB","status":"available","can_download":true,"can_remove":false},
        {"id":"moonshine","name":"Moonshine","family":"Useful Sensors · Local speech","description":"A lighter model for quick captures on smaller computers.","installed":false,"selected":false,"size":"190 MB","status":"available","can_download":true,"can_remove":false},
        {"id":"api","name":"Cloud speech providers","family":"OpenAI & Groq · API","description":"Use a hosted speech model with your own provider key.","installed":false,"selected":false,"status":"available","can_download":false,"can_remove":false}
    ]});
    s.data["devices.list"] = json!({"items":[{"id":0,"name":"Built-in microphone","default":true,"key":{"name":"Built-in microphone","hostapi":"Core Audio"}},{"id":1,"name":"Studio USB microphone","default":false,"key":{"name":"Studio USB microphone","hostapi":"Core Audio"}}],"selected":0,"priority":[{"name":"Built-in microphone","hostapi":"Core Audio"}]});
    s.data["components.list"] = json!({"items":[
        {"component_id":"parakeet","display_name":"Parakeet speech runtime","summary":"Fast local dictation on Linux, Windows, and macOS.","state":"installed","installed_version":"0.4.2"},
        {"component_id":"qwen_asr","display_name":"Qwen ASR speech runtime","summary":"Multilingual recognition with an optional local speech engine.","state":"not_installed","available_version":"0.2.0"},
        {"component_id":"meeting_agent","display_name":"Local meeting assistant","summary":"Keep meeting summaries and action items on this computer.","state":"update_available","installed_version":"1.2.0","available_version":"1.3.0"}
    ]});
    s.data["mcp.state"] = json!({"state":"stopped","message":"Enable local connections to connect your AI assistant.","port":8766});
    s.data["_demo.collections"] = json!({
        "dictionary":[{"id":"rune","term":"Rune","heard":["roon","rune"],"starred":true},{"id":"openwhisper","term":"OpenWhisper","heard":["open whisper"],"starred":false}],
        "snippets":[{"id":"signature","trigger":"insert my signature","text":"Best,\nAlex","formatted":false}],
        "profiles":[{"id":"clear","name":"Clear and natural","instructions":"Keep my meaning and make each sentence easy to read.","hotkey":"ctrl+alt+1"}],
        "hotkeys":[{"id":"hotkey","name":"Record shortcut","value":"ctrl+alt+space"},{"id":"paste_hotkey","name":"Paste latest transcript","value":"ctrl+alt+v"}],
        "engine_devices":[{"id":"parakeet","name":"Parakeet","device":"auto"},{"id":"local_whisper","name":"Local Whisper","device":"cpu"}]
    });
    s.data["remote.state"] = json!({"host":{"enabled":false,"running":false,"port":8767,"host_name":"Studio desktop","address":"192.168.1.42","devices":[],"clients":[],"pairing":null,"pair_request":null,"model_management":false,"keep_records":false,"manage_mcp":false,"tailscale_trust":false},"client":{"paired":false},"operation":null});
    s.data["backup.state"] = json!({"busy":false,"operation":"","frequency":"weekly","retention_count":5,"destination":"/home/alex/Backups","last_error":"","last_backup":{"created_at":"November 5, 2026","path":"/home/alex/Backups/OpenWhisper.owbackup"},"inspection":null,"progress":null});
    s.data["updates.state"] = json!({"channel":"installer","source_hint":"Stable releases for your desktop.","error":"","progress":null,"download":{"handoff":null,"error":""},"result":{"status":"update_available","current_version":"1.4.0","can_apply":true,"release":{"version":"1.5.0","notes":"A calmer native workspace, faster model switching, and thoughtful improvements to everyday dictation.","url":"https://github.com/Knuckles92/OpenWhisper/releases"}}});
    let bars: Vec<_> = [420, 865, 610, 1340, 980, 1520, 840]
        .iter()
        .enumerate()
        .map(|(i, n)| json!([format!("2026-11-{:02}", i + 1), n]))
        .collect();
    let activity: Vec<_> = (0..91)
        .map(|i| {
            json!([
                format!("Day {}", i + 1),
                if i % 6 == 0 { 0 } else { (i * 137) % 1600 }
            ])
        })
        .collect();
    s.data["stats.get"] = json!({"words_all_time":48620,"entries":642,"day_streak":12,"longest_streak":24,"activity":activity,"periods":[{"key":"week","words":6575,"dictations":42,"average_wpm":146.,"bars":bars,"top_apps":[["Notes",2840],["Mail",1920],["Code editor",1815]]},{"key":"month","words":24560,"dictations":168,"average_wpm":142.,"bars":bars,"top_apps":[["Notes",12840],["Mail",6920],["Code editor",4815]]},{"key":"all","words":48620,"dictations":642,"average_wpm":144.,"bars":bars,"top_apps":[["Notes",22840],["Mail",16920],["Code editor",8815]]}]});
    s.data["setup.state"] = json!({"complete":false,"steps":[{"id":"microphone","title":"Choose your microphone","description":"Your built-in microphone is ready.","complete":true},{"id":"model","title":"Set up a speech engine","description":"Parakeet is installed and selected.","complete":true},{"id":"recording","title":"Capture your first thought","description":"Try a short recording to get a feel for your new workspace.","complete":false}]});
}
pub fn command(s: &mut State, method: &str, params: &Value) -> bool {
    match method {
        "hotkeys.capture" => {
            s.data["hotkeys.capture"] = json!({"ok":true,"active":params["active"]});
        }
        "settings.schema" | "settings.values" | "models.list" | "devices.list"
        | "collections.schema" | "remote.state" | "backup.state" | "updates.state"
        | "stats.get" | "setup.state" | "components.list" | "mcp.state" | "operations.list"
        | "collections.tests" => {}
        "components.install" | "components.remove" | "components.cancel" => {
            s.data["_demo.last_system_command"] = json!({"method":method,"params":params});
            if let Some(items) = s.data["components.list"]["items"].as_array_mut() {
                for item in items
                    .iter_mut()
                    .filter(|item| item["component_id"] == params["id"])
                {
                    item["state"] = json!(if method == "components.remove" {
                        "not_installed"
                    } else {
                        "installed"
                    });
                }
            }
        }
        "settings.update" => {
            if let Some(key) = params["key"].as_str() {
                s.snapshot.settings[key] = params["value"].clone();
                s.data["settings.values"]["values"][key] = params["value"].clone();
                s.notice = Some("Preference saved".into());
            }
        }
        "devices.select" => {
            s.data["devices.list"]["selected"] = params["id"].clone();
        }
        "devices.priority" => {
            s.data["devices.list"]["priority"] = params["priority"].clone();
            if let Some(id) = s.data["devices.list"]["items"]
                .as_array()
                .and_then(|items| {
                    items
                        .iter()
                        .find(|item| item["key"] == params["priority"][0])
                })
                .map(|item| item["id"].clone())
            {
                s.data["devices.list"]["selected"] = id;
            }
        }
        "collections.list" => load_collection(s, params["collection"].as_str().unwrap_or("")),
        "collections.save" => {
            let collection = params["collection"].as_str().unwrap_or("");
            let mut item = params["item"].clone();
            if item["id"].is_null() {
                item["id"] = json!(format!(
                    "demo-{}",
                    s.data["_demo.collections"][collection]
                        .as_array()
                        .map(Vec::len)
                        .unwrap_or(0)
                ));
            }
            if !s.data["_demo.collections"][collection].is_array() {
                s.data["_demo.collections"][collection] = json!([]);
            }
            let items = s.data["_demo.collections"][collection]
                .as_array_mut()
                .unwrap();
            if let Some(index) = items.iter().position(|v| v["id"] == item["id"]) {
                items[index] = item;
            } else {
                items.push(item);
            }
            load_collection(s, collection);
            s.selected_item = None;
            s.notice = Some("Item saved".into());
        }
        "collections.delete" => {
            let collection = params["collection"].as_str().unwrap_or("");
            if let Some(items) = s.data["_demo.collections"][collection].as_array_mut() {
                items.retain(|v| v["id"] != params["id"]);
            }
            load_collection(s, collection);
            s.notice = Some("Item removed".into());
        }
        "credentials.set" => {
            if let Some(items) = s.data["settings.schema"]["credentials"].as_array_mut() {
                for item in items {
                    if item["name"] == params["name"] {
                        item["configured"] =
                            json!(!params["value"].as_str().unwrap_or("").is_empty());
                    }
                }
            }
            s.edits.remove(&format!(
                "settings:credential:{}",
                params["name"].as_str().unwrap_or("")
            ));
            s.notice = Some("Provider key updated".into());
        }
        "models.select" | "models.download" | "models.remove" => {
            if let Some(items) = s.data["models.list"]["items"].as_array_mut() {
                for item in items {
                    if method == "models.select" {
                        item["selected"] = json!(item["id"] == params["id"]);
                    } else if item["id"] == params["id"] {
                        item["installed"] = json!(method == "models.download");
                    }
                }
            }
            if method == "models.select" {
                s.snapshot.engine = params["id"].as_str().unwrap_or("").into();
            }
        }
        "models.cancel" => {}
        method if method.starts_with("remote.") => {
            s.data["_demo.last_system_command"] = json!({"method":method,"params":params});
            match method {
                "remote.configure" => {
                    for (key, value) in params.as_object().into_iter().flatten() {
                        s.data["remote.state"]["host"][key] = value.clone();
                        if key == "enabled" {
                            s.data["remote.state"]["host"]["running"] = value.clone();
                        }
                    }
                }
                "remote.pair.open" => {
                    s.data["remote.state"]["host"]["pairing"] = json!(["482916", 120])
                }
                "remote.pair.close" => s.data["remote.state"]["host"]["pairing"] = Value::Null,
                "remote.pair" | "remote.pair.request" => {
                    s.data["remote.state"]["client"] =
                        json!({"paired":true,"name":"Studio desktop","address":params["address"]})
                }
                "remote.forget" | "remote.disconnect" => {
                    s.data["remote.state"]["client"] = json!({"paired":false})
                }
                "remote.scan" => {
                    s.data["remote.state"]["operation"] = json!({"status":"done","result":{"hosts":[{"name":"Studio desktop","address":"192.168.1.42:8767"}]}})
                }
                "remote.pair.answer" => {
                    s.data["remote.state"]["host"]["pair_request"] = Value::Null
                }
                _ => {}
            }
        }
        method if method.starts_with("backup.") => {
            s.data["_demo.last_system_command"] = json!({"method":method,"params":params});
            match method {
                "backup.configure" => {
                    for (key, value) in params.as_object().into_iter().flatten() {
                        s.data["backup.state"][key] = value.clone();
                    }
                }
                "backup.create" => {
                    s.data["backup.state"]["last_backup"] =
                        json!({"created_at":"Just now","path":params["path"]});
                    s.notice = Some("Backup created".into());
                }
                "backup.validate" => {
                    s.data["backup.state"]["inspection"] = json!({"path":params["path"],"valid":true,"info":{"created_at":"November 5, 2026","counts":{"transcripts":42,"meetings":8,"walkthroughs":3}},"error":""})
                }
                "backup.restore" => {
                    s.notice = Some("Preview · verified backup restore requested".into())
                }
                _ => {}
            }
        }
        method if method.starts_with("updates.") => {
            s.data["_demo.last_system_command"] = json!({"method":method,"params":params});
            if method == "updates.download" {
                s.data["updates.state"]["download"]["handoff"] =
                    json!({"path":"/tmp/verified-update"});
            }
            if method == "updates.apply" {
                s.notice = Some("Preview · verified update installation requested".into());
            }
        }
        "setup.complete" | "setup.skip" => {
            s.data["setup.state"]["complete"] = json!(true);
            s.notice = Some("Your workspace is ready".into());
        }
        _ => return false,
    }
    true
}
fn load_collection(s: &mut State, id: &str) {
    let fields = s.data["settings.schema"]["collections"]
        .as_array()
        .and_then(|items| items.iter().find(|v| v["id"] == id))
        .map(|v| v["fields"].clone())
        .unwrap_or(json!([]));
    let items = s.data["_demo.collections"][id]
        .as_array()
        .cloned()
        .unwrap_or_default();
    s.data["collections.list"] = json!({"collection":id,"fields":fields,"items":items});
}
