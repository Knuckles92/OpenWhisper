mod bridge;
mod pages;
mod state;
mod ui;

#[cfg(test)]
mod tests;

use rune::{App, AppSpec, Cx, El, Key, KeyEvent, Tx};
use state::{Page, State};

fn view(s: &State, cx: &mut Cx<State>) -> El {
    ui::shell(s, cx)
}

fn on_key(s: &mut State, event: &KeyEvent, _tx: &mut Tx) -> bool {
    match event.key {
        Key::Command('1') => s.page = Page::Record,
        Key::Command('2') => s.page = Page::Files,
        Key::Command('3') => s.page = Page::Meetings,
        Key::Command('4') => s.page = Page::Walkthrough,
        Key::Command('5') => s.page = Page::History,
        Key::Command(',') => s.page = Page::Settings,
        Key::Command('k') => {
            s.page = Page::History;
            s.search.focused = true;
        }
        Key::Escape => {
            s.error = None;
            s.notice = None;
        }
        _ => return false,
    }
    true
}

fn main() {
    let app = App::new(AppSpec {
        min_size: Some((720.0, 580.0)),
        on_key: Some(on_key),
        scenes: state::SCENES,
        ..AppSpec::new("OpenWhisper", (1220.0, 860.0), State::new, view)
    });
    let handle = app.handle();
    app.on_ready(move |_| {
        let worker_handle = handle.clone();
        handle.update(move |s, _| {
            if !s.demo && s.client.is_none() {
                s.client = Some(bridge::Client::start(worker_handle));
            }
        });
    })
    .run();
}
