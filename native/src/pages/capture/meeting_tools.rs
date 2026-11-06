//! Direct, typed editing of the same durable meeting document used by the
//! companion dashboard. Human corrections retain the service's protection rules.
use crate::state::State;
use crate::ui::{Palette, btn, card, divider, label, pill};
use rune::{El, FileDialogOptions, TextEdit, col, el, row};
use rune_kit::{Markdown, TextArea, TextInput};
use serde_json::{Value, json};

#[cfg(test)]
mod tests;

const CARDS: [(&str, &str); 7] = [
    ("user_notes", "Your notes"),
    ("key_points", "Key points"),
    ("decisions", "Decisions"),
    ("action_items", "Action items"),
    ("risks", "Risks"),
    ("timeline", "Timeline"),
    ("live_notes", "AI notes"),
];
fn string<'a>(v: &'a Value, key: &str) -> &'a str {
    v[key].as_str().unwrap_or("")
}
fn values(v: &Value) -> Vec<&Value> {
    if let Some(a) = v.as_array() {
        a.iter().collect()
    } else if let Some(o) = v.as_object() {
        o.values().collect()
    } else {
        vec![]
    }
}
fn draft(s: &State, key: &str, initial: &str) -> String {
    s.edits
        .get(key)
        .map(|v| v.text.clone())
        .unwrap_or_else(|| initial.into())
}
fn edit_mut<'a>(s: &'a mut State, key: &str, initial: &str) -> &'a mut TextEdit {
    s.edits
        .entry(key.into())
        .or_insert_with(|| TextEdit::new(initial))
}
fn editor(
    s: &State,
    p: &Palette,
    key: &str,
    title: &str,
    initial: &str,
    placeholder: &str,
    multiline: bool,
) -> El {
    let value = s
        .edits
        .get(key)
        .cloned()
        .unwrap_or_else(|| TextEdit::new(initial));
    let bind_key = key.to_owned();
    let initial = initial.to_owned();
    let input = if multiline {
        TextArea::new(&value)
            .width(None)
            .label(title)
            .placeholder(placeholder)
            .auto_grow(3, 9)
            .wrap_anywhere()
            .disabled(!s.available() || s.pending)
            .on_key(|s: &mut State, e, tx| crate::ui::suppress_hotkey(s, e, tx))
            .on_edit(move |s: &mut State| edit_mut(s, &bind_key, &initial))
            .build(&p.kit)
    } else {
        TextInput::new(&value)
            .width(None)
            .height(35.)
            .label(title)
            .placeholder(placeholder)
            .disabled(!s.available() || s.pending)
            .on_key(|s: &mut State, e, tx| crate::ui::suppress_hotkey(s, e, tx))
            .on_edit(move |s: &mut State| edit_mut(s, &bind_key, &initial))
            .build(&p.kit)
    };
    col()
        .key(format!("meeting-field-{key}"))
        .min_w(0.)
        .gap(6.)
        .child(label(title, 11.5, 550., p.ink))
        .child(input)
}
fn enabled(s: &State) -> bool {
    s.available() && !s.pending
}
fn patch(s: &mut State, id: &str, ops: Value) {
    s.command("meetings.patch", json!({"id":id,"ops":ops}));
}
/// Resolve transient editors only after the service accepts their domain edit.
pub fn meeting_tools_response(s: &mut State, method: &str, result: &Value) {
    let Some(id) = result["id"].as_str() else {
        return;
    };
    if method == "meetings.report.request" && result["ok"].as_bool() == Some(true) {
        s.edits.remove(&format!("meeting:{id}:report-request"));
    }
    if !matches!(method, "meetings.patch" | "meetings.review") {
        return;
    }
    for outcome in values(&result["results"]) {
        if outcome["ok"].as_bool() != Some(true) {
            continue;
        }
        let op = &outcome["op"];
        let (key, text) = match string(op, "op") {
            "add_item" => (
                format!("meeting:{id}:new:{}", string(op, "card")),
                Some(string(op, "text")),
            ),
            "ask_question" => (
                format!("meeting:{id}:new-question"),
                Some(string(op, "text")),
            ),
            "upsert_participant" if op["id"].is_null() => (
                format!("meeting:{id}:new-person"),
                Some(string(op, "display_name")),
            ),
            "answer_question" => (
                format!("meeting:{id}:answer:{}", string(op, "question_id")),
                Some(string(op, "answer_text")),
            ),
            "update_item" => {
                let key = format!("meeting:{id}:editing");
                if draft(s, &key, "") == string(op, "id") {
                    select(s, &key, "");
                }
                continue;
            }
            _ => continue,
        };
        if s.edits
            .get(&key)
            .is_some_and(|draft| Some(draft.text.as_str()) == text)
        {
            s.edits.remove(&key);
        }
    }
}
fn select(s: &mut State, key: &str, value: &str) {
    s.edits.insert(key.into(), TextEdit::new(value));
    s.confirmation = None;
}
fn confirm_remove(
    s: &State,
    p: &Palette,
    kind: &str,
    meeting: &str,
    id: &str,
    method: &'static str,
    params: Value,
) -> El {
    if !s
        .confirmation
        .as_ref()
        .is_some_and(|v| v["kind"] == kind && v["meeting"] == meeting && v["id"] == id)
    {
        return el();
    }
    col()
        .gap(9.)
        .child(label("Remove this from the meeting?", 12., 400., p.danger))
        .child(
            row()
                .gap(8.)
                .flex_wrap()
                .child(
                    btn(
                        format!("confirm-{kind}-{id}"),
                        "Remove",
                        p,
                        true,
                        enabled(s),
                        move |s, _| {
                            s.command(method, params.clone());
                            s.confirmation = None;
                        },
                    )
                    .h(31.),
                )
                .child(
                    btn(
                        format!("cancel-{kind}-{id}"),
                        "Keep it",
                        p,
                        false,
                        true,
                        |s, _| s.confirmation = None,
                    )
                    .h(31.),
                ),
        )
}

pub fn tools(s: &State, p: &Palette) -> El {
    let detail = if let Some(id) = s.selected_item.as_deref() {
        if s.data["meetings.detail"]["id"].as_str() == Some(id) {
            &s.data["meetings.detail"]
        } else {
            return el();
        }
    } else if s.snapshot.meeting && s.data["meetings.current"]["id"].is_string() {
        &s.data["meetings.current"]
    } else if s.snapshot.meeting {
        return card(p)
            .key("meeting-tools-load")
            .child(label("Your meeting workspace", 16., 550., p.ink))
            .child(label(
                "Keep notes, clarify speakers, and guide the conversation from here.",
                12.,
                400.,
                p.muted,
            ))
            .child(
                btn(
                    "meeting-load-current",
                    "Open live meeting tools",
                    p,
                    true,
                    enabled(s),
                    |s, _| s.command("meetings.current", json!({})),
                )
                .h(34.),
            );
    } else {
        return el();
    };
    let id = string(detail, "id");
    if id.is_empty() {
        return el();
    }
    let tab_key = format!("meeting:{id}:tools-tab");
    let tab = draft(s, &tab_key, "notes");
    let refresh_id = id.to_owned();
    let live = s.selected_item.is_none();
    let mut tabs = row().gap(6.).flex_wrap();
    for (key, title) in [
        ("brief", "Brief"),
        ("notes", "Notes & insights"),
        ("people", "People"),
        ("questions", "Questions"),
        ("review", "Review"),
        ("reports", "Tailored reports"),
    ] {
        let select_key = tab_key.clone();
        tabs = tabs.child(
            btn(
                format!("meeting-tools-tab-{key}"),
                title,
                p,
                tab == key,
                true,
                move |s, _| select(s, &select_key, key),
            )
            .h(31.),
        );
    }
    let body = match tab.as_str() {
        "brief" => brief(s, p, id, detail),
        "people" => people(s, p, id, detail),
        "questions" => questions(s, p, id, detail),
        "review" => review(s, p, id, detail),
        "reports" => reports(s, p, id, detail),
        _ => notes(s, p, id, detail),
    };
    col()
        .key("meeting-native-tools")
        .min_w(0.)
        .gap(16.)
        .child(
            row()
                .gap(10.)
                .center_y()
                .flex_wrap()
                .child(
                    label("Make this meeting useful", 18., 550., p.ink)
                        .grow(1.)
                        .min_w(0.),
                )
                .child(
                    btn(
                        "meeting-refresh-tools",
                        "Refresh",
                        p,
                        false,
                        enabled(s),
                        move |s, _| {
                            s.command(
                                if live {
                                    "meetings.current"
                                } else {
                                    "meetings.detail"
                                },
                                if live {
                                    json!({})
                                } else {
                                    json!({"id":refresh_id})
                                },
                            )
                        },
                    )
                    .h(31.),
                ),
        )
        .child(tabs)
        .child(body)
}
fn brief(s: &State, p: &Palette, id: &str, detail: &Value) -> El {
    let state = &detail["state"];
    let topic = state["topic"]["current"].as_str().unwrap_or("");
    let intent = state["intent"]["text"].as_str().unwrap_or("");
    let summary = string(detail, "summary");
    let topic_key = format!("meeting:{id}:topic");
    let intent_key = format!("meeting:{id}:intent");
    let summary_key = format!("meeting:{id}:summary");
    let topic_text = draft(s, &topic_key, topic);
    let intent_text = draft(s, &intent_key, intent);
    let summary_text = draft(s, &summary_key, summary);
    let changed = topic_text != topic || intent_text != intent || summary_text != summary;
    let valid = topic_text.chars().count() <= 500
        && draft(s, &intent_key, intent).chars().count() <= 2000
        && draft(s, &summary_key, summary).chars().count() <= 8000
        && (topic_text == topic || !topic_text.trim().is_empty())
        && (summary_text == summary || !summary_text.trim().is_empty());
    let meeting = id.to_owned();
    let initial_topic = topic.to_owned();
    let initial_intent = intent.to_owned();
    let initial_summary = summary.to_owned();
    card(p)
        .min_w(0.)
        .child(label(
            "Give the meeting a little direction",
            16.,
            550.,
            p.ink,
        ))
        .child(editor(
            s,
            p,
            &topic_key,
            "Current topic",
            topic,
            "A short title for the discussion",
            false,
        ))
        .child(editor(
            s,
            p,
            &intent_key,
            "What should the copilot pay attention to?",
            intent,
            "Decisions, commitments, open questions…",
            true,
        ))
        .child(label(
            "Your brief guides future AI passes. You can clear it at any time.",
            11.,
            400.,
            p.muted,
        ))
        .child(editor(
            s,
            p,
            &summary_key,
            "Working summary",
            summary,
            "A concise account of the conversation so far",
            true,
        ))
        .child(label(
            "Topic: up to 500 characters. Brief: 2,000. Summary: 8,000.",
            10.5,
            400.,
            p.muted,
        ))
        .child(
            btn(
                "meeting-save-brief",
                "Save brief & summary",
                p,
                true,
                enabled(s) && valid && changed,
                move |s, _| {
                    let mut ops = vec![];
                    let topic = draft(s, &topic_key, &initial_topic);
                    if topic != initial_topic {
                        ops.push(json!({"op":"set_topic","text":topic}));
                    }
                    let intent = draft(s, &intent_key, &initial_intent);
                    if intent != initial_intent {
                        ops.push(json!({"op":"set_meeting_intent","text":intent}));
                    }
                    let summary = draft(s, &summary_key, &initial_summary);
                    if summary != initial_summary {
                        ops.push(json!({"op":"set_rolling_summary","text":summary}));
                    }
                    if !ops.is_empty() {
                        patch(s, &meeting, Value::Array(ops));
                    }
                },
            )
            .h(33.),
        )
}
fn notes(s: &State, p: &Palette, id: &str, detail: &Value) -> El {
    let card_key = format!("meeting:{id}:card");
    let selected = draft(s, &card_key, "user_notes");
    let mut tabs = row().gap(6.).flex_wrap();
    for (key, title) in CARDS {
        let select_key = card_key.clone();
        tabs = tabs.child(
            btn(
                format!("meeting-card-tab-{key}"),
                title,
                p,
                key == selected,
                true,
                move |s, _| select(s, &select_key, key),
            )
            .h(29.),
        );
    }
    let add_key = format!("meeting:{id}:new:{selected}");
    let text = draft(s, &add_key, "");
    let meeting = id.to_owned();
    let card_id = selected.clone();
    let save_key = add_key.clone();
    let mut content = col().gap(15.).min_w(0.).child(tabs).child(
        card(p)
            .min_w(0.)
            .child(editor(
                s,
                p,
                &add_key,
                "Add to this section",
                "",
                "Capture a detail worth keeping…",
                true,
            ))
            .child(label(
                format!("{} / 4,000 characters", text.chars().count()),
                10.5,
                400.,
                if text.chars().count() > 4000 {
                    p.danger
                } else {
                    p.muted
                },
            ))
            .child(
                btn(
                    "meeting-add-note",
                    "Add note",
                    p,
                    true,
                    enabled(s) && !text.trim().is_empty() && text.chars().count() <= 4000,
                    move |s, _| {
                        patch(
                            s,
                            &meeting,
                            json!([{"op":"add_item","card":card_id,"text":draft(s,&save_key,"")}]),
                        )
                    },
                )
                .h(32.),
            ),
    );
    let items = values(&detail["cards"][&selected]);
    let mut shown = 0;
    for item in items
        .into_iter()
        .filter(|item| string(item, "status") != "removed")
    {
        content = content.child(note(s, p, id, item, detail));
        shown += 1;
    }
    if shown == 0 {
        content = content.child(card(p).child(label(
            "There’s room for the next good detail.",
            13.,
            400.,
            p.muted,
        )));
    }
    content
}
fn note(s: &State, p: &Palette, meeting: &str, item: &Value, detail: &Value) -> El {
    let id = string(item, "id");
    let text = string(item, "text");
    let edit_key = format!("meeting:{meeting}:item:{id}");
    let editing_key = format!("meeting:{meeting}:editing");
    let editing = draft(s, &editing_key, "") == id;
    let edit_select = editing_key.clone();
    let edit_id = id.to_owned();
    let initial = text.to_owned();
    let seed_key = edit_key.clone();
    let mut block = card(p)
        .key(format!("meeting-item-{id}"))
        .min_w(0.)
        .gap(11.)
        .child(label(text, 13., 400., p.ink).selectable(true));
    let mut badges = row().gap(6.).flex_wrap();
    if item["pinned"].as_bool() == Some(true) {
        badges = badges.child(pill("Pinned", p));
    }
    if matches!(string(item, "status"), "confirmed" | "edited") {
        badges = badges.child(pill(
            if string(item, "status") == "confirmed" {
                "Confirmed"
            } else {
                "Edited by you"
            },
            p,
        ));
    }
    block = block.child(badges);
    let pin_meeting = meeting.to_owned();
    let pin_id = id.to_owned();
    let pinned = item["pinned"].as_bool().unwrap_or(false);
    let confirm_meeting = meeting.to_owned();
    let confirm_id = id.to_owned();
    let remove_meeting = meeting.to_owned();
    let remove_id = id.to_owned();
    block=block.child(row().gap(7.).flex_wrap()
        .child(btn(format!("edit-meeting-item-{id}"),"Edit",p,editing,enabled(s),move |s,_| {select(s,&edit_select,&edit_id);s.edits.insert(seed_key.clone(),TextEdit::new(initial.clone()));}).h(29.))
        .child(btn(format!("pin-meeting-item-{id}"),if pinned{"Unpin"}else{"Pin"},p,false,enabled(s),move |s,_|patch(s,&pin_meeting,json!([{"op":if pinned{"unpin_item"}else{"pin_item"},"id":pin_id}]))).h(29.))
        .child(btn(format!("confirm-meeting-item-{id}"),"Confirm",p,false,enabled(s)&&string(item,"status")!="confirmed",move |s,_|patch(s,&confirm_meeting,json!([{"op":"confirm_item","id":confirm_id}]))).h(29.))
        .child(btn(format!("remove-meeting-item-{id}"),"Remove",p,false,enabled(s),move |s,_|s.confirmation=Some(json!({"kind":"meeting.item.remove","meeting":remove_meeting,"id":remove_id}))).h(29.)));
    if editing {
        let current = draft(s, &edit_key, text);
        let meeting = meeting.to_owned();
        let item_id = id.to_owned();
        let save_key = edit_key.clone();
        let original = text.to_owned();
        block=block.child(editor(s,p,&edit_key,"Your correction",text,"Keep the meaning clear",true))
            .child(row().gap(8.).flex_wrap()
                .child(btn(format!("save-meeting-item-{id}"),"Save correction",p,true,enabled(s)&&!current.trim().is_empty()&&current.chars().count()<=4000,move |s,_|{patch(s,&meeting,json!([{"op":"update_item","id":item_id,"set":{"text":draft(s,&save_key,&original)}}]));}).h(31.))
                .child(btn(format!("cancel-meeting-item-{id}"),"Cancel",p,false,true,move |s,_|select(s,&editing_key,"")).h(31.)));
    }
    let evidence = values(&item["evidence"]);
    if !evidence.is_empty() {
        block = block.child(label("Supporting transcript", 10.5, 550., p.muted));
        for reference in evidence {
            if let Some(segment) = values(&detail["segments"])
                .into_iter()
                .find(|segment| segment["id"] == *reference)
            {
                block = block.child(
                    label(format!("“{}”", string(segment, "text")), 11., 400., p.muted)
                        .selectable(true),
                );
            }
        }
    }
    block.child(confirm_remove(
        s,
        p,
        "meeting.item.remove",
        meeting,
        id,
        "meetings.patch",
        json!({"id":meeting,"ops":[{"op":"remove_item","id":id}]}),
    ))
}

fn people(s: &State, p: &Palette, id: &str, detail: &Value) -> El {
    let participants = values(&detail["participants"]);
    let mut content=col().gap(15.).min_w(0.)
        .child(card(p).child(label("Put a name to the voice",16.,550.,p.ink))
            .child(label("Names you confirm stay attached to the speaker. Assign a transcript passage below when a speaker was misidentified.",12.,400.,p.muted)));
    for person in &participants {
        let person_id = string(person, "id");
        let name = string(person, "display_name");
        let key = format!("meeting:{id}:person:{person_id}");
        let text = draft(s, &key, name);
        let meeting = id.to_owned();
        let save_person = person_id.to_owned();
        let save_key = key.clone();
        let initial = name.to_owned();
        content=content.child(card(p).key(format!("meeting-person-{person_id}")).min_w(0.)
            .child(editor(s,p,&key,"Speaker name",name,"How should this speaker appear?",false))
            .child(label(if person["is_provisional"].as_bool()==Some(true){"Suggested speaker · confirm the name when you’re sure."}else{"Confirmed speaker"},10.5,400.,p.muted))
            .child(btn(format!("rename-meeting-person-{person_id}"),"Save name",p,true,enabled(s)&&!text.trim().is_empty()&&text.chars().count()<=120,move |s,_|patch(s,&meeting,json!([{"op":"rename_participant","participant_id":save_person,"display_name":draft(s,&save_key,&initial)}]))).h(31.)));
    }
    let new_key = format!("meeting:{id}:new-person");
    let new_name = draft(s, &new_key, "");
    let meeting = id.to_owned();
    let save_key = new_key.clone();
    content=content.child(card(p).min_w(0.).child(editor(s,p,&new_key,"Add a speaker","","Speaker name",false))
        .child(btn("meeting-add-person","Add speaker",p,false,enabled(s)&&!new_name.trim().is_empty()&&new_name.chars().count()<=120,move |s,_|patch(s,&meeting,json!([{"op":"upsert_participant","display_name":draft(s,&save_key,""),"kind":"others_cluster"}]))).h(31.)));
    if !participants.is_empty() {
        let target_key = format!("meeting:{id}:speaker-target");
        let target = draft(s, &target_key, "");
        let mut choose = row().gap(6.).flex_wrap();
        for person in &participants {
            let pid = string(person, "id").to_owned();
            let select_key = target_key.clone();
            choose = choose.child(
                btn(
                    format!("meeting-select-speaker-{pid}"),
                    string(person, "display_name"),
                    p,
                    pid == target,
                    true,
                    move |s, _| select(s, &select_key, &pid),
                )
                .h(29.),
            );
        }
        let mut assignment = card(p)
            .min_w(0.)
            .child(label("Correct a speaker attribution", 15., 550., p.ink))
            .child(label(
                "Choose a speaker, then the passage that belongs to them.",
                11.,
                400.,
                p.muted,
            ))
            .child(choose);
        if !target.is_empty() {
            let segments = values(&detail["segments"]);
            let page_key = format!("meeting:{id}:segments-page");
            let offset = draft(s, &page_key, "0").parse::<usize>().unwrap_or(0);
            for segment in segments.iter().skip(offset).take(12) {
                let segment_id = string(segment, "id").to_owned();
                let meeting = id.to_owned();
                let target = target.clone();
                assignment=assignment.child(divider(p)).child(label(string(segment,"text"),12.,400.,p.ink).selectable(true))
                    .child(btn(format!("assign-meeting-segment-{segment_id}"),"Assign to selected speaker",p,false,enabled(s),move |s,_|patch(s,&meeting,json!([{"op":"reassign_segment_speaker","segment_id":segment_id,"participant_id":target}]))).h(29.));
            }
            let back_key = page_key.clone();
            let next_key = page_key.clone();
            assignment = assignment.child(
                row()
                    .gap(8.)
                    .flex_wrap()
                    .child(
                        btn(
                            "meeting-segments-previous",
                            "Previous passages",
                            p,
                            false,
                            offset > 0,
                            move |s, _| {
                                select(s, &back_key, &offset.saturating_sub(12).to_string())
                            },
                        )
                        .h(29.),
                    )
                    .child(
                        btn(
                            "meeting-segments-next",
                            "More passages",
                            p,
                            false,
                            offset + 12 < segments.len(),
                            move |s, _| select(s, &next_key, &(offset + 12).to_string()),
                        )
                        .h(29.),
                    ),
            );
        }
        content = content.child(assignment);
    }
    content
}
fn questions(s: &State, p: &Palette, id: &str, detail: &Value) -> El {
    let ask_key = format!("meeting:{id}:new-question");
    let question = draft(s, &ask_key, "");
    let meeting = id.to_owned();
    let new_key = ask_key.clone();
    let mut content = col().gap(15.).min_w(0.).child(
        card(p)
            .min_w(0.)
            .child(label("A quiet place for open questions", 16., 550., p.ink))
            .child(editor(
                s,
                p,
                &ask_key,
                "What needs an answer?",
                "",
                "Capture a question without interrupting the conversation",
                true,
            ))
            .child(
                btn(
                    "meeting-ask-question",
                    "Add question",
                    p,
                    true,
                    enabled(s) && !question.trim().is_empty() && question.chars().count() <= 4000,
                    move |s, _| {
                        patch(
                            s,
                            &meeting,
                            json!([{"op":"ask_question","text":draft(s,&new_key,"")}]),
                        )
                    },
                )
                .h(32.),
            ),
    );
    for question in values(&detail["questions"]) {
        let qid = string(question, "id");
        let status = string(question, "status");
        let mut group = card(p)
            .key(format!("meeting-question-{qid}"))
            .min_w(0.)
            .child(
                row()
                    .gap(8.)
                    .flex_wrap()
                    .child(
                        label(string(question, "text"), 14., 550., p.ink)
                            .grow(1.)
                            .min_w(0.),
                    )
                    .child(pill(
                        if status == "resolved" {
                            "Answered"
                        } else if status == "dismissed" {
                            "Dismissed"
                        } else {
                            "Open"
                        },
                        p,
                    )),
            );
        if !string(question, "answer").is_empty() {
            group =
                group.child(label(string(question, "answer"), 12., 400., p.ink).selectable(true));
        }
        let answer_key = format!("meeting:{id}:answer:{qid}");
        let answer = draft(s, &answer_key, "");
        if status == "open" {
            group = group.child(editor(
                s,
                p,
                &answer_key,
                "Your answer",
                "",
                "Add the context this question needs",
                true,
            ));
            if !string(question, "suggested_answer").is_empty() {
                let suggestion = string(question, "suggested_answer").to_owned();
                let suggestion_key = answer_key.clone();
                group = group
                    .child(label(
                        format!("Suggested from the conversation: {suggestion}"),
                        11.,
                        400.,
                        p.muted,
                    ))
                    .child(
                        btn(
                            format!("suggest-meeting-answer-{qid}"),
                            "Use suggested answer",
                            p,
                            false,
                            true,
                            move |s, _| select(s, &suggestion_key, &suggestion),
                        )
                        .h(29.),
                    );
            }
            let meeting = id.to_owned();
            let question_id = qid.to_owned();
            let save_key = answer_key.clone();
            let dismiss_meeting = id.to_owned();
            let dismiss_id = qid.to_owned();
            group=group.child(row().gap(8.).flex_wrap()
                .child(btn(format!("answer-meeting-question-{qid}"),"Save answer",p,true,enabled(s)&&!answer.trim().is_empty()&&answer.chars().count()<=4000,move |s,_|patch(s,&meeting,json!([{"op":"answer_question","question_id":question_id,"answer_text":draft(s,&save_key,"")}]))).h(31.))
                .child(btn(format!("dismiss-meeting-question-{qid}"),"Dismiss",p,false,enabled(s),move |s,_|patch(s,&dismiss_meeting,json!([{"op":"dismiss_question","question_id":dismiss_id}]))).h(31.)));
        } else {
            let meeting = id.to_owned();
            let question_id = qid.to_owned();
            group = group.child(
                btn(
                    format!("reopen-meeting-question-{qid}"),
                    "Reopen question",
                    p,
                    false,
                    enabled(s),
                    move |s, _| {
                        patch(
                            s,
                            &meeting,
                            json!([{"op":"reopen_question","question_id":question_id}]),
                        )
                    },
                )
                .h(31.),
            );
        }
        content = content.child(group);
    }
    content
}
fn review(s: &State, p: &Palette, id: &str, detail: &Value) -> El {
    let review = &detail["insight_review"];
    let final_status = string(&detail["finalization"], "status");
    let ready =
        string(detail, "status") == "ended" && !matches!(final_status, "pending" | "running");
    let running = string(review, "status") == "running";
    let meeting = id.to_owned();
    let mut content=col().gap(15.).min_w(0.)
        .child(card(p).child(label("A second look at what matters",16.,550.,p.ink))
            .child(label("Check the final insights against the conversation, then resolve uncertainties in your own words.",12.,400.,p.muted))
            .child(label(string(review,"message"),12.,400.,p.muted))
            .child(label(if ready {"Review uses this meeting’s configured AI provider."}else{"Review becomes available after the meeting and its final processing are complete."},11.,400.,p.muted))
            .child(btn("meeting-start-review",if running{"Review in progress…"}else{"Review meeting insights"},p,true,enabled(s)&&ready&&!running,move |s,_|s.command("meetings.review",json!({"id":meeting,"op":"start"}))).h(33.)));
    for question in values(&review["questions"]) {
        let qid = string(question, "id");
        let status = string(question, "status");
        let item = values(&detail["cards"])
            .into_iter()
            .flat_map(values)
            .find(|item| item["id"] == question["item_id"]);
        let question_ready = ready
            && question["superseded"].as_bool() != Some(true)
            && question["revision"].as_u64().is_none_or(|revision| {
                item.is_some_and(|item| item["revision"].as_u64() == Some(revision))
            })
            && (!question["evidence"].is_array()
                || item.is_some_and(|item| item["evidence"] == question["evidence"]));
        let text = question["insight"]["text"]
            .as_str()
            .or_else(|| item.and_then(|v| v["text"].as_str()))
            .unwrap_or("");
        let choices: Vec<(String, String)> = if let Some(map) = question["choices"].as_object() {
            map.iter()
                .map(|(answer, label)| {
                    (
                        answer.clone(),
                        label
                            .as_str()
                            .unwrap_or_else(|| review_label(answer))
                            .to_owned(),
                    )
                })
                .collect()
        } else {
            values(&question["choices"])
                .iter()
                .filter_map(|v| v.as_str())
                .map(|answer| (answer.to_owned(), review_label(answer).to_owned()))
                .collect()
        };
        let mut group = card(p)
            .key(format!("meeting-review-question-{qid}"))
            .min_w(0.)
            .child(label(
                question["question"]
                    .as_str()
                    .or_else(|| question["prompt"].as_str())
                    .or_else(|| question["text"].as_str())
                    .unwrap_or("Does this insight reflect the conversation?"),
                14.,
                550.,
                p.ink,
            ))
            .child(label(text, 12., 400., p.ink).selectable(true));
        for source in values(&question["sources"]) {
            group = group.child(
                label(format!("“{}”", string(source, "text")), 11.5, 400., p.muted)
                    .selectable(true),
            );
        }
        if ready && !question_ready {
            group=group.child(label("This insight changed after the review. Run the review again to check its current wording.",11.,400.,p.muted));
        }
        if status == "open" {
            let edit_key = format!("meeting:{id}:review:{qid}");
            let correction = draft(s, &edit_key, text);
            let field = string(question, "field");
            let owner_key = format!("{edit_key}:owner");
            let owner_name_key = format!("{edit_key}:owner-name");
            let deadline_key = format!("{edit_key}:deadline");
            let initial_owner = item
                .map(|v| string(&v["data"], "owner_participant_id"))
                .unwrap_or("");
            let initial_name = item.map(|v| string(&v["data"], "owner_name")).unwrap_or("");
            let initial_deadline = item
                .and_then(|v| {
                    v["data"]["deadline"]
                        .as_str()
                        .or_else(|| v["data"]["due_date"].as_str())
                })
                .unwrap_or("");
            let owner = draft(s, &owner_key, initial_owner);
            let owner_name = draft(s, &owner_name_key, initial_name);
            let deadline = draft(s, &deadline_key, initial_deadline);
            if choices.iter().any(|(answer, _)| answer == "edit") {
                group = group.child(editor(
                    s,
                    p,
                    &edit_key,
                    "Corrected wording",
                    text,
                    "Write the insight as it should read",
                    true,
                ));
                if field == "owner" {
                    let mut people = row().gap(7.).flex_wrap();
                    for (person_id, name) in
                        std::iter::once((String::new(), "Someone else / unassigned".to_owned()))
                            .chain(values(&detail["participants"]).into_iter().map(|v| {
                                (
                                    string(v, "id").to_owned(),
                                    string(v, "display_name").to_owned(),
                                )
                            }))
                    {
                        let key = owner_key.clone();
                        let chosen = person_id.clone();
                        people = people.child(
                            btn(
                                format!("review-owner-{qid}-{person_id}"),
                                &name,
                                p,
                                owner == person_id,
                                enabled(s),
                                move |s, _| select(s, &key, &chosen),
                            )
                            .h(30.),
                        );
                    }
                    group = group
                        .child(label("Correct owner", 11.5, 550., p.ink))
                        .child(people);
                    if owner.is_empty() {
                        group = group.child(editor(
                            s,
                            p,
                            &owner_name_key,
                            "Other owner’s name",
                            initial_name,
                            "Leave empty if no owner was agreed",
                            false,
                        ));
                    }
                } else if field == "deadline" {
                    group = group.child(editor(
                        s,
                        p,
                        &deadline_key,
                        "Correct deadline",
                        initial_deadline,
                        "Leave empty if no deadline was agreed",
                        false,
                    ));
                }
                if matches!(field, "owner" | "deadline") {
                    group = group.child(label(
                        "Update the wording above to reflect the corrected owner or deadline.",
                        11.,
                        400.,
                        p.muted,
                    ));
                }
            }
            let mut actions = row().gap(7.).flex_wrap();
            for (answer, label) in choices {
                let meeting = id.to_owned();
                let question_id = qid.to_owned();
                let save_key = edit_key.clone();
                let initial = text.to_owned();
                let edit_answer = answer == "edit";
                let field = field.to_owned();
                let owner = owner.clone();
                let owner_name = owner_name.clone();
                let deadline = deadline.clone();
                let metadata_changed = (field == "owner"
                    && (owner != initial_owner || owner_name != initial_name))
                    || (field == "deadline" && deadline != initial_deadline);
                let valid_edit = !correction.trim().is_empty()
                    && correction.chars().count() <= 3500
                    && owner_name.chars().count() <= 120
                    && deadline.chars().count() <= 120
                    && (!metadata_changed || correction != text);
                actions=actions.child(btn(format!("meeting-review-{qid}-{answer}"),&label,p,edit_answer,enabled(s)&&question_ready&&(!edit_answer||valid_edit),move |s,_| {
                    let mut params=json!({"id":meeting,"op":"review_answer","question_id":question_id,"answer":answer});
                    if edit_answer {
                        params["text"]=json!(draft(s,&save_key,&initial));
                        if field=="owner" {params["owner_participant_id"]=if owner.is_empty(){Value::Null}else{json!(owner)};params["owner_name"]=json!(owner_name);}
                        if field=="deadline" {params["deadline"]=json!(deadline);}
                    }
                    s.command("meetings.review",params);
                }).h(31.));
            }
            let meeting = id.to_owned();
            let question_id = qid.to_owned();
            actions = actions.child(
                btn(
                    format!("meeting-review-skip-{qid}"),
                    "Skip for now",
                    p,
                    false,
                    enabled(s) && question_ready,
                    move |s, _| {
                        s.command(
                            "meetings.review",
                            json!({"id":meeting,"op":"review_skip","question_id":question_id}),
                        )
                    },
                )
                .h(31.),
            );
            group = group.child(actions);
        } else {
            group = group.child(label(
                question["answer"]
                    .as_str()
                    .unwrap_or(if status == "skipped" {
                        "Skipped for now"
                    } else {
                        "Reviewed"
                    }),
                11.,
                550.,
                p.accent,
            ));
            let meeting = id.to_owned();
            let question_id = qid.to_owned();
            group = group.child(
                btn(
                    format!("meeting-review-reopen-{qid}"),
                    "Review again",
                    p,
                    false,
                    enabled(s) && question_ready,
                    move |s, _| {
                        s.command(
                            "meetings.review",
                            json!({"id":meeting,"op":"review_reopen","question_id":question_id}),
                        )
                    },
                )
                .h(31.),
            );
        }
        content = content.child(group);
    }
    content
}
fn review_label(answer: &str) -> &'static str {
    match answer {
        "accurate" => "Accurate",
        "accepted" => "Agreed",
        "offered" => "Offered, not agreed",
        "incorrect" => "Incorrect · remove",
        "resolved" => "Risk resolved",
        "open" => "Risk remains open",
        "edit" => "Save correction",
        _ => "Confirm",
    }
}
fn reports(s: &State, p: &Palette, id: &str, detail: &Value) -> El {
    let reports = values(&detail["custom_reports"]);
    let running = reports.iter().any(|r| string(r, "status") == "running");
    let ready = string(detail, "status") == "ended"
        && !matches!(
            string(&detail["finalization"], "status"),
            "pending" | "running"
        );
    let request_key = format!("meeting:{id}:report-request");
    let request = draft(s, &request_key, "");
    let meeting = id.to_owned();
    let save_key = request_key.clone();
    let mut content=col().gap(15.).min_w(0.)
        .child(card(p).min_w(0.).child(label("Ask the meeting for something useful",16.,550.,p.ink))
            .child(label("A client recap, a decision log, or the promises you made. Tailored reports use this meeting’s configured AI provider.",12.,400.,p.muted))
            .child(editor(s,p,&request_key,"What would you like?","","Summarize the decisions, owners, and deadlines with quotes from the discussion.",true))
            .child(label(format!("{} / 2,000 characters",request.chars().count()),10.5,400.,if request.chars().count()>2000 {p.danger}else{p.muted}))
            .child(btn("meeting-request-report",if running{"Writing your report…"}else{"Create tailored report"},p,true,enabled(s)&&ready&&!running&&reports.len()<20&&!request.trim().is_empty()&&request.chars().count()<=2000,move |s,_|s.command("meetings.report.request",json!({"id":meeting,"request":draft(s,&save_key,"")}))).h(33.))
            .child(label(if ready {"You can keep up to 20 reports with this meeting."}else{"Reports become available when the meeting’s final processing is complete."},11.,400.,p.muted)));
    for report in reports {
        let report_id = string(report, "id");
        let title = report["title"]
            .as_str()
            .filter(|v| !v.is_empty())
            .unwrap_or("Tailored report");
        let markdown = string(report, "markdown");
        let copy = markdown.to_owned();
        let mut group = card(p)
            .key(format!("meeting-custom-report-{report_id}"))
            .min_w(0.)
            .child(
                row()
                    .gap(8.)
                    .center_y()
                    .flex_wrap()
                    .child(label(title, 16., 550., p.ink).grow(1.).min_w(0.))
                    .child(pill(
                        match string(report, "status") {
                            "ready" => "Ready",
                            "failed" => "Needs attention",
                            _ => "Writing",
                        },
                        p,
                    )),
            )
            .child(label(string(report, "request"), 11., 400., p.muted));
        if !markdown.is_empty() {
            group = group.child(
                Markdown::new(markdown)
                    .selectable(true)
                    .scroll_wide(true)
                    .build(&p.kit)
                    .min_w(0.),
            );
        }
        if !string(report, "message").is_empty() {
            group = group.child(label(
                string(report, "message"),
                11.,
                400.,
                if string(report, "status") == "failed" {
                    p.danger
                } else {
                    p.muted
                },
            ));
        }
        let export = markdown.to_owned();
        let filename = format!("meeting-report-{report_id}.md");
        let remove_meeting = id.to_owned();
        let remove_id = report_id.to_owned();
        group=group.child(row().gap(8.).flex_wrap()
            .child(btn(format!("copy-meeting-custom-report-{report_id}"),"Copy report",p,false,!markdown.is_empty(),move |s,tx| {tx.copy(copy.clone());s.notice=Some("Report copied".into());}).h(31.))
            .child(btn(format!("export-meeting-custom-report-{report_id}"),"Export Markdown…",p,false,!markdown.is_empty(),move |_,tx| {
                let content=export.clone();tx.save_file(FileDialogOptions {title:"Export tailored report".into(),file_name:Some(filename.clone()),..Default::default()}.filter("Markdown",&["md"]),move |s:&mut State,result,_|match result {Ok(Some(path))=>match std::fs::write(path,&content){Ok(())=>s.notice=Some("Report exported".into()),Err(e)=>s.error=Some(e.to_string())},Ok(None)=>{},Err(e)=>s.error=Some(e.to_string())});
            }).h(31.))
            .child(btn(format!("remove-meeting-custom-report-{report_id}"),"Remove report",p,false,enabled(s),move |s,_|s.confirmation=Some(json!({"kind":"meeting.report.remove","meeting":remove_meeting,"id":remove_id}))).h(31.)))
            .child(confirm_remove(s,p,"meeting.report.remove",id,report_id,"meetings.report.remove",json!({"id":id,"report_id":report_id})));
        content = content.child(group);
    }
    content
}

/// Explicit preview corpus; production commands always use the meeting service.
pub fn meeting_tools_demo(s: &mut State) {
    if !s.data["demo.meetings"].is_array() {
        return;
    }
    if let Some(meeting) = s.data["demo.meetings"]
        .as_array_mut()
        .and_then(|a| a.first_mut())
    {
        meeting["cards"] = json!({
            "user_notes":[{"id":"note-demo","card":"user_notes","text":"Keep the first recording calm, clear, and easy to finish.","status":"edited","pinned":false,"evidence":[],"data":{}}],
            "key_points":[{"id":"point-demo","card":"key_points","text":"First-time setup should focus on one good recording.","status":"proposed","pinned":false,"evidence":["segment-demo-1"],"data":{}}],
            "decisions":[{"id":"decision-demo","card":"decisions","text":"Keep the important capture controls close at hand.","status":"proposed","pinned":false,"evidence":["segment-demo-2"],"data":{}}],
            "action_items":[{"id":"action-demo","card":"action_items","text":"Maya will share the revised flow this afternoon.","status":"proposed","pinned":false,"evidence":["segment-demo-3"],"data":{"owner_participant_id":"person-maya","deadline":"This afternoon"}}],"risks":[],"timeline":[],"live_notes":[]
        });
        meeting["participants"] = json!({"person-alex":{"id":"person-alex","display_name":"Alex","kind":"me","is_provisional":false},"person-maya":{"id":"person-maya","display_name":"Maya","kind":"others_cluster","is_provisional":false}});
        meeting["segments"] = json!([{"id":"segment-demo-1","text":"Let’s make the first experience feel welcoming.","speaker_participant_id":"person-maya"},{"id":"segment-demo-2","text":"We can simplify the setup and keep the important controls close by.","speaker_participant_id":"person-alex"},{"id":"segment-demo-3","text":"I’ll share the updated flow with the team this afternoon.","speaker_participant_id":"person-maya"}]);
        meeting["questions"] = json!({"question-demo":{"id":"question-demo","text":"Who should try the new recording flow first?","status":"open","suggested_answer":"The team’s regular dictation users."}});
        meeting["custom_reports"] = json!([{"id":"report-demo","title":"A thoughtful next step","request":"Write a short recap of the decisions and owners.","status":"ready","markdown":"## Decisions\n\nKeep first-time setup focused on a successful recording.\n\n## Next steps\n\n- **Maya:** Share the revised flow this afternoon.\n- **Alex:** Check keyboard navigation before Friday."}]);
        meeting["insight_review"] = json!({"status":"ready","message":"One detail could use your judgment.","questions":[{"id":"review-demo","status":"open","field":"acceptance","item_id":"action-demo","text":"Was this a firm commitment?","choices":{"accepted":"Agreed","offered":"Only proposed","edit":"Correct it","incorrect":"Incorrect"}}]});
        meeting["state"] = json!({"topic":{"current":"A calmer first recording"},"intent":{"text":"Capture decisions, owners, and the details that will make setup feel welcoming."},"rolling_summary":meeting["summary"],"cards":meeting["cards"],"participants":meeting["participants"],"questions":meeting["questions"]});
    }
}
pub fn meeting_tools_demo_command(s: &mut State, method: &str, params: &Value) -> bool {
    if method == "meetings.current" {
        s.data[method] = s.data["demo.meetings"][0].clone();
        return true;
    }
    if !matches!(
        method,
        "meetings.patch" | "meetings.review" | "meetings.report.request" | "meetings.report.remove"
    ) {
        return false;
    }
    s.data["demo.last_meeting_command"] = json!({"method":method,"params":params});
    let Some(index) = s.data["demo.meetings"]
        .as_array()
        .and_then(|items| items.iter().position(|v| v["id"] == params["id"]))
    else {
        return true;
    };
    let meeting = &mut s.data["demo.meetings"][index];
    match method {
        "meetings.patch" => {
            for op in values(&params["ops"]) {
                match string(op, "op") {
                    "add_item" => {
                        let card = string(op, "card");
                        if !meeting["cards"][card].is_array() {
                            meeting["cards"][card] = json!([]);
                        }
                        let len = meeting["cards"][card].as_array().unwrap().len();
                        meeting["cards"][card].as_array_mut().unwrap().push(json!({"id":format!("item-demo-{len}"),"card":card,"text":op["text"],"status":"edited","pinned":false,"evidence":[],"data":{}}));
                    }
                    "update_item" | "remove_item" | "pin_item" | "unpin_item" | "confirm_item" => {
                        for (_, items) in meeting["cards"].as_object_mut().into_iter().flatten() {
                            for item in items.as_array_mut().into_iter().flatten() {
                                if item["id"] == op["id"] {
                                    match string(op, "op") {
                                        "update_item" => {
                                            for (k, v) in
                                                op["set"].as_object().into_iter().flatten()
                                            {
                                                item[k] = v.clone();
                                            }
                                            item["status"] = json!("edited");
                                        }
                                        "remove_item" => item["status"] = json!("removed"),
                                        "pin_item" => item["pinned"] = json!(true),
                                        "unpin_item" => item["pinned"] = json!(false),
                                        "confirm_item" => item["status"] = json!("confirmed"),
                                        _ => {}
                                    }
                                }
                            }
                        }
                    }
                    "rename_participant" => {
                        let pid = string(op, "participant_id");
                        meeting["participants"][pid]["display_name"] = op["display_name"].clone();
                    }
                    "upsert_participant" => {
                        let count = meeting["participants"]
                            .as_object()
                            .map(|m| m.len())
                            .unwrap_or(0);
                        let pid = format!("person-demo-{count}");
                        meeting["participants"][&pid] = json!({"id":pid,"display_name":op["display_name"],"is_provisional":false});
                    }
                    "reassign_segment_speaker" => {
                        for segment in meeting["segments"].as_array_mut().into_iter().flatten() {
                            if segment["id"] == op["segment_id"] {
                                segment["speaker_participant_id"] = op["participant_id"].clone();
                            }
                        }
                    }
                    "ask_question" => {
                        let count = meeting["questions"]
                            .as_object()
                            .map(|m| m.len())
                            .unwrap_or(0);
                        let qid = format!("question-demo-{count}");
                        meeting["questions"][&qid] =
                            json!({"id":qid,"text":op["text"],"status":"open"});
                    }
                    "answer_question" | "dismiss_question" | "reopen_question" => {
                        let qid = string(op, "question_id");
                        meeting["questions"][qid]["status"] = json!(match string(op, "op") {
                            "answer_question" => "resolved",
                            "dismiss_question" => "dismissed",
                            _ => "open",
                        });
                        if string(op, "op") == "answer_question" {
                            meeting["questions"][qid]["answer"] = op["answer_text"].clone();
                        }
                    }
                    "set_topic" => meeting["state"]["topic"]["current"] = op["text"].clone(),
                    "set_meeting_intent" => meeting["state"]["intent"]["text"] = op["text"].clone(),
                    "set_rolling_summary" => meeting["summary"] = op["text"].clone(),
                    _ => {}
                }
            }
        }
        "meetings.review" => {
            if string(params, "op") == "start" {
                meeting["insight_review"]["status"] = json!("ready");
            }
            for q in meeting["insight_review"]["questions"]
                .as_array_mut()
                .into_iter()
                .flatten()
            {
                if q["id"] == params["question_id"] {
                    q["status"] = json!(match string(params, "op") {
                        "review_answer" => "answered",
                        "review_skip" => "skipped",
                        _ => "open",
                    });
                    q["answer"] = params["answer"].clone();
                }
            }
        }
        "meetings.report.request" => {
            let len = meeting["custom_reports"]
                .as_array()
                .map(Vec::len)
                .unwrap_or(0);
            meeting["custom_reports"].as_array_mut().unwrap().push(json!({"id":format!("report-demo-{len}"),"title":"Your tailored recap","request":params["request"],"status":"ready","markdown":"## Your next steps\n\nKeep setup welcoming, make recording status clear, and review the details together."}));
        }
        "meetings.report.remove" => {
            if let Some(reports) = meeting["custom_reports"].as_array_mut() {
                reports.retain(|r| r["id"] != params["report_id"]);
            }
        }
        _ => {}
    }
    let updated = meeting.clone();
    s.data["meetings.detail"] = updated.clone();
    s.data["meetings.current"] = updated;
    let results = values(&params["ops"])
        .iter()
        .map(|op| json!({"ok":true,"op":op}))
        .collect::<Vec<_>>();
    meeting_tools_response(
        s,
        method,
        &json!({"id":params["id"],"ok":true,"results":results}),
    );
    s.notice = Some("Meeting updated".into());
    true
}
