use crate::state::{Page, State};
use crate::ui::{Palette, btn, card, label};
use rune::{El, col, el, row};
use serde_json::{Value, json};

fn result(s: &State) -> &Value {
    if s.data["history.remote.accepted"].is_object() {
        &s.data["history.remote.accepted"]
    } else {
        &s.data["history.remote.get"]
    }
}

pub(super) fn response(s: &mut State, method: &str, result: &Value) {
    if method == "history.remote.get" {
        s.data["history.remote.accepted"] = result.clone();
        let early = s.data["events"]["history.remote.loaded"].clone();
        if early["request_id"].is_string() && early["request_id"] == result["request_id"] {
            s.data["history.remote.accepted"] = early;
        }
        return;
    }
    if method == "history.remote.loaded" {
        if result["request_id"].is_string()
            && result["request_id"] == s.data["history.remote.accepted"]["request_id"]
        {
            s.data["history.remote.accepted"] = result.clone();
        }
        return;
    }
    if s.demo || s.page != Page::History || s.data["history.view"] == "calendar" {
        return;
    }
    if method == "history.list" {
        if result["query"].as_str() != Some(s.search.text.trim()) {
            return;
        }
        let previous = self::result(s);
        let same = previous["query"].as_str() == Some(s.search.text.trim())
            && previous["kind"] == s.filter;
        let offset = if same {
            previous["offset"].as_u64().unwrap_or(0)
        } else {
            0
        };
        s.command(
            "history.remote.get",
            json!({"query":s.search.text,"kind":s.filter,"offset":offset,"limit":50}),
        );
    } else if method == "history.delete" {
        let previous = self::result(s);
        let offset = previous["offset"].as_u64().unwrap_or(0);
        s.command(
            "history.remote.get",
            json!({"query":s.search.text,"kind":s.filter,"offset":offset,"limit":50}),
        );
    }
}

pub(super) fn view(s: &State, p: &Palette) -> El {
    let data = result(s);
    if data["active"] != true
        || data["query"].as_str() != Some(s.search.text.trim())
        || data["kind"] != s.filter
    {
        return el();
    }
    let loading = data["loading"] == true;
    let host = data["host"].as_str().unwrap_or("your paired host");
    let mut section=card(p).key("history-host-results").gap(10.)
        .child(row().gap(8.).center_y().flex_wrap()
            .child(label(format!("Kept on {host}"),15.,550.,p.ink).grow(1.).min_w(0.))
            .child(btn("history-host-refresh","Refresh host",p,false,!loading,|s,_|s.command("history.remote.get",json!({"query":s.search.text,"kind":s.filter,"offset":0,"limit":50})))))
        .child(label("Search includes complete transcripts kept only on the paired host. Your local library stays available while it answers.",11.,400.,p.muted));
    if loading {
        return section.child(label("Searching the host…", 12., 400., p.muted));
    }
    if let Some(notice) = data["notice"].as_str().filter(|notice| !notice.is_empty()) {
        section = section.child(label(notice, 12., 400., p.danger));
    }
    let mut rows = col().gap(4.);
    for item in data["items"].as_array().into_iter().flatten() {
        if let Ok(entry) = serde_json::from_value::<crate::state::Entry>(item.clone()) {
            rows = rows.child(crate::ui::entry_row(
                &entry,
                p,
                s.selected.as_deref() == Some(&entry.id),
                false,
            ));
        }
    }
    if data["total"] == 0 && data["notice"] == "" {
        rows = rows.child(label(
            "No matching transcripts kept on the host.",
            12.,
            400.,
            p.muted,
        ));
    }
    let offset = data["offset"].as_u64().unwrap_or(0);
    let count = data["items"].as_array().map_or(0, Vec::len) as u64;
    let total = data["total"].as_u64().unwrap_or(0);
    section=section.child(rows).child(row().gap(8.).center_y().flex_wrap()
        .child(label(if total==0 {"No host results".into()}else{format!("{}–{} of {total} on {host}",offset+1,offset+count)},10.5,400.,p.muted).grow(1.))
        .child(btn("history-host-previous","Previous",p,false,offset>0,move |s,_|s.command("history.remote.get",json!({"query":s.search.text,"kind":s.filter,"offset":offset.saturating_sub(50),"limit":50}))))
        .child(btn("history-host-next","Next",p,false,data["has_more"]==true,move |s,_|s.command("history.remote.get",json!({"query":s.search.text,"kind":s.filter,"offset":offset+50,"limit":50})))));
    if data["limited"] == true {
        section=section.child(label("The host returned its newest 500 matches. Narrow the search to find more specific words.",10.5,400.,p.muted));
    }
    section
}
