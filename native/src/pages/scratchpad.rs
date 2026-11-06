use crate::state::State;
use crate::ui::{Palette, btn, card, label, page_header};
use rune::{El, FileDialogOptions, TextEdit, col, row};
use rune_kit::TextArea;
use serde_json::json;

fn persist(s: &mut State) {
    if let Some(client) = &s.client {
        if let Err(error) = client.send(
            "scratchpad.update",
            json!({"text":s.edits.get("scratchpad").map(|e|e.text.as_str()).unwrap_or("")}),
        ) {
            s.error = Some(error);
        }
    }
}
pub fn scratchpad(s: &State, p: &Palette) -> El {
    if s.data["scratchpad.loaded"] != true {
        let failed = s.data["scratchpad.get"]["save"]["loaded"] == false;
        return col()
            .gap(24.)
            .child(page_header(
                "A little room to think.",
                if failed { "Your saved notes could not be opened. They have been kept unchanged." } else { "Your saved notes are loading before you can edit them." },
                p,
            ))
            .child(card(p).child(label(if failed { "Check the error above, then restart OpenWhisper after restoring access to your notes." } else { "Opening your scratchpad…" }, 14., 400., p.muted)));
    }
    let empty = TextEdit::new("");
    let edit = s.edits.get("scratchpad").unwrap_or(&empty);
    col()
        .gap(24.)
        .child(page_header(
            "A little room to think.",
            "Dictate, collect, and shape your words before sending them anywhere.",
            p,
        ))
        .child(
            card(p)
                .gap(16.)
                .child(
                    row()
                        .gap(10.)
                        .center_y()
                        .child(
                            label(
                                format!("{} words", edit.text.split_whitespace().count()),
                                12.,
                                450.,
                                p.muted,
                            )
                            .grow(1.),
                        )
                        .child(btn(
                            "scratchpad-copy",
                            "Copy all",
                            p,
                            true,
                            !edit.text.is_empty(),
                            |s, tx| {
                                tx.copy(s.edit("scratchpad").text.clone());
                                s.notice = Some("Scratchpad copied".into());
                            },
                        ))
                        .child(btn(
                            "scratchpad-export",
                            "Export",
                            p,
                            false,
                            !edit.text.is_empty(),
                            |_, tx| {
                                tx.save_file(
                                    FileDialogOptions {
                                        title: "Export scratchpad".into(),
                                        file_name: Some("scratchpad.txt".into()),
                                        ..Default::default()
                                    },
                                    |s: &mut State, result, _| match result {
                                        Ok(Some(path)) => {
                                            if let Err(e) =
                                                std::fs::write(path, &s.edit("scratchpad").text)
                                            {
                                                s.error = Some(e.to_string());
                                            } else {
                                                s.notice = Some("Scratchpad exported".into());
                                            }
                                        }
                                        Err(e) => s.error = Some(e.to_string()),
                                        _ => {}
                                    },
                                );
                            },
                        ))
                        .child(btn(
                            "scratchpad-clear",
                            "Clear",
                            p,
                            false,
                            !edit.text.is_empty(),
                            |s, _| {
                                s.edit("scratchpad").text.clear();
                                persist(s);
                            },
                        )),
                )
                .child(
                    TextArea::new(edit)
                        .label("Scratchpad text")
                        .placeholder("Your thoughts can start anywhere…")
                        .width(None)
                        .rows(18)
                        .on_edit(|s: &mut State| s.edit_seeded("scratchpad", ""))
                        .on_change(|s: &mut State, _| persist(s))
                        .on_key(|s, e, tx| crate::ui::suppress_hotkey(s, e, tx))
                        .build(&p.kit)
                        .key("scratchpad-text"),
                ),
        )
        .child(label(
            "Your scratchpad stays in this workspace. Copy or export when it’s ready.",
            11.,
            400.,
            p.muted,
        ))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::state::Page;
    use rune::{Env, Runtime};

    #[test]
    fn saved_notes_load_before_an_editor_can_replace_them() {
        let mut state = State::new();
        state.demo = true;
        state.page = Page::Scratchpad;
        let mut runtime = Runtime::new(
            state,
            crate::view,
            (1220., 860.),
            Env {
                system_dark: false,
                scale: 1.,
            },
        );
        runtime.frame(0.);
        assert!(runtime.find("scratchpad-text").is_none());
        runtime.update(|s, _| {
            // A stale focus bit must not hide the first durable response.
            s.edit("scratchpad").focused = true;
            s.receive(
                "scratchpad.get",
                json!({"result":{"text":"Notes saved before this launch"}}),
            );
        });
        runtime.frame(1. / 60.);
        assert!(runtime.find("scratchpad-text").is_some());
        assert_eq!(
            runtime.state.edits["scratchpad"].text,
            "Notes saved before this launch"
        );
    }

    #[test]
    fn unreadable_saved_notes_keep_the_editor_closed() {
        let mut state = State::new();
        state.demo = true;
        state.page = Page::Scratchpad;
        state.receive("scratchpad.get", json!({"result":{"text":"", "save":{"loaded":false,"error":"Your notes cannot be read"}}}));
        let mut runtime = Runtime::new(
            state,
            crate::view,
            (1220., 860.),
            Env {
                system_dark: false,
                scale: 1.,
            },
        );
        runtime.frame(0.);
        assert!(runtime.find("scratchpad-text").is_none());
        assert_eq!(
            runtime.state.error.as_deref(),
            Some("Your notes cannot be read")
        );
    }
}
