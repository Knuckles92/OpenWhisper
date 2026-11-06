use super::*;
use rune::{Cx, Env, Runtime, ScrollSpec};
fn view(s: &State, _: &mut Cx<State>) -> El {
    let p = Palette::new(false);
    col()
        .w_full()
        .h_full()
        .min_w(0.)
        .min_h(0.)
        .padding(24.)
        .scroll(ScrollSpec {
            stick_bottom: false,
        })
        .child(tools(s, &p))
}
fn workspace(tab: &str) -> Runtime<State> {
    let mut s = State::new();
    s.demo = true;
    s.snapshot.ready = true;
    s.selected_item = Some("meeting-demo-1".into());
    s.data["demo.meetings"] = json!([{"id":"meeting-demo-1","title":"A calmer launch","status":"ended","summary":"The team will make setup clearer.","finalization":{"status":"completed"}}]);
    meeting_tools_demo(&mut s);
    s.data["meetings.detail"] = s.data["demo.meetings"][0].clone();
    select(&mut s, "meeting:meeting-demo-1:tools-tab", tab);
    let mut rt = Runtime::new(
        s,
        view,
        (960., 1700.),
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
#[test]
fn personal_note_can_be_added_pinned_and_removed_with_confirmation() {
    let mut rt = workspace("notes");
    rt.update(|s, _| {
        select(
            s,
            "meeting:meeting-demo-1:new:user_notes",
            "A detail that matters.",
        );
    });
    settle(&mut rt);
    tap(&mut rt, "meeting-add-note");
    assert_eq!(
        rt.state.data["meetings.detail"]["cards"]["user_notes"]
            .as_array()
            .unwrap()
            .len(),
        2
    );
    tap(&mut rt, "pin-meeting-item-item-demo-1");
    assert_eq!(
        rt.state.data["meetings.detail"]["cards"]["user_notes"][1]["pinned"],
        json!(true)
    );
    tap(&mut rt, "remove-meeting-item-item-demo-1");
    assert_ne!(
        rt.state.data["meetings.detail"]["cards"]["user_notes"][1]["status"],
        json!("removed")
    );
    tap(&mut rt, "confirm-meeting.item.remove-item-demo-1");
    assert_eq!(
        rt.state.data["meetings.detail"]["cards"]["user_notes"][1]["status"],
        json!("removed")
    );
}
#[test]
fn speaker_rename_uses_participant_identity() {
    let mut rt = workspace("people");
    rt.update(|s, _| {
        select(s, "meeting:meeting-demo-1:person:person-maya", "Maya Chen");
    });
    settle(&mut rt);
    tap(&mut rt, "rename-meeting-person-person-maya");
    assert_eq!(
        rt.state.data["meetings.detail"]["participants"]["person-maya"]["display_name"],
        json!("Maya Chen")
    );
    assert_eq!(
        rt.state.data["demo.last_meeting_command"]["params"]["ops"][0]["participant_id"],
        json!("person-maya")
    );
}
#[test]
fn question_answer_can_be_saved_and_reopened() {
    let mut rt = workspace("questions");
    tap(&mut rt, "suggest-meeting-answer-question-demo");
    tap(&mut rt, "answer-meeting-question-question-demo");
    assert_eq!(
        rt.state.data["meetings.detail"]["questions"]["question-demo"]["status"],
        json!("resolved")
    );
    tap(&mut rt, "reopen-meeting-question-question-demo");
    assert_eq!(
        rt.state.data["meetings.detail"]["questions"]["question-demo"]["status"],
        json!("open")
    );
}
#[test]
fn insight_review_sends_the_domain_answer() {
    let mut rt = workspace("review");
    tap(&mut rt, "meeting-review-review-demo-accepted");
    assert_eq!(
        rt.state.data["demo.last_meeting_command"]["method"],
        json!("meetings.review")
    );
    assert_eq!(
        rt.state.data["demo.last_meeting_command"]["params"]["answer"],
        json!("accepted")
    );
    assert_eq!(
        rt.state.data["meetings.detail"]["insight_review"]["questions"][0]["status"],
        json!("answered")
    );
}
#[test]
fn tailored_report_limit_blocks_oversized_requests() {
    let mut rt = workspace("reports");
    rt.update(|s, _| {
        select(
            s,
            "meeting:meeting-demo-1:report-request",
            &"x".repeat(2001),
        )
    });
    settle(&mut rt);
    tap(&mut rt, "meeting-request-report");
    assert_eq!(
        rt.state.data["meetings.detail"]["custom_reports"]
            .as_array()
            .unwrap()
            .len(),
        1
    );
    rt.update(|s, _| {
        select(
            s,
            "meeting:meeting-demo-1:report-request",
            "Write the decisions and owners.",
        )
    });
    settle(&mut rt);
    tap(&mut rt, "meeting-request-report");
    assert_eq!(
        rt.state.data["meetings.detail"]["custom_reports"]
            .as_array()
            .unwrap()
            .len(),
        2
    );
}
#[test]
fn all_meeting_tool_tabs_fit_narrow_and_wide_views() {
    for width in [620., 960.] {
        let mut rt = workspace("brief");
        rt.set_viewport((width, 1700.), 1.);
        for tab in ["brief", "notes", "people", "questions", "review", "reports"] {
            rt.update(|s, _| select(s, "meeting:meeting-demo-1:tools-tab", tab));
            settle(&mut rt);
            for node in rt
                .elements()
                .iter()
                .filter(|n| n.text.is_some() && !n.exiting)
            {
                assert!(
                    node.rect[0] >= -0.5 && node.rect[0] + node.rect[2] <= width + 0.5,
                    "{tab} at {width}: {:?} {:?}",
                    node.text,
                    node.rect
                );
            }
        }
    }
}

#[test]
fn review_deadline_correction_requires_matching_wording_and_preserves_metadata() {
    let mut rt = workspace("review");
    rt.update(|s, _| {
        s.data["meetings.detail"]["insight_review"]["questions"][0]["field"] = json!("deadline");
        select(
            s,
            "meeting:meeting-demo-1:review:review-demo:deadline",
            "Friday",
        );
    });
    settle(&mut rt);
    tap(&mut rt, "meeting-review-review-demo-edit");
    assert!(rt.state.data["demo.last_meeting_command"].is_null());
    rt.update(|s, _| {
        select(
            s,
            "meeting:meeting-demo-1:review:review-demo",
            "Maya will share the revised flow on Friday.",
        )
    });
    settle(&mut rt);
    tap(&mut rt, "meeting-review-review-demo-edit");
    assert_eq!(
        rt.state.data["demo.last_meeting_command"]["params"]["deadline"],
        json!("Friday")
    );
    assert_eq!(
        rt.state.data["demo.last_meeting_command"]["params"]["text"],
        json!("Maya will share the revised flow on Friday.")
    );
}

#[test]
fn rejected_correction_preserves_the_open_editor_and_success_closes_it() {
    let mut s = State::new();
    select(&mut s, "meeting:m:editing", "note");
    select(&mut s, "meeting:m:item:note", "The corrected wording.");
    let op = json!({"op":"update_item","id":"note","set":{"text":"The corrected wording."}});
    meeting_tools_response(
        &mut s,
        "meetings.patch",
        &json!({"id":"m","ok":false,"results":[{"ok":false,"op":op,"reason":"revision_mismatch"}]}),
    );
    assert_eq!(draft(&s, "meeting:m:editing", ""), "note");
    assert_eq!(
        draft(&s, "meeting:m:item:note", ""),
        "The corrected wording."
    );
    meeting_tools_response(
        &mut s,
        "meetings.patch",
        &json!({"id":"m","ok":true,"results":[{"ok":true,"op":op}]}),
    );
    assert_eq!(draft(&s, "meeting:m:editing", ""), "");
}
