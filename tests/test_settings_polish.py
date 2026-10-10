"""The shared Settings building blocks: switches, compact buttons, Remote engine tabs."""
from __future__ import annotations

import socket
from pathlib import Path

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QAbstractButton, QApplication, QVBoxLayout, QWidget

from services.remote_asr import settings as remote_settings
from services.remote_asr import tailscale
from services.remote_asr.service import RemoteEngineService
from services.settings import SettingsKey, settings_manager
from tests.test_remote_engine import FakeEngine, _wait_for
from ui_qt.widgets import Button, PrimaryButton, SettingTile
from ui_qt.widgets.button_row import ButtonRow
from ui_qt.widgets.buttons import compact_primary_button, neutral_button
from ui_qt.widgets.segmented_bar import SegmentedBar
from ui_qt.widgets.settings_switch import SettingsSwitch
from ui_qt.widgets.wrapped_label import WrappedLabel

THEME = Path(__file__).resolve().parents[1] / "ui_qt" / "styles" / "theme.qss"


def test_setting_tiles_use_switches_that_say_what_they_toggle():
    tile = SettingTile("Paste into the active window", "Types the transcript.")
    try:
        assert isinstance(tile.checkbox, SettingsSwitch)
        assert isinstance(tile.checkbox, QAbstractButton) and tile.checkbox.isCheckable()
        assert tile.checkbox.accessibleName() == "Paste into the active window"
        assert tile.property("checked") is False
        tile.checkbox.setChecked(True)
        assert tile.property("checked") is True
        tile.checkbox.click()
        assert not tile.checkbox.isChecked() and tile.property("checked") is False
    finally:
        tile.deleteLater()


def test_compact_buttons_are_chosen_by_property_and_keep_their_own_name():
    secondary = neutral_button(Button("Forget host"))
    primary = compact_primary_button(PrimaryButton("Use for dictation"))
    primary.setObjectName("remoteUseButton")  # what callers do to find a button
    try:
        assert secondary.property("tone") == "neutral" and secondary.minimumHeight() == 34
        assert primary.property("tone") == "primary" and primary.objectName() == "remoteUseButton"
        # A property selector ranks below #id rules, so the theme's own primary and
        # danger buttons are never overridden by these.
        theme = THEME.read_text(encoding="utf-8")
        assert 'QPushButton[tone="neutral"]' in theme and 'QPushButton[tone="primary"]' in theme
        assert theme.index("QPushButton#primaryButton") < theme.index('QPushButton[tone="primary"]')
    finally:
        secondary.deleteLater()
        primary.deleteLater()


def test_segmented_bar_details_can_change_while_shown():
    bar = SegmentedBar((("Use another computer", "Not paired"), ("Share this computer", "Off")))
    try:
        bar.show()
        clicks = []
        bar.activated.connect(clicks.append)
        bar.set_detail(0, "Paired · jed")
        assert bar.buttons[0]._detail.text() == "Paired · jed"
        bar.setCurrentIndex(1)  # programmatic: not a person's click
        assert clicks == [] and bar.currentIndex() == 1
        bar.buttons[0].click()
        assert clicks == [0] and bar.currentIndex() == 0
    finally:
        bar.close()
        bar.deleteLater()


COMMIT = "d90ca5fe260221311c53c58e660288d3deb8d356"
FOLDER = "C:" + "\\Users\\Big D\\Documents\\openwhisper-models\\faster-whisper-large-v3-turbo-ct2\\model.bin"


def _narrow(label, width=130):
    label.setFixedWidth(width)
    label.show()
    QApplication.processEvents()
    return label


@pytest.mark.parametrize("value", [COMMIT, FOLDER])
def test_wrapped_label_can_break_a_long_value_and_still_reports_it_unchanged(value):
    plain = _narrow(WrappedLabel(value))
    breaking = _narrow(WrappedLabel(value, break_long_words=True))
    try:
        line = breaking.fontMetrics().height()
        # Without the option the widest word demands more than the label has: it gets clipped.
        assert plain.minimumSizeHint().width() > 130
        assert breaking.minimumSizeHint().width() <= 130 and breaking.heightForWidth(130) >= 2 * line
        assert breaking.text() == value
    finally:
        plain.close()
        breaking.close()


def test_wrapped_label_leaves_prose_and_breakable_names_alone():
    for value in ("Multilingual transcription, translation, and language identification", "Systran/faster-whisper-tiny"):
        plain = _narrow(WrappedLabel(value))
        breaking = _narrow(WrappedLabel(value, break_long_words=True))
        try:
            assert breaking.heightForWidth(130) == plain.heightForWidth(130), value
            assert breaking.text() == value
        finally:
            plain.close()
            breaking.close()


def _private_clipboard():
    """These tests write the clipboard; only the offscreen platform keeps it to this process."""
    if QApplication.platformName() != "offscreen":
        pytest.skip("would overwrite the real clipboard")


def test_copying_a_long_value_gives_back_the_original_text():
    _private_clipboard()
    label = WrappedLabel(COMMIT, break_long_words=True)
    label.setTextInteractionFlags(
        Qt.TextInteractionFlag.TextSelectableByMouse | Qt.TextInteractionFlag.TextSelectableByKeyboard
    )
    _narrow(label)
    try:
        label.setFocus()
        QTest.keyClick(label, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
        QTest.keyClick(label, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
        assert label.selectedText() == COMMIT
        assert QApplication.clipboard().text() == COMMIT
    finally:
        label.close()


def test_clean_up_never_rewrites_text_that_did_not_come_from_the_label():
    _private_clipboard()
    label = WrappedLabel(COMMIT, break_long_words=True)
    elsewhere = "from" + chr(0x200B) + "another app"
    QApplication.clipboard().setText(elsewhere)
    label._clean_clipboard()
    assert QApplication.clipboard().text() == elsewhere


def _link_row(width):
    """A ButtonRow of three labelled buttons in a column ``width`` px wide.

    The column is fixed like Downloads' inspector: a top-level widget cannot be
    resized below its layout's minimum, which would hide the very squeeze under test.
    """
    buttons = [Button(label) for label in ("Hugging Face ↗", "Original ↗", "License ↗")]
    for button in buttons:
        neutral_button(button)
        button.setMinimumWidth(0)  # compact buttons clear their floor, as in Settings
    row = ButtonRow(buttons)
    column = QWidget()
    column.setFixedWidth(width)
    layout = QVBoxLayout(column)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.addWidget(row)
    column.show()
    QApplication.processEvents()
    return column, row, buttons


def _overlaps(buttons):
    return [
        (a.text(), b.text())
        for index, a in enumerate(buttons)
        for b in buttons[index + 1:]
        if a.geometry().intersects(b.geometry())
    ]


def test_button_row_shares_one_line_while_the_labels_fit():
    column, row, buttons = _link_row(900)
    try:
        assert len({button.y() for button in buttons}) == 1
        assert not _overlaps(buttons)
    finally:
        column.close()
        column.deleteLater()


@pytest.mark.parametrize("width", [150, 180, 268, 330])
def test_button_row_wraps_instead_of_letting_buttons_overlap(width):
    column, row, buttons = _link_row(width)
    try:
        assert not _overlaps(buttons)
        for button in buttons:
            assert button.width() >= button.sizeHint().width(), button.text()
            assert 0 <= button.x() and button.x() + button.width() <= row.width()
    finally:
        column.close()
        column.deleteLater()


def test_button_row_puts_the_short_row_first_and_follows_the_labels():
    column, row, (repo, origin, license_) = _link_row(268)
    try:
        # Three labels need more than 268 px, so the main link takes the top row alone.
        assert repo.y() < origin.y() == license_.y()
        assert repo.width() == row.width()
        repo.setText("Repository ↗")  # a shorter label and a hidden neighbour re-pick the rows
        license_.hide()
        row.refresh()
        QApplication.processEvents()
        assert license_.isHidden() and repo.y() == origin.y()
        assert not _overlaps([repo, origin])
    finally:
        column.close()
        column.deleteLater()


@pytest.fixture
def remote_page(tmp_path, monkeypatch):
    from ui_qt.dialogs.settings_destinations import REMOTE_ENGINE
    from ui_qt.dialogs.settings_dialog import SettingsDialog

    monkeypatch.setattr(tailscale, "status", lambda timeout=4.0: tailscale.TailscaleStatus("not_installed"))
    monkeypatch.setattr(remote_settings, "load_client_pairing", lambda: None)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    settings_manager.save_setting(SettingsKey.REMOTE_HOST_PORT, port)
    service = RemoteEngineService(lambda: None, identity_dir=str(tmp_path / "id"), bind="127.0.0.1")
    engine = FakeEngine()
    service._engine = lambda: engine
    service._host_addresses = lambda: []
    dialog = SettingsDialog(get_loaded_model=lambda: None, background_cache_scan=False)
    dialog.show()
    dialog.select_destination(REMOTE_ENGINE)
    section = dialog.remote_section
    section.bind(service, lambda name: None)
    yield section, service
    service.shutdown()
    dialog.close()
    dialog.deleteLater()


def _pump(predicate, timeout=10):
    return _wait_for(lambda: (QApplication.processEvents() or True) and predicate(), timeout)


def test_remote_engine_splits_using_another_computer_from_sharing_this_one(remote_page):
    section, _ = remote_page
    assert [b.text() for b in section.tabs.buttons] == ["Use another computer", "Share this computer"]
    assert section.tabs.currentIndex() == 0
    assert not section.client_container.isHidden() and section.host_container.isHidden()
    assert section.tabs.buttons[0]._detail.text() == "Not paired"
    assert section.tabs.buttons[1]._detail.text() == "Off"
    section.tabs.buttons[1].click()
    assert section.client_container.isHidden() and not section.host_container.isHidden()
    # The host-side opt-ins live on the sharing tab, as switches.
    for tile in (section.management_tile, section.keep_records_tile, section.manage_mcp_tile):
        assert isinstance(tile.checkbox, SettingsSwitch)
        assert section.host_container.isAncestorOf(tile)


def test_remote_engine_opens_on_the_side_that_is_in_use(remote_page):
    section, service = remote_page
    section.share_tile.checkbox.setChecked(True)  # sharing, and not paired to another computer
    assert _pump(lambda: service.host_state()["running"])
    assert _pump(lambda: section.tabs.currentIndex() == 1)
    assert section.tabs.buttons[1]._detail.text() == "Sharing · 0 paired"
    assert not section.host_container.isHidden()


def test_remote_engine_keeps_the_tab_a_person_chose(remote_page):
    section, service = remote_page
    section.tabs.buttons[0].click()  # chose to look at the client side
    section.share_tile.checkbox.setChecked(True)
    assert _pump(lambda: service.host_state()["running"])
    _pump(lambda: False, timeout=0.5)
    assert section.tabs.currentIndex() == 0 and not section.client_container.isHidden()


def test_remote_engine_tiles_keep_the_theme_card_style(remote_page):
    """Renaming a tile once stripped its card styling; identifiers go in a property."""
    section, _ = remote_page
    for tile in (section.client_tile, section.share_tile, section.management_tile,
                 section.keep_records_tile, section.manage_mcp_tile, section.devices_tile):
        assert tile.objectName() == "settingsTile"
    assert section.manage_mcp_tile.property("tileId") == "remoteManageMcpTile"


def test_paired_computer_remove_buttons_keep_the_danger_style(remote_page):
    """Renaming Remove once turned it into a grey block; its identifier goes in a property."""
    section, _ = remote_page
    section._rebuild_devices([{"id": "dev-a", "name": "Laptop"}, {"id": "dev-b", "name": "Desk"}], set())
    removes = [button for button in section.devices_tile.findChildren(QAbstractButton)
               if button.property("buttonId") == "remoteRemoveDeviceButton"]
    assert len(removes) == 2
    assert all(button.objectName() == "dangerButton" for button in removes)
