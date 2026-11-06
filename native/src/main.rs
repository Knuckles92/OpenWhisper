#![cfg_attr(target_os = "windows", windows_subsystem = "windows")]

mod bridge;
mod dialogs;
mod pages;
mod platform;
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
    if event.key == Key::Command('q') {
        platform::request_quit(s);
        return true;
    }
    if s.page == Page::Screenshot {
        return crate::pages::screenshot_key(s, event);
    }
    if ui::suppress_hotkey(s, event, _tx) {
        return true;
    }
    match event.key {
        Key::Command('1') => s.navigate(Page::Record),
        Key::Command('2') => s.navigate(Page::Files),
        Key::Command('3') => s.navigate(Page::Meetings),
        Key::Command('4') => s.navigate(Page::Walkthrough),
        Key::Command('5') => s.navigate(Page::History),
        Key::Command(',') => s.navigate(Page::Settings),
        Key::Command('k') => {
            s.navigate(Page::History);
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
    let mut app = platform::menus(
        App::new(AppSpec {
            min_size: Some((720.0, 580.0)),
            on_key: Some(on_key),
            setup: Some(|s, wake| s.image_cache = Some(rune::ImageCache::new(wake))),
            scenes: state::SCENES,
            ..AppSpec::new("OpenWhisper", (1220.0, 860.0), State::new, view)
        })
        .keep_running()
        .modifier_keys()
        .on_focus(|s, active, _| s.set_window_active(active))
        .on_close(platform::on_close),
    );
    let mut desktop = Some(platform::install(&mut app));
    let handle = app.handle();
    app.on_ready(move |_| {
        let worker_handle = handle.clone();
        let native_desktop = desktop.take();
        handle.update(move |s, _| {
            if let Some(desktop) = native_desktop {
                platform::ready(s, desktop);
            }
            if !s.demo && s.client.is_none() {
                s.client = Some(bridge::Client::start(worker_handle));
                s.report_window_focus();
            }
        });
    })
    .run();
}
