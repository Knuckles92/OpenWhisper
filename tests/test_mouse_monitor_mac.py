import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from services import _mouse_monitor_mac as mac


@pytest.fixture
def native(monkeypatch):
    monitors = {}
    removed = []

    def install(name, mask, handler):
        token = object()
        monitors[name] = SimpleNamespace(token=token, mask=mask, handler=handler)
        return token

    events = SimpleNamespace(
        addGlobalMonitorForEventsMatchingMask_handler_=lambda mask, handler: install("global", mask, handler),
        addLocalMonitorForEventsMatchingMask_handler_=lambda mask, handler: install("local", mask, handler),
        removeMonitor_=removed.append,
    )
    appkit = SimpleNamespace(
        NSEvent=events, NSEventTypeLeftMouseDown=1, NSEventTypeLeftMouseUp=2,
        NSEventTypeRightMouseDown=3, NSEventTypeRightMouseUp=4,
    )
    quartz = SimpleNamespace(
        kCGEventSourceUnixProcessID=41,
        CGEventGetIntegerValueField=lambda event, field: event.pid,
        CGEventGetLocation=lambda event: event.point,
    )
    monkeypatch.setitem(sys.modules, "AppKit", appkit)
    monkeypatch.setitem(sys.modules, "Quartz", quartz)
    monkeypatch.setattr(mac, "time", SimpleNamespace(monotonic=lambda: 123.0))
    on_click = Mock()
    monitor = mac.MouseClickMonitor(on_click)
    monitor.start()

    def event(kind, *, pid=0, x=-120.25, y=42.75, local=False):
        item = SimpleNamespace(type=lambda: kind, pid=pid, point=SimpleNamespace(x=x, y=y))
        item.CGEvent = lambda: item
        result = monitors["local" if local else "global"].handler(item)
        if local:
            assert result is item
        return item

    yield SimpleNamespace(monitor=monitor, events=events, monitors=monitors,
                          removed=removed, click=on_click, event=event)
    monitor.stop()


@pytest.mark.parametrize("down,up", [(1, 2), (3, 4)])
def test_external_mouse_and_trackpad_clicks_keep_desktop_points(native, down, up):
    native.event(up)
    native.click.assert_not_called()
    native.event(down)
    native.click.assert_not_called()
    native.event(up)
    native.click.assert_called_once_with(-121, 42, 123.0)
    assert native.monitors["global"].mask == sum(1 << kind for kind in (1, 2, 3, 4))


def test_posted_clicks_and_unmatched_releases_are_ignored(native):
    native.event(1, pid=777)
    native.event(2, pid=777)
    native.event(1)
    native.event(2, pid=777)
    native.event(2)
    native.event(1, pid=777)
    native.event(2)
    native.click.assert_not_called()
    native.event(1)
    native.event(2)
    native.click.assert_called_once()


def test_own_windows_pass_input_through_and_clear_cross_window_drags(native):
    native.event(1, local=True)
    native.event(2, local=True)
    native.event(2)
    native.event(1)
    native.event(2, local=True)
    native.event(1, local=True)
    native.event(2)
    native.click.assert_not_called()
    native.event(3)
    native.event(4)
    native.click.assert_called_once()


def test_start_stop_and_restart_remove_both_monitors_and_reject_stale_events(native):
    original = dict(native.monitors)
    native.monitor.start()
    assert native.monitors == original
    assert native.monitor.running and native.monitor.wait_until_installed()
    native.event(1)
    native.monitor.stop()
    native.monitor.stop()
    assert set(native.removed) == {item.token for item in original.values()}
    assert not native.monitor.running
    assert not native.monitor.wait_until_installed()
    native.monitor.start()
    old_up = SimpleNamespace(type=lambda: 2)
    original["global"].handler(old_up)
    native.event(2)
    native.click.assert_not_called()
    native.event(1)
    native.event(2)
    native.click.assert_called_once()


def test_partial_installation_is_cleaned_up_and_can_be_retried(native, monkeypatch):
    native.monitor.stop()
    install = native.events.addGlobalMonitorForEventsMatchingMask_handler_
    monkeypatch.setattr(native.events, "addGlobalMonitorForEventsMatchingMask_handler_",
                        lambda *_: None)
    with pytest.raises(OSError, match="click observation"):
        native.monitor.start()
    assert not native.monitor.running
    assert native.monitors["local"].token in native.removed
    monkeypatch.setattr(native.events, "addGlobalMonitorForEventsMatchingMask_handler_", install)
    native.monitor.start()
    assert native.monitor.running


def test_callback_failure_cannot_escape_into_appkit(native):
    native.click.side_effect = RuntimeError("callback failed")
    native.event(1)
    native.event(2)
    native.click.assert_called_once()
