use super::*;
use rune::{Env, Runtime};
use serde_json::json;

fn workspace(size: (f32, f32), scene: &str) -> Runtime<State> {
    let mut state = State::new();
    state::SCENES
        .iter()
        .find(|(name, _)| *name == scene)
        .unwrap()
        .1(&mut state);
    let mut rt = Runtime::new(
        state,
        view,
        size,
        Env {
            system_dark: false,
            scale: 1.,
        },
    );
    rt.frame(0.);
    rt
}

fn tap(rt: &mut Runtime<State>, key: &str) {
    let (x, y) = rt.find(key).unwrap_or_else(|| panic!("missing {key}"));
    rt.pointer_down(x, y);
    rt.pointer_up(x, y);
    for _ in 0..30 {
        rt.frame(1. / 60.);
    }
}

#[test]
fn navigation_and_history_selection_load_a_readable_transcript() {
    let mut rt = workspace((1220., 860.), "studio");
    tap(&mut rt, "nav-history");
    assert_eq!(rt.state.page, Page::History);
    tap(&mut rt, "entry-demo-0");
    assert_eq!(rt.state.selected.as_deref(), Some("demo-0"));
    assert!(rt.state.detail.contains("quieter workspace"));
    assert!(rt.find("copy-history").is_some());
}

#[test]
fn recording_control_starts_and_stops_without_changing_pages() {
    let mut rt = workspace((1220., 860.), "studio");
    tap(&mut rt, "record-toggle");
    assert!(rt.state.snapshot.recording);
    tap(&mut rt, "record-toggle");
    assert!(!rt.state.snapshot.recording);
    assert!(!rt.state.snapshot.transcript.is_empty());
    assert_eq!(rt.state.page, Page::Record);
}

#[test]
fn busy_recording_button_does_not_start_another_job() {
    let mut rt = workspace((1220., 860.), "studio");
    rt.update(|s, _| s.snapshot.busy = true);
    rt.frame(1. / 60.);
    tap(&mut rt, "record-toggle");
    assert!(!rt.state.snapshot.recording);
}

#[test]
fn history_filters_and_search_update_visible_entries() {
    let mut rt = workspace((1220., 860.), "history");
    tap(&mut rt, "filter-file");
    assert!(rt.find("entry-demo-1").is_some());
    assert!(rt.find("entry-demo-0").is_none());
    rt.update(|s, _| s.search.text = "no match".into());
    rt.frame(1. / 60.);
    assert!(rt.find("entry-demo-1").is_none());
}

#[test]
fn stale_history_response_does_not_replace_the_selected_transcript() {
    let mut s = State::new();
    s.selected = Some("new".into());
    s.detail = "new words".into();
    s.receive(
        "history.detail",
        json!({"result":{"id":"old","text":"old words","title":"old"}}),
    );
    assert_eq!(s.detail, "new words");
}

#[test]
fn visible_text_stays_inside_narrow_and_wide_windows() {
    for size in [(720., 860.), (1220., 980.)] {
        for scene in [
            "studio",
            "dark",
            "history",
            "settings",
            "files",
            "meetings",
            "walkthrough",
            "transcript",
        ] {
            let mut rt = workspace(size, scene);
            for _ in 0..30 {
                rt.frame(1. / 60.);
            }
            for node in rt
                .elements()
                .iter()
                .filter(|e| e.text.is_some() && !e.exiting)
            {
                assert!(
                    node.rect[0] >= -0.5 && node.rect[0] + node.rect[2] <= size.0 + 0.5,
                    "{scene} at {size:?} overflows: {:?} {:?}",
                    node.text,
                    node.rect
                );
            }
        }
    }
}
