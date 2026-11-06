use crate::state::State;
use crate::ui::{self, Palette, btn, card, divider, label, page_header};
use rune::{El, col, row};
use serde_json::json;

use super::setting_toggle;

pub fn settings(s: &State, p: &Palette) -> El {
    let language = s.snapshot.settings["local_asr_language"]
        .as_str()
        .unwrap_or("en");
    let mut languages = row().gap(7.).flex_wrap();
    for (code, title) in [
        ("", "Auto"),
        ("en", "English"),
        ("es", "Spanish"),
        ("fr", "French"),
        ("de", "German"),
        ("it", "Italian"),
        ("pt", "Portuguese"),
        ("ja", "Japanese"),
        ("zh", "Chinese"),
    ] {
        languages = languages.child(
            btn(
                format!("language-{code}"),
                title,
                p,
                language == code,
                s.available() && !s.working(),
                move |s, _| {
                    s.command(
                        "settings.update",
                        json!({"key":"local_asr_language","value":code}),
                    )
                },
            )
            .h(32.),
        );
    }
    col().gap(24.).child(page_header("A workspace that feels like you.","Keep the essentials close. Fine-tune the rest whenever you need to.",p))
        .child(card(p).key("settings-output").gap(22.).child(label("Your words, your way",16.,550.,p.ink))
            .child(setting_toggle(s,p,"transcript_cleanup_enabled","AI cleanup","Tidy punctuation and phrasing using your configured text model."))
            .child(divider(p)).child(setting_toggle(s,p,"auto_paste","Paste into the active app","Send finished dictation to the app you’re working in."))
            .child(divider(p)).child(setting_toggle(s,p,"copy_clipboard","Keep a copy on the clipboard","Make your latest dictation easy to paste again.")))
        .child(card(p).key("settings-language").gap(13.).child(label("Dictation language",16.,550.,p.ink))
            .child(label("Language hints for local speech models. Supported languages depend on your selected model.",12.,400.,p.muted)).child(languages))
        .child(card(p).key("settings-appearance").child(row().gap(20.).center_y()
            .child(col().grow(1.).gap(7.).child(label("A quieter kind of workspace",16.,550.,p.ink)).child(label("Pick the light that helps you think.",12.,400.,p.muted)))
            .child(btn("appearance-light","Daylight",p,!s.dark,true,|s,_|s.set_dark(false)))
            .child(btn("appearance-dark","After hours",p,s.dark,true,|s,_|s.set_dark(true)))))
        .child(card(p).key("settings-model").child(row().gap(15.).center_y().flex_wrap()
            .child(col().grow(1.).gap(8.).child(label("Models & the finer details",16.,550.,p.ink))
                .child(label(format!("Current voice model: {}",ui::engine(&s.snapshot.engine)),12.,400.,p.muted))
                .child(label("Microphones, shortcuts, providers, dictionary, automation, and storage.",12.,400.,p.muted)))
            .child(btn("advanced-settings","Advanced settings",p,false,s.available(),|s,_|s.command("window.settings",json!({}))))
            .child(btn("model-downloads","Model downloads",p,false,s.available(),|s,_|s.command("window.downloads",json!({}))))))
        .child(label(format!("OpenWhisper  ·  {}  ·  Native interface powered by Rune",if s.snapshot.version.is_empty() {"Connecting"} else {&s.snapshot.version}),10.5,400.,p.muted))
}
