use crate::state::State;
use rune::{Key, KeyEvent, Tx};

/// Keep active desktop chords out of editors without consuming ordinary text.
pub fn suppress_hotkey(s: &State, event: &KeyEvent, tx: &mut Tx) -> bool {
    if s.data
        .pointer("/snapshot/hotkeys_suspended")
        .and_then(|v| v.as_bool())
        == Some(true)
    {
        return false;
    }
    let physical = tx.physical_key().map(|key| format!("{key:?}"));
    let key = match &event.key {
        Key::Text(text) => {
            if text == " " {
                "space".into()
            } else {
                text.to_lowercase()
            }
        }
        Key::Command(c) => c.to_lowercase().to_string(),
        Key::Function(n) => format!("f{n}"),
        Key::Escape => "esc".into(),
        Key::Enter => "enter".into(),
        Key::Tab => "tab".into(),
        Key::Backspace => "backspace".into(),
        Key::Delete => "delete".into(),
        Key::Insert => "insert".into(),
        Key::Left => "left".into(),
        Key::Right => "right".into(),
        Key::Up => "up".into(),
        Key::Down => "down".into(),
        Key::Home => "home".into(),
        Key::End => "end".into(),
        Key::PageUp => "page_up".into(),
        Key::PageDown => "page_down".into(),
        _ => return false,
    };
    let keypad = physical.as_deref().is_some_and(|p| p.starts_with("Numpad"));
    let ctrl = event.mods.ctrl || (!cfg!(target_os = "macos") && event.mods.cmd);
    let logo = event.mods.logo || (cfg!(target_os = "macos") && event.mods.cmd);
    s.data
        .pointer("/snapshot/hotkey_chords")
        .and_then(|v| v.as_array())
        .is_some_and(|chords| {
            chords.iter().filter_map(|v| v.as_str()).any(|chord| {
                let mut required = (false, false, false, false);
                let mut main = Vec::new();
                let chord = chord.to_lowercase();
                for token in chord.split('+').map(str::trim) {
                    match token {
                        "ctrl" | "control" => required.0 = true,
                        "alt" | "option" => required.1 = true,
                        "shift" => required.2 = true,
                        "cmd" | "super" | "win" | "meta" => required.3 = true,
                        _ => main.push(token),
                    }
                }
                if required != (ctrl, event.mods.alt, event.mods.shift, logo) {
                    return false;
                }
                let main = main.join("+");
                let main = main.trim();
                if let Some(main) = main
                    .strip_prefix("kp ")
                    .or_else(|| main.strip_prefix("num "))
                    .or_else(|| main.strip_prefix("numpad "))
                {
                    keypad && main == key
                } else {
                    main == key
                }
            })
        })
}
