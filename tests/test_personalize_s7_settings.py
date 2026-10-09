"""Settings → Recording: the preferred microphone, its backups, and the Basic mirror."""
import os
import tempfile
import threading
import time
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QCoreApplication
from PyQt6.QtWidgets import QAbstractButton, QApplication, QComboBox, QLabel, QPushButton

from services import audio_devices
from services.audio_devices import InputDevice
from services.settings import SettingsKey, SettingsManager, SettingsView
from ui_qt.dialogs import settings_dialog as settings_dialog_module
from ui_qt.dialogs import settings_downloads as downloads_module
from ui_qt.dialogs import settings_models as models_module
from ui_qt.dialogs.settings_destinations import BASIC_DICTATION, RECORDING
from ui_qt.dialogs.settings_microphones import SYSTEM_DEFAULT, entry_token, rail_value

MME, DSOUND = "MME", "Windows DirectSound"
USB = {"name": "Microphone (2- USB Audio Device", "hostapi": MME}
SNOWBALL = {"name": "Microphone (Blue Snowball )", "hostapi": MME}
LAPEL = {"name": "Microphone (Lapel)", "hostapi": MME}
DSOUND_USB = {"name": "Microphone (2- USB Audio Device)", "hostapi": DSOUND}
DEVICES = [
    InputDevice(0, "Microsoft Sound Mapper - Input", MME, 2, default_api=True),
    InputDevice(1, SNOWBALL["name"], MME, default_api=True),
    InputDevice(2, USB["name"], MME, label="Microphone (2- USB Audio Device)", default_api=True),
    InputDevice(3, "Microphone (Webcam)", MME, default_api=True),
    InputDevice(4, DSOUND_USB["name"], DSOUND),
    InputDevice(5, "Microphone (Webcam)", DSOUND),
]


@pytest.fixture(scope="module", autouse=True)
def _qapp():
    return QApplication.instance() or QApplication([])


def _pump_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        QCoreApplication.processEvents()
        threading.Event().wait(0.001)
    assert predicate()


@pytest.fixture
def make_dialog(monkeypatch):
    stacks = []
    temp = tempfile.TemporaryDirectory()
    devices = list(DEVICES)
    monkeypatch.setattr(settings_dialog_module.AudioRecorder, "get_input_devices", staticmethod(lambda: list(devices)))

    def build(values=None, *, view=SettingsView.ADVANCED, wait=True):
        store = SettingsManager(os.path.join(temp.name, f"settings{len(stacks)}.json"))
        store.save_all_settings({SettingsKey.SETTINGS_VIEW: view, **(values or {})})
        stack = ExitStack()
        for module in (settings_dialog_module, models_module, downloads_module):
            stack.enter_context(patch.object(module, "settings_manager", store))
        stack.enter_context(patch.object(settings_dialog_module.history_manager, "set_retention"))
        for module in (models_module, downloads_module):
            stack.enter_context(patch.object(module, "scan_cached_models", return_value={}))
        stack.enter_context(patch("services.local_asr.cache.inventory", return_value={}))
        stacks.append(stack)
        dialog = settings_dialog_module.SettingsDialog(get_loaded_model=lambda: None, background_cache_scan=False)
        dialog.on_audio_device_changed = MagicMock()
        if view == SettingsView.ADVANCED:
            dialog.select_destination(RECORDING)
        if wait:
            _pump_until(lambda: dialog._microphones is not None and dialog._microphones.devices is not None)
        return dialog, store

    build.devices = devices
    yield build
    for stack in reversed(stacks):
        stack.close()
    temp.cleanup()


def _rows(dialog):
    layout = dialog._microphones.rows_layout
    return [layout.itemAt(i).widget() for i in range(layout.count())]


def _row_text(row):
    return [label.text() for label in row.findChildren(QLabel)]


def _button(row, kind):
    return next(button for button in row.findChildren(QPushButton) if button.property("kind") == kind)


def _combo_items(combo):
    return [(combo.itemText(i), combo.itemData(i)) for i in range(combo.count())]


def test_preferred_combo_lists_one_row_per_microphone_and_keeps_saved_ones(make_dialog):
    dialog, _store = make_dialog({SettingsKey.AUDIO_INPUT_PRIORITY: [USB, LAPEL, DSOUND_USB]})

    assert _combo_items(dialog.audio_device_combo) == [
        ("System default", SYSTEM_DEFAULT),
        ("Microphone (Blue Snowball)", entry_token(SNOWBALL)),
        ("Microphone (USB Audio Device)", entry_token(USB)),
        ("Microphone (Webcam)", entry_token({"name": "Microphone (Webcam)", "hostapi": MME})),
        ("Microphone (Lapel) · Not connected", entry_token(LAPEL)),
        ("Microphone (USB Audio Device) · DirectSound", entry_token(DSOUND_USB)),
    ]
    assert dialog.audio_device_combo.currentData() == entry_token(USB)


def test_backups_list_in_order_with_a_greyed_disconnected_row_and_system_default_last(make_dialog):
    dialog, _store = make_dialog({SettingsKey.AUDIO_INPUT_PRIORITY: [USB, LAPEL, SNOWBALL]})

    rows = _rows(dialog)
    assert [_row_text(row) for row in rows] == [
        ["Microphone (Lapel)", "Not connected"],
        ["Microphone (Blue Snowball)"],
        ["System default", "Always last"],
    ]
    assert [row.property("state") for row in rows] == ["missing", "", "fixed"]
    assert not _button(rows[0], "up").isEnabled() and _button(rows[0], "down").isEnabled()
    assert _button(rows[1], "up").isEnabled() and not _button(rows[1], "down").isEnabled()
    assert rows[2].findChildren(QPushButton) == []
    assert dialog.rail.value(RECORDING) == "USB Audio Device → Lapel +1"


def test_up_down_and_remove_save_the_new_order(make_dialog):
    dialog, store = make_dialog({SettingsKey.AUDIO_INPUT_PRIORITY: [USB, LAPEL, SNOWBALL]})

    _button(_rows(dialog)[1], "up").click()
    assert store.get(SettingsKey.AUDIO_INPUT_PRIORITY) == [USB, SNOWBALL, LAPEL]
    _button(_rows(dialog)[0], "down").click()
    assert store.get(SettingsKey.AUDIO_INPUT_PRIORITY) == [USB, LAPEL, SNOWBALL]
    _button(_rows(dialog)[0], "remove").click()
    assert store.get(SettingsKey.AUDIO_INPUT_PRIORITY) == [USB, SNOWBALL]

    assert [call.args[0] for call in dialog.on_audio_device_changed.call_args_list] == [
        [USB, SNOWBALL, LAPEL], [USB, LAPEL, SNOWBALL], [USB, SNOWBALL],
    ]
    assert dialog.rail.value(RECORDING) == "USB Audio Device → Blue Snowball"


def test_add_offers_connected_microphones_not_already_ranked(make_dialog):
    dialog, store = make_dialog({SettingsKey.AUDIO_INPUT_PRIORITY: [USB]})
    add = dialog._microphones.add_combo
    webcam = {"name": "Microphone (Webcam)", "hostapi": MME}

    assert [text for text, _data in _combo_items(add)] == [
        "Add a microphone…", "Microphone (Blue Snowball)", "Microphone (Webcam)",
    ]
    add.setCurrentIndex(add.findData(entry_token(webcam)))
    add.activated.emit(add.currentIndex())

    assert store.get(SettingsKey.AUDIO_INPUT_PRIORITY) == [USB, webcam]
    assert add.currentIndex() == 0
    assert [text for text, _data in _combo_items(add)] == ["Add a microphone…", "Microphone (Blue Snowball)"]


def test_choosing_a_backup_as_preferred_moves_it_to_the_front(make_dialog):
    dialog, store = make_dialog({SettingsKey.AUDIO_INPUT_PRIORITY: [USB, LAPEL, SNOWBALL]})
    combo = dialog.audio_device_combo

    combo.setCurrentIndex(combo.findData(entry_token(SNOWBALL)))

    assert store.get(SettingsKey.AUDIO_INPUT_PRIORITY) == [SNOWBALL, USB, LAPEL]
    assert store.get(SettingsKey.AUDIO_INPUT_DEVICE) == 1
    assert [_row_text(row)[0] for row in _rows(dialog)] == [
        "Microphone (USB Audio Device)", "Microphone (Lapel)", "System default",
    ]


def test_system_default_clears_the_order_and_hides_backups(make_dialog):
    dialog, store = make_dialog({
        SettingsKey.AUDIO_INPUT_PRIORITY: [USB, SNOWBALL], SettingsKey.AUDIO_INPUT_DEVICE: 2,
    })
    combo = dialog.audio_device_combo

    combo.setCurrentIndex(combo.findData(SYSTEM_DEFAULT))

    assert store.get(SettingsKey.AUDIO_INPUT_PRIORITY) == []
    assert SettingsKey.AUDIO_INPUT_DEVICE not in store.load_all_settings()
    assert dialog.microphone_backup_tile.isHidden()
    assert dialog.rail.value(RECORDING) == "System default"


def test_a_preferred_microphone_on_another_host_api_drops_the_legacy_index(make_dialog):
    dialog, store = make_dialog({SettingsKey.AUDIO_INPUT_DEVICE: 2, SettingsKey.AUDIO_INPUT_PRIORITY: [USB, DSOUND_USB]})
    combo = dialog.audio_device_combo

    combo.setCurrentIndex(combo.findData(entry_token(DSOUND_USB)))

    assert store.get(SettingsKey.AUDIO_INPUT_PRIORITY) == [DSOUND_USB, USB]
    assert SettingsKey.AUDIO_INPUT_DEVICE not in store.load_all_settings()


def test_basic_microphone_writes_the_first_entry_and_keeps_the_backups(make_dialog):
    dialog, store = make_dialog({SettingsKey.AUDIO_INPUT_PRIORITY: [USB, SNOWBALL]}, view=SettingsView.BASIC)
    _pump_until(lambda: dialog._microphones.devices is not None)
    combo = dialog._basic_pages[BASIC_DICTATION].controls[SettingsKey.AUDIO_INPUT_PRIORITY]
    assert combo.currentData() == entry_token(USB)
    webcam = {"name": "Microphone (Webcam)", "hostapi": MME}

    combo.setCurrentIndex(combo.findData(entry_token(webcam)))

    assert store.get(SettingsKey.AUDIO_INPUT_PRIORITY) == [webcam, USB, SNOWBALL]
    dialog.on_audio_device_changed.assert_called_once_with([webcam, USB, SNOWBALL])


def test_nothing_is_called_disconnected_before_the_device_list_arrives(make_dialog, monkeypatch):
    release = threading.Event()

    def slow():
        assert release.wait(2)
        return list(DEVICES)

    monkeypatch.setattr(settings_dialog_module.AudioRecorder, "get_input_devices", staticmethod(slow))
    dialog, _store = make_dialog({SettingsKey.AUDIO_INPUT_PRIORITY: [USB, LAPEL]}, wait=False)
    try:
        assert [_row_text(row) for row in _rows(dialog)][0] == ["Microphone (Lapel)"]
        assert dialog._microphones.add_combo.itemText(0) == "Looking for microphones…"
        assert not dialog._microphones.add_combo.isEnabled()
    finally:
        release.set()
    _pump_until(lambda: dialog._microphones.devices is not None)
    assert _row_text(_rows(dialog)[0]) == ["Microphone (Lapel)", "Not connected"]


def test_refresh_rereads_devices_only_while_nothing_records(make_dialog, monkeypatch):
    dialog, _store = make_dialog({SettingsKey.AUDIO_INPUT_PRIORITY: [USB]})
    refreshed = []
    monkeypatch.setattr(audio_devices, "refresh_portaudio", lambda sd=None: refreshed.append(True) or True)
    stream = object()
    audio_devices.register_stream(stream)
    try:
        dialog._microphones.refresh_button.click()
        assert "in use" in dialog._microphones.status.text()
        assert refreshed == []
    finally:
        audio_devices.unregister_stream(stream)
    dialog.get_meeting_active = lambda: True
    dialog._microphones.refresh_button.click()
    assert refreshed == []

    dialog.get_meeting_active = lambda: False
    before = dialog._microphones.devices
    dialog._microphones.refresh_button.click()
    assert not dialog._microphones.refresh_button.isEnabled()
    _pump_until(lambda: dialog._microphones.refresh_button.isEnabled())
    assert refreshed == [True]
    assert dialog._microphones.status.isHidden()
    assert dialog._microphones.devices is not before


def test_rule_dictation_records_from_the_saved_order(make_dialog):
    dialog, _store = make_dialog({SettingsKey.AUDIO_INPUT_PRIORITY: [SNOWBALL, USB]})
    created = []

    class FakeRecorder:
        def __init__(self, device_id=None, output_file=None, *, device_priority=None):
            created.append(device_priority)
            self.last_start_error = None

        def set_audio_level_callback(self, _callback):
            pass

        def start_recording(self):
            return False

        def cleanup(self):
            pass

    dialog.on_dictation_transcribe = MagicMock()
    with patch.object(settings_dialog_module, "AudioRecorder", FakeRecorder):
        dialog._toggle_rule_dictation()
        dialog._toggle_rule_dictation()

    assert created == [[SNOWBALL, USB]]


def test_rail_value_reads_short_names():
    assert rail_value({}) == "System default"
    assert rail_value({SettingsKey.AUDIO_INPUT_PRIORITY: [SNOWBALL]}) == "Blue Snowball"


# The Advanced view's narrowest widths; Basic's 460/720 rows are covered by
# test_settings_unified's test_basic_rows_fit_narrow_windows_and_large_fonts.
@pytest.mark.parametrize("ui_mode,width", [("classic", 940), ("omarchy", 560)])
def test_recording_page_fits_narrow_windows_and_large_fonts(make_dialog, monkeypatch, ui_mode, width):
    from ui_qt.utils.font_scale import apply_ui_font_scale, current_ui_font_scale_percent
    from ui_qt.utils.palette import current_palette, set_current_palette
    from ui_qt.utils.theme_manager import ThemeManager

    monkeypatch.setenv("OPENWHISPER_UI", ui_mode)
    app = QApplication.instance()
    previous_style, previous_font = app.styleSheet(), app.font()
    previous_scale, previous_palette = current_ui_font_scale_percent(), current_palette()
    dialog = None
    try:
        apply_ui_font_scale(130, app=app, theme_manager=ThemeManager())
        dialog, _store = make_dialog({SettingsKey.AUDIO_INPUT_PRIORITY: [USB, LAPEL, SNOWBALL]})
        dialog.show()
        dialog._fit_to_screen()
        dialog.resize(width, 600)
        for _ in range(8):
            app.processEvents()
        page = dialog._pages[RECORDING]
        tile = dialog.microphone_backup_tile
        assert tile.isVisible()
        for control in tile.findChildren(QAbstractButton) + dialog.audio_device_tile.findChildren(QComboBox) \
                + tile.findChildren(QComboBox) + [dialog._microphones.refresh_button]:
            if not control.isVisible():
                continue
            assert control.mapTo(page, control.rect().topLeft()).x() >= 0
            assert control.mapTo(page, control.rect().bottomRight()).x() < page.width()
        for row in _rows(dialog):
            for label in row.findChildren(QLabel):
                assert label.width() > 0 and label.height() >= label.sizeHint().height() - 1
        for label in page.findChildren(QLabel):
            if label.isVisible() and label.wordWrap():
                assert label.height() >= label.heightForWidth(label.width())
    finally:
        if dialog is not None:
            dialog.close()
        apply_ui_font_scale(previous_scale, app=app)
        set_current_palette(previous_palette)
        app.setFont(previous_font)
        app.setStyleSheet(previous_style)


def test_search_finds_both_microphone_tiles(make_dialog):
    from ui_qt.dialogs import settings_metadata

    dialog, _store = make_dialog({SettingsKey.AUDIO_INPUT_PRIORITY: [USB]})

    found = {entry.title: entry.destination for entry in dialog._search_index()}

    assert found["Preferred microphone"] == RECORDING
    assert found["If it's unplugged, use"] == RECORDING
    # Before the page is built, search reads its copy from the metadata.
    unbuilt = {name: title for name, title, _ in settings_metadata.PAGE_SEARCH_FIELDS[RECORDING]}
    assert unbuilt["audio_device_tile"] == dialog.audio_device_tile.title_label.text()
    assert unbuilt["microphone_backup_tile"] == dialog.microphone_backup_tile.title_label.text()


def test_system_default_suggests_choosing_a_microphone_for_backups(make_dialog):
    dialog, _store = make_dialog()
    assert "backups" in dialog.audio_device_tile.description_label.text()

    combo = dialog.audio_device_combo
    combo.setCurrentIndex(combo.findData(entry_token(SNOWBALL)))

    assert dialog.audio_device_tile.description_label.text() == "Used for dictation and meetings."
    assert not dialog.microphone_backup_tile.isHidden()


def test_refresh_moves_under_the_combo_in_a_narrow_column(make_dialog):
    from PyQt6.QtWidgets import QBoxLayout

    dialog, _store = make_dialog({SettingsKey.AUDIO_INPUT_PRIORITY: [USB]})
    row = dialog.audio_device_combo.parentWidget()
    dialog.show()
    dialog.setMinimumSize(0, 0)
    try:
        for width, direction in ((1100, QBoxLayout.Direction.LeftToRight),
                                 (520, QBoxLayout.Direction.TopToBottom),
                                 (1100, QBoxLayout.Direction.LeftToRight)):
            dialog.resize(width, 700)
            for _ in range(8):
                QApplication.instance().processEvents()
            assert row.layout().direction() == direction, width
    finally:
        dialog.close()
