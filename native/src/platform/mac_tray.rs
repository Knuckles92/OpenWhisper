//! AppKit status item; kept on the Rune event thread because AppKit objects
//! cannot cross threads. Menu actions only queue state updates on that thread.
use super::{AppHandle, State, request_quit, tray_action};
use objc2::{
    class, msg_send,
    runtime::{AnyClass, AnyObject, Bool, Sel},
    sel,
};
use std::cell::RefCell;
use std::ffi::{c_char, c_void};
use tray_icon::menu::{Menu, MenuEvent, MenuItem, PredefinedMenuItem};
use tray_icon::{Icon, TrayIcon, TrayIconBuilder};

struct StatusItem {
    icon: TrayIcon,
    record: MenuItem,
    meeting: MenuItem,
    cancel: MenuItem,
}
thread_local! {static STATUS:RefCell<Option<StatusItem>>=const {RefCell::new(None)};}
thread_local! {static QUIT_APP:RefCell<Option<AppHandle<State>>>=const {RefCell::new(None)};}

unsafe extern "C" {
    fn class_addMethod(
        class: *const AnyClass,
        selector: Sel,
        implementation: *const c_void,
        types: *const c_char,
    ) -> Bool;
}

extern "C" fn should_terminate(_: *mut AnyObject, _: Sel, _: *mut AnyObject) -> usize {
    // AppKit asks this for the application menu, Cmd-Q, the Dock's Quit and logout.
    // Cancel its immediate termination, then let the Rune event loop own shutdown.
    QUIT_APP.with_borrow(|app| {
        if let Some(app) = app {
            app.update(|state, _| request_quit(state));
        }
    });
    0 // NSTerminateCancel
}

pub(super) fn install_quit_guard(handle: AppHandle<State>) -> Result<(), String> {
    // SAFETY: called once on Rune's main AppKit thread. The existing winit delegate
    // is retained unchanged; this adds only its previously absent optional method.
    // NSUInteger is 64-bit on every supported macOS target (the Q@:@ method ABI).
    unsafe {
        let app: *mut AnyObject = msg_send![class!(NSApplication), sharedApplication];
        let delegate: *mut AnyObject = msg_send![app, delegate];
        let delegate = delegate.as_ref().ok_or("No macOS application delegate")?;
        let selector = sel!(applicationShouldTerminate:);
        let exists: bool = msg_send![delegate, respondsToSelector: selector];
        if exists {
            return Err("The macOS application delegate already handles termination".into());
        }
        if !class_addMethod(
            delegate.class(),
            selector,
            should_terminate as *const c_void,
            c"Q@:@".as_ptr(),
        )
        .as_bool()
        {
            return Err("Could not install the macOS safe-quit handler".into());
        }
        QUIT_APP.with_borrow_mut(|app| *app = Some(handle));
        // Refresh AppKit's cache of optional delegate methods without replacing it.
        let _: () = msg_send![app, setDelegate: delegate];
    }
    Ok(())
}

pub(super) fn install(app: AppHandle<State>) -> Result<(), String> {
    let menu = Menu::new();
    let record = MenuItem::with_id("openwhisper-record", "Start dictation", true, None);
    let meeting = MenuItem::with_id("openwhisper-meeting", "Start meeting", true, None);
    let cancel = MenuItem::with_id("openwhisper-cancel", "Cancel capture", false, None);
    let open = MenuItem::with_id("openwhisper-open", "Open OpenWhisper", true, None);
    let history = MenuItem::with_id("openwhisper-history", "History", true, None);
    let scratchpad = MenuItem::with_id("openwhisper-scratchpad", "Scratchpad", true, None);
    let settings = MenuItem::with_id("openwhisper-settings", "Settings…", true, None);
    let quit = MenuItem::with_id("openwhisper-quit", "Quit OpenWhisper", true, None);
    menu.append_items(&[
        &open,
        &PredefinedMenuItem::separator(),
        &record,
        &meeting,
        &cancel,
        &PredefinedMenuItem::separator(),
        &history,
        &scratchpad,
        &settings,
        &PredefinedMenuItem::separator(),
        &quit,
    ])
    .map_err(|e| e.to_string())?;
    let mut pixels = vec![0_u8; 32 * 32 * 4];
    for y in 0..32 {
        for x in 0..32 {
            let microphone = ((12..20).contains(&x) && (4..19).contains(&y))
                || ((8..24).contains(&x) && (15..23).contains(&y) && !(10..22).contains(&x))
                || ((10..22).contains(&x) && (22..24).contains(&y))
                || ((15..17).contains(&x) && (23..28).contains(&y))
                || ((11..21).contains(&x) && (27..29).contains(&y));
            if microphone {
                pixels[(y * 32 + x) * 4 + 3] = 255;
            }
        }
    }
    let icon = Icon::from_rgba(pixels, 32, 32).map_err(|e| e.to_string())?;
    let icon = TrayIconBuilder::new()
        .with_id("openwhisper-status")
        .with_icon(icon)
        .with_icon_as_template(true)
        .with_tooltip("OpenWhisper · Ready")
        .with_menu(Box::new(menu))
        .build()
        .map_err(|e| e.to_string())?;
    MenuEvent::set_event_handler(Some(move |event: MenuEvent| {
        let Some(action) = event.id.as_ref().strip_prefix("openwhisper-") else {
            return;
        };
        let action = action.to_string();
        let handle = app.clone();
        app.update(move |state, _| tray_action(state, &handle, &action));
    }));
    STATUS.with_borrow_mut(|state| {
        *state = Some(StatusItem {
            icon,
            record,
            meeting,
            cancel,
        })
    });
    Ok(())
}

pub(super) fn update(recording: bool, meeting: bool, busy: bool, walkthrough: bool) {
    STATUS.with_borrow(|state| {
        if let Some(state) = state {
            state.record.set_text(if recording && walkthrough {
                "Finish walkthrough"
            } else if recording {
                "Finish dictation"
            } else {
                "Start dictation"
            });
            state.record.set_enabled(recording || (!meeting && !busy));
            state.meeting.set_text(if meeting {
                "End meeting"
            } else {
                "Start meeting"
            });
            state.meeting.set_enabled(meeting || (!recording && !busy));
            state.cancel.set_enabled(recording || busy);
            let _ = state.icon.set_tooltip(Some(if meeting {
                "OpenWhisper · Meeting in progress"
            } else if recording {
                "OpenWhisper · Recording"
            } else {
                "OpenWhisper · Ready"
            }));
        }
    });
}
