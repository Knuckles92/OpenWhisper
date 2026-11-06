use crate::state::{Page, State};
use crate::ui::{Palette, btn, card, divider, label, page_header};
use rune::{El, col, el, row};
use serde_json::{Value, json};

mod preview;
#[cfg(test)]
mod tests;

pub fn calendar_response(s: &mut State, method: &str, result: &Value) {
    super::remote::response(s, method, result);
    if method == "history.calendar.get" {
        s.data["history.calendar.accepted"] = result.clone();
        let mut params = s.data["calendar.params"]
            .as_object()
            .cloned()
            .unwrap_or_default();
        params.remove("today");
        params.insert("year".into(), result["year"].clone());
        params.insert("month".into(), result["month"].clone());
        params.insert("day".into(), result["selected"].clone());
        s.data["calendar.params"] = json!(params);
        let early = s.data["events"]["history.calendar.loaded"].clone();
        if early["request_id"].is_string() && early["request_id"] == result["request_id"] {
            s.data["history.calendar.accepted"] = early;
        }
    }
    if method == "history.calendar.loaded" {
        if result["request_id"].is_string()
            && result["request_id"] == s.data["history.calendar.accepted"]["request_id"]
        {
            s.data["history.calendar.accepted"] = result.clone();
        }
        return;
    }
    if method != "history.calendar.open"
        || s.page != Page::History
        || s.data["history.view"] != "calendar"
        || result["request_id"].as_str() != s.data["calendar.open_request"].as_str()
        || !result["request_id"].is_string()
    {
        return;
    }
    if let Some(error) = result["error"].as_str() {
        s.error = Some(error.to_owned());
        return;
    }
    if result["loading"] == true {
        s.notice = Some("Getting this meeting from your paired host…".into());
        return;
    }
    let Some(id) = result["id"].as_str() else {
        return;
    };
    if !result["item"].is_object() {
        return;
    }
    s.data["calendar.open_request"] = Value::Null;
    if result["kind"] == "meeting" {
        s.navigate(Page::Meetings);
        s.selected_item = Some(id.into());
        s.data["meetings.detail"] = result["item"].clone();
    } else {
        s.data["history.view"] = json!("list");
        s.selected = Some(id.into());
        s.selected_tab.clear();
        s.show_raw = false;
        s.detail = result["item"]["text"].as_str().unwrap_or("").into();
        s.detail_raw = result["item"]["raw"].as_str().unwrap_or("").into();
        s.detail_title = result["item"]["title"]
            .as_str()
            .unwrap_or("Transcript")
            .into();
        s.data["history.detail"] = result["item"].clone();
    }
}

fn result(s: &State) -> &Value {
    if s.data["history.calendar.accepted"].is_object() {
        return &s.data["history.calendar.accepted"];
    }
    let initial = &s.data["history.calendar.get"];
    initial
}

pub fn calendar_refresh(s: &mut State) {
    if s.page == Page::History && s.data["history.view"] == "calendar" {
        request(s, json!({"refresh":true}));
    }
}

fn request(s: &mut State, changes: Value) {
    let mut params = s.data["calendar.params"]
        .as_object()
        .cloned()
        .unwrap_or_default();
    params.remove("today");
    if let Some(changes) = changes.as_object() {
        for (key, value) in changes {
            params.insert(key.clone(), value.clone());
        }
    }
    s.data["calendar.params"] = json!(params);
    if s.demo {
        preview::load(s, &json!(params));
        return;
    }
    s.command("history.calendar.get", json!(params));
}

pub(super) fn open(s: &mut State) {
    s.data["history.view"] = json!("calendar");
    s.data["calendar.open_request"] = Value::Null;
    request(s, json!({"today":true,"refresh":true,"offset":0}));
}

fn period(s: &mut State, year: i64, month: i64) {
    request(
        s,
        json!({"year":year,"month":month,"day":null,"offset":0,"refresh":false}),
    );
}

pub(super) fn view(s: &State, p: &Palette, _compact: bool) -> El {
    let data = result(s);
    let year = data["year"].as_i64().unwrap_or(2026);
    let month = data["month"].as_i64().unwrap_or(1);
    let mut page = col()
        .gap(18.)
        .child(page_header(
            "Your words, day by day.",
            "Dictations, transcribed files and meetings, wherever you keep them.",
            p,
        ))
        .child(
            row()
                .gap(8.)
                .center_y()
                .flex_wrap()
                .child(btn(
                    "calendar-back",
                    "Transcript list",
                    p,
                    false,
                    true,
                    |s, _| {
                        s.data["history.view"] = json!("list");
                        s.data["calendar.open_request"] = Value::Null;
                    },
                ))
                .child(el().grow(1.))
                .child(btn(
                    "calendar-refresh",
                    "Refresh",
                    p,
                    false,
                    true,
                    |s, _| request(s, json!({"refresh":true})),
                )),
        );
    if !data["weeks"].is_array() {
        return page.child(card(p).child(label(
            "Loading your recording calendar…",
            14.,
            400.,
            p.muted,
        )));
    }
    let previous = data["previous"].clone();
    let next = data["next"].clone();
    let today = data["today"].as_str().unwrap_or("").to_string();
    let heading = row().gap(8.).center_y().flex_wrap()
        .child(btn("calendar-previous", "←", p, false, year > 2, move |s,_| period(s,previous[0].as_i64().unwrap_or(year),previous[1].as_i64().unwrap_or(month))))
        .child(btn("calendar-month-picker", data["label"].as_str().unwrap_or("Choose a month"), p, true, true, |s,_| {
            let showing = s.data["calendar.picker"] == true;
            if !showing {s.data["calendar.picker_year"]=result(s)["year"].clone();}
            s.data["calendar.picker"] = json!(!showing);
        }).grow(1.).min_w(0.))
        .child(btn("calendar-next", "→", p, false, year < 9998, move |s,_| period(s,next[0].as_i64().unwrap_or(year),next[1].as_i64().unwrap_or(month))))
        .child(btn("calendar-today", "Today", p, false, true, move |s,_| {
            let parts:Vec<_> = today.split('-').collect();
            if parts.len()==3 {request(s,json!({"year":parts[0].parse::<i64>().unwrap_or(year),"month":parts[1].parse::<i64>().unwrap_or(month),"day":today,"offset":0,"refresh":false}));}
        }));
    let mut calendar = card(p).gap(12.).child(heading);
    if s.data["calendar.picker"] == true {
        let picker_year = s.data["calendar.picker_year"].as_i64().unwrap_or(year);
        calendar = calendar.child(
            row()
                .gap(8.)
                .center_y()
                .child(btn(
                    "calendar-year-previous",
                    "← Year",
                    p,
                    false,
                    picker_year > 2,
                    move |s, _| s.data["calendar.picker_year"] = json!(picker_year - 1),
                ))
                .child(label(picker_year.to_string(), 16., 550., p.ink).grow(1.))
                .child(btn(
                    "calendar-year-next",
                    "Year →",
                    p,
                    false,
                    picker_year < 9998,
                    move |s, _| s.data["calendar.picker_year"] = json!(picker_year + 1),
                )),
        );
        let names = [
            "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
        ];
        for start in [0, 4, 8] {
            let mut months = row().gap(7.);
            for (index, name) in names.iter().enumerate().skip(start).take(4) {
                let selected_month = index as i64 + 1;
                let count = data["months"]
                    .as_array()
                    .into_iter()
                    .flatten()
                    .find(|item| item["year"] == picker_year && item["month"] == selected_month)
                    .and_then(|item| item["count"].as_u64())
                    .unwrap_or(0);
                months = months.child(
                    btn(
                        format!("calendar-pick-{selected_month}"),
                        &format!("{name} · {count}"),
                        p,
                        picker_year == year && selected_month == month,
                        true,
                        move |s, _| {
                            s.data["calendar.picker"] = json!(false);
                            period(s, picker_year, selected_month);
                        },
                    )
                    .grow(1.)
                    .min_w(0.),
                );
            }
            calendar = calendar.child(months);
        }
    }
    let mut filters = row().gap(7.).flex_wrap();
    for (kind, title) in [
        ("dictation", "Dictations"),
        ("file", "Files"),
        ("meeting", "Meetings"),
    ] {
        let chosen = data["kinds"]
            .as_array()
            .is_some_and(|items| items.iter().any(|item| item == kind));
        filters = filters.child(btn(
            format!("calendar-kind-{kind}"),
            title,
            p,
            chosen,
            true,
            move |s, _| {
                let mut kinds = result(s)["kinds"].as_array().cloned().unwrap_or_default();
                if kinds.iter().any(|item| item == kind) {
                    if kinds.len() > 1 {
                        kinds.retain(|item| item != kind);
                    }
                } else {
                    kinds.push(json!(kind));
                }
                request(s, json!({"kinds":kinds,"offset":0,"refresh":false}));
            },
        ));
    }
    let first = data["first_weekday"].as_i64().unwrap_or(6);
    filters = filters.child(btn(
        "calendar-week-start",
        if first == 6 {
            "Week starts Sunday"
        } else {
            "Week starts Monday"
        },
        p,
        false,
        true,
        move |s, _| {
            request(
                s,
                json!({"first_weekday":if first==6 {0}else{6},"refresh":false}),
            )
        },
    ));
    calendar = calendar.child(filters);
    if data["places"]
        .as_array()
        .is_some_and(|items| items.len() > 1)
    {
        let mut places = row().gap(7.).flex_wrap().child(btn(
            "calendar-place-all",
            "Everywhere",
            p,
            data["place"].is_null(),
            true,
            |s, _| request(s, json!({"place":null,"offset":0,"refresh":false})),
        ));
        for (index, place) in data["places"].as_array().into_iter().flatten().enumerate() {
            let value = place["value"].clone();
            places = places.child(btn(
                format!("calendar-place-{index}"),
                place["label"].as_str().unwrap_or("Computer"),
                p,
                data["place"] == value,
                true,
                move |s, _| request(s, json!({"place":value,"offset":0,"refresh":false})),
            ));
        }
        calendar = calendar.child(places);
    }
    let weekdays = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
    let mut names = row().gap(5.);
    for day in 0..7 {
        names = names.child(
            label(weekdays[((first + day) % 7) as usize], 10.5, 500., p.muted)
                .w(0.)
                .grow(1.),
        );
    }
    calendar = calendar.child(names);
    for (week_index, week) in data["weeks"].as_array().into_iter().flatten().enumerate() {
        let mut days = row().gap(5.);
        for cell in week.as_array().into_iter().flatten() {
            let day = cell["date"].as_str().unwrap_or("").to_string();
            let cell_year = cell["year"].as_i64().unwrap_or(year);
            let cell_month = cell["month"].as_i64().unwrap_or(month);
            let count = cell["count"].as_u64().unwrap_or(0);
            let title = format!(
                "{}{}{}",
                cell["day"].as_u64().unwrap_or(1),
                if cell["today"] == true { " •" } else { "" },
                if count > 0 {
                    format!("\n{count} ·")
                } else {
                    String::new()
                }
            );
            let mut button=btn(format!("calendar-day-{day}"),&title,p,cell["selected"]==true,true,move |s,tx| {
                request(s,json!({"year":cell_year,"month":cell_month,"day":day,"offset":0,"refresh":false}));
                tx.scroll_key_to("calendar-agenda",0.);
            }).w(0.).grow(1.).min_w(0.).h(53.).px(3.);
            if cell["current_month"] == false {
                button = button.opacity(0.5);
            }
            days = days.child(button);
        }
        calendar = calendar.child(days.key(format!("calendar-week-{week_index}")));
    }
    let stats = &data["stats"];
    calendar = calendar.child(divider(p)).child(label(
        format!(
            "{} recordings · {} recorded · {} active days · {} day streak",
            stats["count"].as_u64().unwrap_or(0),
            crate::ui::duration(stats["seconds"].as_f64().unwrap_or(0.)),
            stats["active_days"].as_u64().unwrap_or(0),
            data["streak"].as_u64().unwrap_or(0)
        ),
        11.5,
        450.,
        p.muted,
    ));
    if let Some(busiest) = stats["busiest"]["day"].as_str() {
        let busiest = busiest.to_string();
        calendar = calendar.child(btn(
            "calendar-busiest",
            &format!("Busiest day: {busiest}"),
            p,
            false,
            true,
            move |s, tx| {
                request(s, json!({"day":busiest,"offset":0,"refresh":false}));
                tx.scroll_key_to("calendar-agenda", 0.);
            },
        ));
    }
    if data["remote_loading"] == true {
        calendar = calendar.child(label(
            "Checking recordings kept on your paired host…",
            11.,
            400.,
            p.muted,
        ));
    }
    if let Some(notice) = data["notice"].as_str().filter(|text| !text.is_empty()) {
        calendar = calendar.child(label(notice, 11., 400., p.muted));
    }
    let mut agenda = card(p).key("calendar-agenda").gap(12.).child(label(
        data["selected_label"].as_str().unwrap_or("Selected day"),
        18.,
        550.,
        p.ink,
    ));
    let items = data["items"].as_array().cloned().unwrap_or_default();
    if items.is_empty() {
        agenda = agenda
            .child(label("A quiet day", 14., 500., p.ink))
            .child(label(
                "Choose a day with recordings, or return to today.",
                12.,
                400.,
                p.muted,
            ));
        if stats["count"] == 0 && data["nearest"].is_array() {
            let nearest = data["nearest"].clone();
            agenda = agenda.child(btn(
                "calendar-nearest",
                "Go to the nearest month with recordings",
                p,
                false,
                true,
                move |s, _| {
                    period(
                        s,
                        nearest[0].as_i64().unwrap_or(year),
                        nearest[1].as_i64().unwrap_or(month),
                    )
                },
            ));
        }
    }
    for item in items {
        let id = item["id"].as_str().unwrap_or("").to_string();
        let kind = item["kind"].as_str().unwrap_or("dictation").to_string();
        let title = item["title"]
            .as_str()
            .filter(|text| !text.is_empty())
            .unwrap_or("Recording");
        let key = format!("calendar-open-{id}");
        let mut entry = col()
            .gap(6.)
            .padding(12.)
            .radius(8.)
            .fill(p.soft)
            .child(label(
                format!("{} · {}", item["time"].as_str().unwrap_or(""), kind),
                10.5,
                500.,
                p.muted,
            ))
            .child(label(title, 14., 500., p.ink))
            .child(label(
                item["preview"].as_str().unwrap_or(""),
                12.,
                400.,
                p.muted,
            ));
        for key in ["location", "milestone"] {
            if let Some(value) = item[key].as_str().filter(|text| !text.is_empty()) {
                entry = entry.child(label(value, 10.5, 450., p.accent));
            }
        }
        agenda = agenda.child(entry.child(btn(
            key,
            "Open recording",
            p,
            false,
            !s.pending,
            move |s, _| {
                let sequence = s.data["calendar.open_sequence"].as_u64().unwrap_or(0) + 1;
                s.data["calendar.open_sequence"] = json!(sequence);
                let request_id = format!("calendar-{sequence}-{id}");
                s.data["calendar.open_request"] = json!(request_id);
                if s.demo {
                    preview::open(s, &id, &kind, &request_id);
                    return;
                }
                s.command(
                    "history.calendar.open",
                    json!({"id":id,"kind":kind,"request_id":request_id}),
                );
            },
        )));
    }
    let offset = data["offset"].as_u64().unwrap_or(0);
    let limit = data["limit"].as_u64().unwrap_or(50);
    agenda = agenda.child(
        row()
            .gap(8.)
            .flex_wrap()
            .child(btn(
                "calendar-agenda-previous",
                "Earlier",
                p,
                false,
                offset > 0,
                move |s, _| {
                    request(
                        s,
                        json!({"offset":offset.saturating_sub(limit),"refresh":false}),
                    )
                },
            ))
            .child(
                label(
                    format!(
                        "{} recordings this day",
                        data["total"].as_u64().unwrap_or(0)
                    ),
                    11.,
                    400.,
                    p.muted,
                )
                .grow(1.),
            )
            .child(btn(
                "calendar-agenda-next",
                "Later",
                p,
                false,
                data["has_more"] == true,
                move |s, _| request(s, json!({"offset":offset+limit,"refresh":false})),
            )),
    );
    page = page.child(calendar).child(agenda);
    page
}
