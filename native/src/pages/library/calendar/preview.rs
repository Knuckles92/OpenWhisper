//! Fixtures for Rune's explicit preview scenes; live data comes from Python.
use crate::state::State;
use serde_json::{Value, json};

pub(super) fn open(s: &mut State, id: &str, kind: &str, request_id: &str) {
    let source = if kind == "meeting" {
        "demo.meetings"
    } else {
        "demo.history"
    };
    if let Some(item) = s.data[source]
        .as_array()
        .into_iter()
        .flatten()
        .find(|item| item["id"] == id)
        .cloned()
    {
        super::calendar_response(
            s,
            "history.calendar.open",
            &json!({"id":id,"kind":kind,"request_id":request_id,"item":item}),
        );
    }
}

pub(super) fn load(s: &mut State, params: &Value) {
    let today = params["today"] == true;
    let year = if today {
        2026
    } else {
        params["year"].as_i64().unwrap_or(2026)
    };
    let month = if today {
        11
    } else {
        params["month"].as_i64().unwrap_or(11).clamp(1, 12)
    };
    let weekday = params["first_weekday"].as_i64().unwrap_or(6);
    let kinds = params["kinds"]
        .as_array()
        .cloned()
        .unwrap_or_else(|| vec![json!("dictation"), json!("file"), json!("meeting")]);
    let chosen = if today {
        "2026-11-06".to_string()
    } else {
        params["day"]
            .as_str()
            .map(str::to_owned)
            .unwrap_or_else(|| format!("{year:04}-{month:02}-06"))
    };
    let names = [
        "January",
        "February",
        "March",
        "April",
        "May",
        "June",
        "July",
        "August",
        "September",
        "October",
        "November",
        "December",
    ];
    let shift = |y: i64, m: i64, step: i64| {
        let n = y * 12 + m - 1 + step;
        (n.div_euclid(12), n.rem_euclid(12) + 1)
    };
    let days = |y: i64, m: i64| match m {
        4 | 6 | 9 | 11 => 30,
        2 => {
            if y % 4 == 0 && (y % 100 != 0 || y % 400 == 0) {
                29
            } else {
                28
            }
        }
        _ => 31,
    };
    let previous = shift(year, month, -1);
    let next = shift(year, month, 1);
    let offsets = [0, 3, 2, 5, 0, 3, 5, 1, 4, 6, 2, 4];
    let y = year - if month < 3 { 1 } else { 0 };
    let sunday = (y + y / 4 - y / 100 + y / 400 + offsets[(month - 1) as usize] + 1) % 7;
    let offset = ((sunday + 6) % 7 - weekday + 7) % 7;
    let total_days = days(year, month);
    let mut weeks = Vec::new();
    for week in 0..((offset + total_days + 6) / 7) {
        let mut cells = Vec::new();
        for column in 0..7 {
            let raw = week * 7 + column - offset + 1;
            let (cell_year, cell_month, day) = if raw < 1 {
                (previous.0, previous.1, days(previous.0, previous.1) + raw)
            } else if raw > total_days {
                (next.0, next.1, raw - total_days)
            } else {
                (year, month, raw)
            };
            let date = format!("{cell_year:04}-{cell_month:02}-{day:02}");
            cells.push(json!({"date":date,"day":day,"year":cell_year,"month":cell_month,"current_month":cell_month==month,"today":date=="2026-11-06","selected":date==chosen,"count":if cell_month==month&&day==6 {4}else{0}}));
        }
        weeks.push(cells);
    }
    let mut items:Vec<Value>=s.data["demo.history"].as_array().into_iter().flatten().map(|item|json!({"id":item["id"],"kind":item["kind"],"title":item["title"],"preview":item["preview"],"time":"09:41","duration":item["duration"],"location":"This computer"})).collect();
    if let Some(meeting) = s.data["demo.meetings"]
        .as_array()
        .and_then(|items| items.first())
    {
        items.push(json!({"id":meeting["id"],"kind":"meeting","title":meeting["title"],"preview":meeting["preview"],"time":"10:30","duration":meeting["duration"],"milestone":"Your 100th recording"}));
    }
    items.retain(|item| kinds.iter().any(|kind| kind == &item["kind"]) && chosen.ends_with("-06"));
    let result = json!({"request_id":"calendar-preview","year":year,"month":month,"label":format!("{} {year}",names[(month-1)as usize]),"today":"2026-11-06","selected":chosen,"selected_label":chosen,"first_weekday":weekday,"kinds":kinds,"place":null,"places":[{"value":["here",""],"label":"This computer"}],"weeks":weeks,"stats":{"count":4,"active_days":1,"seconds":1694.,"busiest":{"day":format!("{year:04}-{month:02}-06"),"count":4}},"streak":3,"months":[{"year":year,"month":month,"count":4}],"previous":[previous.0,previous.1],"next":[next.0,next.1],"nearest":[year,month],"items":items,"total":items.len(),"offset":0,"limit":50,"has_more":false});
    s.data["history.calendar.get"] = result.clone();
    super::calendar_response(s, "history.calendar.get", &result);
}
