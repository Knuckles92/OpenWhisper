use super::*;
use crate::{Page, State, state, view};
use rune::{Env, Runtime};

fn workspace(page: Page, width: f32) -> Runtime<State> {
    let mut s = State::new();
    state::SCENES
        .iter()
        .find(|(name, _)| *name == "studio")
        .unwrap()
        .1(&mut s);
    library_demo(&mut s);
    s.page = page;
    let mut rt = Runtime::new(
        s,
        view,
        (width, 860.),
        Env {
            system_dark: false,
            scale: 1.,
        },
    );
    for _ in 0..5 {
        rt.frame(1. / 60.);
    }
    rt
}

fn settle(rt: &mut Runtime<State>) {
    for _ in 0..30 {
        rt.frame(1. / 60.);
    }
}

fn tap(rt: &mut Runtime<State>, key: &str) {
    for _ in 0..10 {
        let (_, y) = rt.find(key).unwrap_or_else(|| panic!("missing {key}"));
        if y > 110. && y < 790. {
            break;
        }
        rt.wheel(650., 450., 450. - y);
        settle(rt);
    }
    let (x, y) = rt.find(key).unwrap_or_else(|| panic!("missing {key}"));
    assert!(y > 65. && y < 825., "{key} is not reachable: {x},{y}");
    rt.pointer_down(x, y);
    rt.pointer_up(x, y);
    settle(rt);
}

#[test]
fn full_text_search_finds_words_beyond_preview_and_pages_the_library() {
    let mut rt = workspace(Page::History, 1220.);
    rt.update(|s,_| {
        s.data["demo.history"] = json!((0..105).map(|i|json!({"id":format!("long-{i}"),"title":format!("Recording {i}"),"text":format!("{} hidden phrase {}", "Introductory words. ".repeat(30), i),"preview":"Introductory words.","raw":"Original words","kind":"dictation","duration":12.,"words":90,"model":"local"})).collect::<Vec<_>>());
        s.command("history.list",json!({"limit":50,"offset":0}));
    });
    settle(&mut rt);
    tap(&mut rt, "history-next");
    assert_eq!(rt.state.data["history.list"]["offset"], 50);
    assert_eq!(
        rt.state.data["history.list"]["items"]
            .as_array()
            .unwrap()
            .len(),
        50
    );
    rt.update(|s, _| s.search.text = "hidden phrase 104".into());
    settle(&mut rt);
    tap(&mut rt, "history-search-submit");
    assert_eq!(rt.state.data["history.list"]["total"], 1);
    assert!(
        rt.find("entry-long-104").is_some(),
        "A full-text match must remain visible even when its preview omits the query"
    );
}

#[test]
fn history_edit_and_delete_require_explicit_actions() {
    let mut rt = workspace(Page::History, 1220.);
    tap(&mut rt, "entry-demo-0");
    tap(&mut rt, "history-edit");
    rt.update(|s, _| {
        s.edit("history-title").text = "A title I chose".into();
        s.edit("history-body").text = "Words I corrected.".into();
    });
    settle(&mut rt);
    tap(&mut rt, "history-save");
    assert_eq!(
        rt.state.data["demo.history"][0]["text"],
        "Words I corrected."
    );
    tap(&mut rt, "history-delete");
    assert!(rt.state.confirmation.is_some());
    assert_eq!(rt.state.data["demo.history"].as_array().unwrap().len(), 3);
    tap(&mut rt, "history-keep");
    assert!(rt.state.confirmation.is_none());
    tap(&mut rt, "history-delete");
    tap(&mut rt, "history-confirm-delete");
    assert_eq!(rt.state.data["demo.history"].as_array().unwrap().len(), 2);
}

#[test]
fn saved_meetings_open_reports_and_allow_rename() {
    let mut rt = workspace(Page::Meetings, 720.);
    tap(&mut rt, "meeting-meeting-demo-1");
    assert!(
        rt.find("meeting-card").is_none(),
        "Review should open without the capture setup above it"
    );
    assert!(rt.find("meeting-workspace-tab").is_some());
    assert!(
        rt.presented("meeting-detail").unwrap().0[1] < 180.,
        "Opening a saved meeting should reveal its reader immediately"
    );
    tap(&mut rt, "meeting-report-tab");
    assert_eq!(rt.state.selected_tab, "meeting-report");
    assert!(rt.elements().iter().any(|node| {
        node.text
            .as_deref()
            .is_some_and(|text| text.contains("Maya: share the revised flow"))
    }));
    tap(&mut rt, "rename-meeting");
    rt.update(|s, _| s.edit("saved-meeting-title").text = "Planning together".into());
    settle(&mut rt);
    tap(&mut rt, "save-meeting-title");
    assert_eq!(
        rt.state.data["meetings.detail"]["title"],
        "Planning together"
    );
}

#[test]
fn walkthrough_review_keeps_text_image_inclusion_and_order_together() {
    let mut rt = workspace(Page::Walkthrough, 1220.);
    rt.update(|s, _| {
        // A real screenshot path is not needed to verify the document's inclusion metadata.
        s.data["demo.walkthroughs"][0]["steps"][0]["image_path"] = json!("preview.png");
    });
    settle(&mut rt);
    tap(&mut rt, "walkthrough-walkthrough-demo-1");
    assert!(
        rt.find("walkthrough-card").is_none(),
        "Saved review should replace capture setup"
    );
    assert!(rt.presented("walkthrough-detail").unwrap().0[1] < 180.);
    tap(&mut rt, "walkthrough-review-edit");
    rt.update(|s, _| s.edit("walkthrough-edit-step-0").text = "An edited first step".into());
    settle(&mut rt);
    tap(&mut rt, "review-include-0");
    tap(&mut rt, "review-down-0");
    tap(&mut rt, "save-walkthrough-review");
    let steps = rt.state.data["walkthroughs.detail"]["steps"]
        .as_array()
        .unwrap();
    assert_eq!(steps[1]["text"], "An edited first step");
    assert_eq!(steps[1]["included"], false);
    assert_eq!(steps[0]["title"], "Capture a thought");
}

#[test]
fn file_queue_reorders_removes_and_submits_the_selected_files() {
    let mut rt = workspace(Page::Files, 720.);
    rt.update(|s, _| s.file_queue = vec!["/tmp/first.wav".into(), "/tmp/second.wav".into()]);
    settle(&mut rt);
    tap(&mut rt, "move-file-up-1");
    assert_eq!(rt.state.file_queue[0], "/tmp/second.wav");
    tap(&mut rt, "remove-file-1");
    assert_eq!(rt.state.file_queue.len(), 1);
    tap(&mut rt, "transcribe-file");
    assert_eq!(
        rt.state.data["file.queue"]["items"][0]["path"],
        "/tmp/second.wav"
    );
    tap(&mut rt, "file-job-remove-preview-0");
    assert!(
        rt.state.data["file.queue"]["items"]
            .as_array()
            .unwrap()
            .is_empty()
    );
}

#[test]
fn screenshot_gallery_reaches_every_saved_step_with_bounded_pages() {
    let mut rt = workspace(Page::Walkthrough, 720.);
    rt.update(|s,_| {
        s.data["demo.walkthroughs"][0]["steps"] = json!((0..37).map(|i|json!({"index":i,"title":format!("Step {i}"),"text":format!("Details {i}"),"included":true})).collect::<Vec<_>>());
    });
    settle(&mut rt);
    tap(&mut rt, "walkthrough-walkthrough-demo-1");
    for _ in 0..6 {
        tap(&mut rt, "saved-gallery-next");
    }
    assert!(rt.find("walkthrough-step-36").is_some());
    assert!(rt.find("walkthrough-step-0").is_none());
    assert!(rt.state.images.len() <= 32);
}

#[test]
fn dense_library_workflows_stay_within_narrow_and_wide_windows() {
    for width in [720., 1220.] {
        for page in [
            Page::History,
            Page::Meetings,
            Page::Walkthrough,
            Page::Files,
        ] {
            let mut rt = workspace(page, width);
            rt.update(move |s, _| match page {
                Page::History => {
                    s.selected = Some("demo-0".into());
                    s.command("history.detail", json!({"id":"demo-0"}));
                }
                Page::Meetings => {
                    s.selected_item = Some("meeting-demo-1".into());
                    s.command("meetings.detail", json!({"id":"meeting-demo-1"}));
                }
                Page::Walkthrough => {
                    s.selected_item = Some("walkthrough-demo-1".into());
                    s.command("walkthroughs.detail", json!({"id":"walkthrough-demo-1"}));
                }
                Page::Files => {
                    s.file_queue = vec![format!(
                        "/tmp/{}.wav",
                        "a-very-long-filename-with-context-".repeat(8)
                    )]
                }
                _ => {}
            });
            settle(&mut rt);
            for node in rt
                .elements()
                .iter()
                .filter(|node| node.text.is_some() && !node.exiting)
            {
                assert!(
                    node.rect[0] >= -0.5 && node.rect[0] + node.rect[2] <= width + 0.5,
                    "{page:?} at {width}: {:?} {:?}",
                    node.text,
                    node.rect
                );
            }
        }
    }
}
