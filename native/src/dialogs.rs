use crate::state::State;
use crate::ui::{Palette, btn, card, label};
use rune::{El, ScrollSpec, col, el, row};
use serde_json::Value;

pub fn prompt(s: &State, p: &Palette) -> Option<El> {
    let data = if s.data["prompt"].is_object() {
        &s.data["prompt"]
    } else {
        s.data.pointer("/snapshot/prompts")?.as_array()?.first()?
    };
    if s.data["dismissed_prompt"] == data["id"] {
        return None;
    }
    let title = data["title"].as_str().unwrap_or("OpenWhisper");
    let options = data["options"].as_array();
    let mut actions = row().gap(10.).flex_wrap().justify_end();
    for (i, option) in options.into_iter().flatten().enumerate() {
        let value = option
            .get("value")
            .cloned()
            .unwrap_or_else(|| option.clone());
        let caption = option
            .get("label")
            .or_else(|| option.get("title"))
            .and_then(Value::as_str)
            .map(str::to_owned)
            .unwrap_or_else(|| match &value {
                Value::Bool(true) => "Continue".into(),
                Value::Bool(false) => "Cancel".into(),
                Value::String(v) => v.replace('_', " "),
                _ => value.to_string(),
            });
        let primary =
            value == data["default"] && value != Value::Bool(false) && value != Value::Null;
        actions = actions.child(btn(
            format!("prompt-option-{i}"),
            &caption,
            p,
            primary,
            true,
            move |s, _| s.answer_prompt(value.clone()),
        ));
    }
    let body = card(p)
        .key("service-prompt")
        .w(560.)
        .max_w(560.)
        .role("dialog")
        .label(title)
        .modal(false, |s: &mut State| {
            let prompt = &s.data["prompt"];
            let default = prompt["default"].clone();
            let permitted = prompt["options"].as_array().is_some_and(|options| {
                options
                    .iter()
                    .any(|o| o.get("value").unwrap_or(o) == &default)
            });
            if permitted {
                s.answer_prompt(default);
            }
        })
        .child(label(title, 22., 550., p.ink))
        .child(
            col()
                .max_h(260.)
                .scroll(ScrollSpec {
                    stick_bottom: false,
                })
                .child(label(
                    data["message"].as_str().unwrap_or(""),
                    13.,
                    400.,
                    p.muted,
                )),
        )
        .child(actions);
    Some(
        el().key("prompt-backdrop")
            .absolute(0., 0.)
            .w_full()
            .h_full()
            .fill(rune::Color::BLACK.alpha(0.3))
            .child(col().w_full().h_full().center().padding(24.).child(body)),
    )
}
