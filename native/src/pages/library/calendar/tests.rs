use super::*;
use rune::{Env, Runtime};

fn workspace(width: f32) -> Runtime<State> {
    let mut s = State::new();
    crate::state::SCENES
        .iter()
        .find(|(name, _)| *name == "studio")
        .unwrap()
        .1(&mut s);
    s.page = Page::History;
    open(&mut s);
    let mut rt = Runtime::new(
        s,
        crate::view,
        (width, 980.),
        Env {
            system_dark: false,
            scale: 1.,
        },
    );
    for _ in 0..8 {
        rt.frame(1. / 60.);
    }
    rt
}
fn tap(rt: &mut Runtime<State>, key: &str) {
    for _ in 0..10 {
        let (_, y) = rt.find(key).unwrap_or_else(|| panic!("missing {key}"));
        if y > 110. && y < 900. {
            break;
        }
        rt.wheel(600., 450., 450. - y);
        for _ in 0..12 {
            rt.frame(1. / 60.);
        }
    }
    let (x, y) = rt.find(key).unwrap_or_else(|| panic!("missing {key}"));
    rt.pointer_down(x, y);
    rt.pointer_up(x, y);
    for _ in 0..15 {
        rt.frame(1. / 60.);
    }
}
#[test]
fn calendar_month_year_day_and_kind_navigation_reach_the_saved_agenda() {
    let mut rt = workspace(1220.);
    tap(&mut rt, "calendar-next");
    assert_eq!(rt.state.data["history.calendar.get"]["month"], 12);
    tap(&mut rt, "calendar-month-picker");
    tap(&mut rt, "calendar-year-previous");
    tap(&mut rt, "calendar-pick-11");
    assert_eq!(rt.state.data["history.calendar.get"]["year"], 2025);
    tap(&mut rt, "calendar-today");
    assert_eq!(
        rt.state.data["history.calendar.get"]["selected"],
        "2026-11-06"
    );
    tap(&mut rt, "calendar-day-2026-11-05");
    assert!(
        rt.find("calendar-agenda").unwrap().1 < 900.,
        "Selecting a day should bring its agenda into view"
    );
    tap(&mut rt, "calendar-kind-file");
    assert_eq!(
        rt.state.data["history.calendar.get"]["selected"],
        "2026-11-05"
    );
    assert!(
        !rt.state.data["history.calendar.get"]["kinds"]
            .as_array()
            .unwrap()
            .contains(&json!("file"))
    );
    tap(&mut rt, "calendar-today");
    tap(&mut rt, "calendar-open-demo-0");
    assert_eq!(rt.state.selected.as_deref(), Some("demo-0"));
    assert_eq!(rt.state.data["history.view"], "list");
    tap(&mut rt, "open-history-calendar");
    assert_eq!(
        rt.state.data["history.calendar.get"]["selected"],
        "2026-11-06"
    );
}
#[test]
fn calendar_text_stays_inside_narrow_and_wide_windows() {
    for width in [720., 1220.] {
        let mut rt = workspace(width);
        tap(&mut rt, "calendar-month-picker");
        for node in rt
            .elements()
            .iter()
            .filter(|node| node.text.is_some() && !node.exiting)
        {
            assert!(
                node.rect[0] >= -0.5 && node.rect[0] + node.rect[2] <= width + 0.5,
                "at {width}: {:?} {:?}",
                node.text,
                node.rect
            );
        }
    }
}
#[test]
fn late_remote_open_never_steals_navigation_or_overwrites_a_newer_choice() {
    let mut rt = workspace(1220.);
    let payload =
        json!({"id":"remote","kind":"dictation","request_id":"older","item":{"text":"Late words"}});
    rt.state.data["calendar.open_request"] = json!("newer");
    calendar_response(&mut rt.state, "history.calendar.open", &payload);
    assert!(rt.state.selected.is_none());
    rt.state.data["calendar.open_request"] = json!("older");
    rt.state.page = Page::Settings;
    calendar_response(&mut rt.state, "history.calendar.open", &payload);
    assert_eq!(rt.state.page, Page::Settings);
    assert!(rt.state.selected.is_none());
}

#[test]
fn accepted_calendar_and_host_search_data_survive_later_older_events() {
    let mut rt = workspace(1220.);
    calendar_response(
        &mut rt.state,
        "history.calendar.get",
        &json!({"request_id":"newer","items":[]}),
    );
    let newest = json!({"request_id":"newer","items":[{"id":"host-recording"}]});
    calendar_response(&mut rt.state, "history.calendar.loaded", &newest);
    let stale = json!({"request_id":"older","items":[]});
    rt.state.data["events"]["history.calendar.loaded"] = stale.clone();
    calendar_response(&mut rt.state, "history.calendar.loaded", &stale);
    assert_eq!(result(&rt.state)["items"][0]["id"], "host-recording");
    calendar_response(
        &mut rt.state,
        "history.remote.get",
        &json!({"request_id":"search-newer","loading":true}),
    );
    calendar_response(
        &mut rt.state,
        "history.remote.loaded",
        &json!({"request_id":"search-newer","items":[{"id":"matched"}],"loading":false}),
    );
    calendar_response(
        &mut rt.state,
        "history.remote.loaded",
        &json!({"request_id":"search-older","items":[]}),
    );
    assert_eq!(
        rt.state.data["history.remote.accepted"]["items"][0]["id"],
        "matched"
    );
    assert!(!rt.state.pending);
}

#[test]
fn paired_host_search_section_keeps_local_browsing_and_narrow_layout_available() {
    for width in [720., 1220.] {
        let mut rt = workspace(width);
        rt.update(|s,_| {
            s.data["history.view"]=json!("list");
            s.data["history.remote.accepted"]=json!({"request_id":"host-query","query":"","kind":"all","active":true,"loading":false,"host":"Office desktop with a memorable name","items":[{"id":"host-1","title":"Words kept on the paired computer","preview":"A matching phrase from the complete host transcript.","kind":"dictation"}],"total":1,"offset":0,"limit":50,"has_more":false,"notice":""});
        });
        for _ in 0..15 {
            rt.frame(1. / 60.);
        }
        assert!(rt.find("history-host-results").is_some());
        assert!(rt.find("entry-demo-0").is_some());
        for node in rt
            .elements()
            .iter()
            .filter(|node| node.text.is_some() && !node.exiting)
        {
            assert!(
                node.rect[0] >= -0.5 && node.rect[0] + node.rect[2] <= width + 0.5,
                "at {width}: {:?} {:?}",
                node.text,
                node.rect
            );
        }
    }
}

#[test]
fn history_deletion_keeps_local_audio_by_default_and_names_host_recordings() {
    let mut rt = workspace(1220.);
    rt.update(|s,_| {
        s.data["history.view"]=json!("list");
        s.selected=Some("demo-0".into());
        s.command("history.detail",json!({"id":"demo-0"}));
        s.confirmation=Some(json!({"scope":"history","id":"demo-0","title":"Retained transcript","stored_on":"","audio_available":true,"delete_audio":false}));
    });
    for _ in 0..15 {
        rt.frame(1. / 60.);
    }
    assert_eq!(
        rt.state.confirmation.as_ref().unwrap()["delete_audio"],
        false
    );
    tap(&mut rt, "history-delete-audio-choice");
    assert_eq!(
        rt.state.confirmation.as_ref().unwrap()["delete_audio"],
        true
    );
    tap(&mut rt, "history-confirm-delete");
    assert_eq!(
        rt.state.data["demo.library-delete-request"]["delete_audio"],
        true
    );
    rt.update(|s,_| {
        s.selected=Some("demo-1".into());
        s.command("history.detail",json!({"id":"demo-1"}));
        s.confirmation=Some(json!({"scope":"history","id":"remote-1","title":"Host transcript","stored_on":"Office host","audio_available":true}));
    });
    for _ in 0..30 {
        rt.frame(1. / 60.);
    }
    assert!(rt.find("history-delete-audio-choice").is_none());
    assert!(rt.elements().iter().any(|node| {
        node.text
            .as_ref()
            .is_some_and(|text| text.contains("retained recording from Office host"))
    }));
}
