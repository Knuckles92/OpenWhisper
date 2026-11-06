//! Explicit preview fixtures. Production commands always use the Python service.
use crate::state::State;
use serde_json::{Value, json};

pub fn library_demo(s: &mut State) {
    let history: Vec<Value> = s.snapshot.history.iter().map(|entry| {
        let mut row = serde_json::to_value(entry).unwrap_or_else(|_| json!({}));
        let text = match entry.id.as_str() {
            "demo-0" => "A quieter workspace gives a small idea room to become something useful. Keep the next step simple, make the details intentional, and let the work speak for itself.",
            "demo-1" => "We discussed the launch plan, the remaining design details, and the first group of people who will try the new workspace. The next step is to gather feedback and keep improving the everyday experience.",
            _ => "A few thoughts worth keeping. Capture the idea while it is fresh, then come back when you are ready to give it shape.",
        };
        row["text"] = json!(text); row["raw"] = json!(text);
        row["editable"] = json!(true); row["audio_available"] = json!(false);
        row
    }).collect();
    s.data["demo.history"] = json!(history);
    s.data["demo.meetings"] = json!([
        {"id":"meeting-demo-1","title":"A calmer launch, one detail at a time","started_at":"Today · 10:30 AM","duration":1482,"status":"ended","preview":"Decisions, next steps, and a little room to think.",
         "transcript":"Maya · 00:00\nLet’s make the first experience feel welcoming.\n\nAlex · 00:12\nWe can simplify the setup and keep the important controls close by.\n\nMaya · 00:25\nI’ll share the updated flow with the team this afternoon.",
         "summary":"The team agreed to simplify first-time setup and test the updated flow this week.",
         "report":"Launch review\n\nDecisions\n• Keep first-time setup focused on the essentials.\n• Make capture status visible throughout the workspace.\n\nNext steps\n• Maya: share the revised flow this afternoon.\n• Alex: review keyboard navigation before Friday.","finalization":{"status":"complete"}},
        {"id":"meeting-demo-2","title":"Friday notes","started_at":"Yesterday · 3:15 PM","duration":960,"status":"ended","preview":"Making space for the next good idea.","transcript":"A brief check-in about the week ahead.","summary":"A weekly check-in.","report":"No follow-up actions were recorded.","finalization":{"status":"complete"}}
    ]);
    s.data["demo.walkthroughs"] = json!([
        {"id":"walkthrough-demo-1","title":"A welcoming first recording","started_at":"Today · 11:45 AM","step_count":3,"preview":"From a quick thought to a transcript worth keeping.","status":"ready","markdown":"A welcoming first recording\n\n1. Choose your microphone.\n2. Capture a thought with Quick record.\n3. Return to History to copy, edit, or export your words.",
         "steps":[{"index":0,"title":"Choose your microphone","text":"Open audio settings and choose the microphone you want to use.","included":true},{"index":1,"title":"Capture a thought","text":"Press Record, speak naturally, and finish when your idea is captured.","included":true},{"index":2,"title":"Make the words your own","text":"Open History to review, edit, and share the transcript.","included":true}],"exports":["md","pdf","docx"]},
        {"id":"walkthrough-demo-2","title":"Sharing a better handoff","started_at":"Yesterday · 4:20 PM","step_count":1,"preview":"Explain the details once. Keep the context together.","status":"ready","markdown":"Sharing a better handoff\n\nWalk through the task and explain what matters.","steps":[{"index":0,"title":"Keep the context","text":"Walk through the task and explain what matters.","included":true}],"exports":["md"]}
    ]);
    for (i, item) in s.data["demo.walkthroughs"]
        .as_array_mut()
        .unwrap()
        .iter_mut()
        .enumerate()
    {
        item["documents"] = json!({"spoken":item["markdown"],"latest":"# A welcoming first recording\n\nA simple guide to capturing and keeping an idea.\n\n## Begin with your microphone\nChoose a clear audio source, record naturally, and return to History to make the words your own.","1":"# First write-up\n\nChoose a microphone and capture a thought."});
        item["written_up"] = json!(i == 0);
        item["can_retry"] = json!(true);
        item["can_recover"] = json!(false);
        item["versions"] = if i == 0 {
            json!([{"label":"Latest write-up","spoken":false,"version":null},{"label":"As spoken","spoken":true,"version":null},{"label":"Version 1","spoken":false,"version":1}])
        } else {
            json!([{"label":"As spoken","spoken":true,"version":null}])
        };
        item["steps"][0]["notes"] =
            json!([{"text":"Keep the instructions welcoming and concise."}]);
        item["steps"][0]["kept_at_s"] = json!(12.);
    }
    s.data["walkthroughs.styles"] = json!({"items":[{"id":"default","name":"Default","instructions":"Write a clear walkthrough with screenshots beside the steps they explain."},{"id":"handoff","name":"Team handoff","instructions":"Write a concise team handoff with the goal, decisions, and next steps."}]});
    s.data["walkthroughs.retention"] = json!({"days":0});
    for (scope, source) in [
        ("meetings", "demo.meetings"),
        ("walkthroughs", "demo.walkthroughs"),
    ] {
        s.data[format!("{scope}.list")] =
            json!({"items":s.data[source],"offset":0,"limit":50,"has_more":false});
    }
}

pub fn library_demo_command(s: &mut State, method: &str, params: &Value) -> bool {
    match method {
        "history.list" => {
            let query = params["query"].as_str().unwrap_or("").trim().to_lowercase();
            let kind = params["kind"].as_str().unwrap_or("all");
            let rows: Vec<Value> = s.data["demo.history"]
                .as_array()
                .into_iter()
                .flatten()
                .filter(|row| {
                    (kind == "all" || row["kind"] == kind)
                        && format!("{} {}", row["title"], row["text"])
                            .to_lowercase()
                            .contains(&query)
                })
                .cloned()
                .collect();
            let offset = params["offset"].as_u64().unwrap_or(0) as usize;
            let limit = params["limit"].as_u64().unwrap_or(50) as usize;
            s.data[method] = json!({"items":rows.iter().skip(offset).take(limit).cloned().collect::<Vec<_>>(),"total":rows.len(),"offset":offset,"limit":limit,"has_more":offset+limit<rows.len(),"query":query});
        }
        "history.detail" => {
            if let Some(item) = find(s, "demo.history", &params["id"]) {
                s.data[method] = item.clone();
                s.detail = item["text"].as_str().unwrap_or("").into();
                s.detail_raw = item["raw"].as_str().unwrap_or("").into();
                s.detail_title = item["title"].as_str().unwrap_or("Transcript").into();
            }
        }
        "history.update" => {
            if let Some(rows) = s.data["demo.history"].as_array_mut() {
                if let Some(row) = rows.iter_mut().find(|row| row["id"] == params["id"]) {
                    for key in ["text", "title"] {
                        if params[key].is_string() {
                            row[key] = params[key].clone();
                        }
                    }
                    row["preview"] = row["text"].clone();
                }
            }
            library_demo_command(
                s,
                "history.list",
                &json!({"query":s.search.text,"kind":s.filter,"offset":s.library_page,"limit":50}),
            );
            library_demo_command(s, "history.detail", &json!({"id":params["id"]}));
            s.selected_tab.clear();
        }
        "meetings.detail" => {
            if let Some(item) = find(s, "demo.meetings", &params["id"]) {
                s.data[method] = item;
            }
        }
        "walkthroughs.detail" => {
            if let Some(item) = walkthrough_detail(s, params) {
                s.data[method] = item.clone();
                crate::pages::walkthrough_response(s, method, &item);
            }
        }
        "meetings.list" | "walkthroughs.list" => {
            let source = if method.starts_with("meetings") {
                "demo.meetings"
            } else {
                "demo.walkthroughs"
            };
            s.data[method] = json!({"items":s.data[source],"offset":0,"limit":50,"has_more":false});
        }
        "history.delete" | "meetings.delete" | "walkthroughs.delete" => {
            let scope = method.split('.').next().unwrap();
            let source = format!("demo.{scope}");
            if let Some(rows) = s.data[&source].as_array_mut() {
                rows.retain(|row| row["id"] != params["id"]);
            }
            library_demo_command(
                s,
                &format!("{scope}.list"),
                &json!({"query":s.search.text,"kind":s.filter,"offset":s.library_page,"limit":50}),
            );
            s.confirmation = None;
            if scope == "history" {
                s.selected = None;
            } else {
                s.selected_item = None;
            }
        }
        "meetings.update" => {
            if let Some(rows) = s.data["demo.meetings"].as_array_mut() {
                if let Some(row) = rows.iter_mut().find(|row| row["id"] == params["id"]) {
                    row["title"] = params["title"].clone();
                    s.data["meetings.detail"] = row.clone();
                }
            }
            library_demo_command(s, "meetings.list", &json!({}));
            s.selected_tab.clear();
        }
        "walkthroughs.review" => {
            s.data["demo.walkthrough-review-request"] = params.clone();
            if let Some(mut item) = find(s, "demo.walkthroughs", &params["id"]) {
                let original = item["steps"].as_array().cloned().unwrap_or_default();
                let order = params["order"].as_array().cloned().unwrap_or_default();
                item["steps"] = json!(
                    order
                        .iter()
                        .filter_map(|i| i.as_u64())
                        .enumerate()
                        .map(|(position, i)| {
                            let mut step = original[i as usize].clone();
                            step["text"] = params["texts"][position].clone();
                            step["included"] = params["keep"][position].clone();
                            step
                        })
                        .collect::<Vec<_>>()
                );
                s.data["walkthroughs.detail"] = item.clone();
                if let Some(rows) = s.data["demo.walkthroughs"].as_array_mut() {
                    if let Some(row) = rows.iter_mut().find(|row| row["id"] == params["id"]) {
                        *row = item;
                    }
                }
                s.data["walkthroughs.detail"] =
                    walkthrough_detail(s, &json!({"id":params["id"],"spoken":true})).unwrap();
                crate::pages::walkthrough_event(
                    s,
                    "walkthroughs.operation",
                    &json!({"id":params["id"],"kind":"review","status":"done","item":s.data["walkthroughs.detail"]}),
                );
            }
        }
        "walkthroughs.styles" => {}
        "walkthroughs.export" => {
            s.data[method] = params.clone();
        }
        "walkthroughs.copy" | "walkthroughs.screenshot.copy" => {
            s.data[method] = params.clone();
            s.notice = Some("Copied to clipboard".into());
        }
        "walkthroughs.retention" => {
            s.data["demo.walkthrough-retention-request"] = params.clone();
            if params["days"].is_number() {
                s.data[method] = json!({"days":params["days"],"pruned":0});
                s.confirmation = None;
            }
        }
        "walkthroughs.write_up.save" => {
            s.data["demo.walkthrough-save-request"] = params.clone();
            let key = params["version"]
                .as_u64()
                .map(|n| n.to_string())
                .unwrap_or_else(|| "latest".into());
            if let Some(rows) = s.data["demo.walkthroughs"].as_array_mut() {
                if let Some(item) = rows.iter_mut().find(|i| i["id"] == params["id"]) {
                    item["documents"][key] = params["markdown"].clone();
                }
            }
            if let Some(item) = walkthrough_detail(s, params) {
                crate::pages::walkthrough_event(
                    s,
                    "walkthroughs.operation",
                    &json!({"id":params["id"],"kind":"write_up.save","status":"done","item":item}),
                );
            }
        }
        "walkthroughs.write_up.retry" => {
            s.data["demo.walkthrough-retry-request"] = params.clone();
            if let Some(rows) = s.data["demo.walkthroughs"].as_array_mut() {
                if let Some(item) = rows.iter_mut().find(|i| i["id"] == params["id"]) {
                    item["documents"]["latest"] = json!(
                        "# A fresh write-up\n\nA clear handoff based on the spoken walkthrough."
                    );
                    item["written_up"] = json!(true);
                }
            }
            if let Some(item) = walkthrough_detail(s, &json!({"id":params["id"],"spoken":false})) {
                crate::pages::walkthrough_event(
                    s,
                    "walkthroughs.operation",
                    &json!({"id":params["id"],"kind":"write_up.retry","status":"done","item":item}),
                );
            }
        }
        "walkthroughs.write_up.cancel" => {
            s.data["walkthrough.operations"][params["id"].as_str().unwrap_or("")]["status"] =
                json!("canceled");
        }
        "walkthroughs.recover" => {
            s.data["demo.walkthrough-recover-request"] = params.clone();
            if let Some(rows) = s.data["demo.walkthroughs"].as_array_mut() {
                if let Some(item) = rows.iter_mut().find(|i| i["id"] == params["id"]) {
                    item["status"] = json!("ready");
                    item["can_recover"] = json!(false);
                }
            }
            library_demo_command(s, "walkthroughs.detail", params);
        }
        "meeting.start" => {
            s.snapshot.meeting = true;
            s.data["snapshot"]["meeting_live"] = json!({"active":true,"paused":false,"transcript":"Your live conversation will appear here.","status":"Listening"});
        }
        "meeting.stop" => {
            s.snapshot.meeting = false;
        }
        "meeting.pause" | "meeting.resume" => {
            s.data["snapshot"]["meeting_live"]["paused"] = json!(method == "meeting.pause");
        }
        "walkthrough.pause" => {
            s.data["snapshot"]["walkthrough_live"]["paused"] = params["paused"].clone();
        }
        "walkthrough.toggle" => {
            s.snapshot.recording = !s.snapshot.recording;
            s.snapshot.mode = "walkthrough".into();
            s.data["snapshot"]["walkthrough_live"]["active"] = json!(s.snapshot.recording);
        }
        "walkthrough.cancel" => {
            s.data["demo.walkthrough-cancel-request"] = params.clone();
            s.data["snapshot"]["walkthrough_live"]["writing_up"] = json!(false);
        }
        "file.enqueue" => {
            s.data["file.queue"] = json!({"items":params["paths"].as_array().into_iter().flatten().enumerate().map(|(i,path)| json!({"id":format!("preview-{i}"),"path":path,"name":path.as_str().and_then(|p|std::path::Path::new(p).file_name()).map(|p|p.to_string_lossy().into_owned()).unwrap_or_default(),"status":"queued"})).collect::<Vec<_>>(),"active":false});
            s.file_queue.clear();
            s.file = None;
        }
        "file.queue" => {}
        "file.remove" => {
            if let Some(items) = s.data["file.queue"]["items"].as_array_mut() {
                items.retain(|item| item["id"] != params["id"]);
            }
        }
        "file.clear" => {
            s.data["file.queue"] = json!({"items":[],"active":false});
        }
        "file.cancel" => {
            s.snapshot.busy = false;
        }
        "screenshot.complete" => {
            s.data["screenshot.submitted"] = params.clone();
            crate::pages::screenshot_finished(s);
        }
        _ => return false,
    }
    true
}

fn find(s: &State, source: &str, id: &Value) -> Option<Value> {
    s.data[source]
        .as_array()?
        .iter()
        .find(|row| row["id"] == *id)
        .cloned()
}

fn walkthrough_detail(s: &State, params: &Value) -> Option<Value> {
    let mut item = find(s, "demo.walkthroughs", &params["id"])?;
    let spoken = params["spoken"]
        .as_bool()
        .unwrap_or(item["written_up"] != true);
    let version = if spoken {
        Value::Null
    } else {
        params["version"].clone()
    };
    let key = if spoken {
        "spoken".into()
    } else {
        version
            .as_u64()
            .map(|n| n.to_string())
            .unwrap_or_else(|| "latest".into())
    };
    if let Some(markdown) = item["documents"][&key].as_str() {
        item["markdown"] = json!(markdown);
    }
    item["spoken"] = json!(spoken);
    item["version"] = version;
    item["styles"] = s.data["walkthroughs.styles"]["items"].clone();
    Some(item)
}
