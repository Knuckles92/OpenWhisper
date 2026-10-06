"""History cards and the entry dialog: versions, chips, playback."""

import importlib
import os
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtWidgets import QApplication, QWidget

import tests.test_personalize_s9_audio_player as player_tests
from services.models import TranscriptionHistory


def _history():
    return importlib.import_module("services.history_manager")


def _playback():
    return importlib.import_module("ui_qt.widgets.history_playback")


@pytest.fixture
def device(monkeypatch):
    """A player on a fake output device, shared by every History control."""
    device = player_tests.FakeSoundDevice()
    module = _playback().audio_player
    player = module.AudioPlayer(sounddevice=device)
    monkeypatch.setattr(module, "_player", player)
    yield device
    player.stop()


@pytest.fixture(autouse=True)
def quiet_sync(monkeypatch):
    monkeypatch.setattr(_playback(), "_fetched", {})
    sync_module = importlib.import_module("services.remote_records.sync")

    class Sync:
        def record_saved(self, *_args):
            pass

        def record_edited(self, *_args):
            pass

        def record_deleted(self, *_args):
            pass

        def audio_for(self, record_id):
            return self.audio

        def listing_wanted(self, _kind):
            return False

    fake = Sync()
    monkeypatch.setattr(sync_module.record_sync, "_instance", fake)
    return fake


def _card(entry, **kwargs):
    from ui_qt.widgets.history_sidebar import HistoryItemWidget

    return HistoryItemWidget(entry, **kwargs)


def _menu(widget):
    captured = {}

    def capture(self, *args, **kwargs):
        captured["actions"] = list(self.actions())

    with patch("ui_qt.widgets.context_menu.QMenu.exec", capture):
        widget._show_context_menu(widget.rect().center())
    return {action.text(): action for action in captured["actions"] if action.text()}


def _cleaned(**fields):
    fields.setdefault("text", "Hello, world.")
    fields.setdefault("raw_text", "um hello world")
    return _history().history_manager.add_entry(model="base", **fields)


def _recorded(tmp_path, **fields):
    source = player_tests._wav(tmp_path / "take.wav")
    return _history().history_manager.add_entry(
        text="played back", model="base", source_audio_path=source, audio_duration=0.25, **fields,
    )


def test_cards_show_the_app_and_cleanup_level():
    entry = TranscriptionHistory.create(
        text="Hi team.", model="base", raw_text="hi team", cleanup_provider="openai",
        cleanup_model="gpt-4o-mini", cleanup_level="medium", app_name="Slack",
    )
    card = _card(entry)
    assert card.app_chip.text() == "Slack"
    assert card.cleanup_chip.text() == "✦ Medium · gpt-4o-mini"
    assert card.cleanup_chip.toolTip() == "Transcript cleaned with OpenAI · gpt-4o-mini (Medium)"

    legacy = TranscriptionHistory.create(
        text="Hi.", model="base", raw_text="hi", cleanup_provider="openai", cleanup_model="gpt-4o-mini",
    )
    legacy_card = _card(legacy)
    assert legacy_card.cleanup_chip.text() == "✦ OpenAI · gpt-4o-mini"
    assert not hasattr(legacy_card, "app_chip")


def test_rewrites_get_a_kind_chip_instead_of_their_source_title():
    command = _card(TranscriptionHistory.create(
        text="Formal.", model="base", source_name="Quick Record", entry_kind="command",
    ))
    assert command.kind_chip.text() == "Command"
    assert not hasattr(command, "title_label")
    transform = _card(TranscriptionHistory.create(
        text="Polished.", model="base", source_name="Polish", entry_kind="transform",
    ))
    assert transform.kind_chip.text() == "Transform · Polish"
    dictation = _card(TranscriptionHistory.create(
        text="Said.", model="base", source_name="Quick Record", entry_kind="dictation",
    ))
    assert not hasattr(dictation, "kind_chip")
    assert dictation.title_label.text() == "Quick Record"


def test_the_card_menu_undoes_and_redoes_the_ai_edit():
    from ui_qt.widgets.history_sidebar import HistorySidebar

    history = _history()
    entry = _cleaned()
    sidebar = HistorySidebar()
    card = _card(history.history_manager.get_entry_by_id(entry.id))
    card.version_requested.connect(sidebar._on_version_requested)
    actions = _menu(card)
    assert {"Copy", "Copy Raw", "Undo AI edit"} <= set(actions)
    assert "Use AI version" not in actions

    actions["Undo AI edit"].trigger()

    stored = history.history_manager.get_entry_by_id(entry.id)
    assert stored.text == "um hello world"
    original = _card(stored)
    assert original.cleanup_chip.text() == "Original"
    assert original.cleanup_chip.objectName() == "historyOriginalChip"
    original.version_requested.connect(sidebar._on_version_requested)
    _menu(original)["Use AI version"].trigger()
    assert history.history_manager.get_entry_by_id(entry.id).text == "Hello, world."


def test_entries_from_elsewhere_offer_no_undo():
    from ui_qt.widgets.history_sidebar import remote_history_entry

    remote = remote_history_entry({
        "id": "h1", "text": "Cleaned.", "raw_text": "cleaned", "timestamp": "2026-10-06T10:00:00+00:00",
        "model": "base", "stored_on": "devbox", "cleanup_level": 3, "entry_kind": "command",
    })
    assert remote.cleanup_level is None and remote.entry_kind == "command"
    assert "Undo AI edit" not in _menu(_card(remote))
    theirs = TranscriptionHistory.create(text="Cleaned.", model="base", raw_text="cleaned")
    theirs.origin_device_id = "laptop"
    assert "Undo AI edit" not in _menu(_card(theirs))


def test_the_dialog_toggles_original_and_ai_and_uses_the_shown_version():
    from ui_qt.dialogs.history_entry_dialog import HistoryEntryDialog

    history = _history()
    entry = _cleaned(cleanup_model="gpt-4o-mini", cleanup_provider="openai")
    dialog = HistoryEntryDialog(history.history_manager.get_entry_by_id(entry.id))
    changed = []
    dialog.version_changed.connect(changed.append)

    assert (dialog.raw_btn.text(), dialog.fixed_btn.text()) == ("Original", "AI")
    assert dialog.fixed_btn.isChecked()
    assert dialog.use_version_button.isHidden()

    dialog.raw_btn.click()
    assert dialog.transcript_text.toPlainText() == "um hello world"
    assert not dialog.use_version_button.isHidden()
    dialog.use_version_button.click()

    assert changed == [entry.id]
    assert history.history_manager.get_entry_by_id(entry.id).text == "um hello world"
    assert dialog.use_version_button.isHidden()
    assert dialog.cleanup_chip.text() == "Original"

    reopened = HistoryEntryDialog(history.history_manager.get_entry_by_id(entry.id))
    assert reopened.raw_btn.isChecked()
    assert reopened.transcript_text.toPlainText() == "um hello world"
    reopened.fixed_btn.click()
    assert reopened.transcript_text.toPlainText() == "Hello, world."
    reopened.use_version_button.click()
    assert history.history_manager.get_entry_by_id(entry.id).text == "Hello, world."


def test_a_legacy_entry_can_be_undone_from_the_dialog():
    from ui_qt.dialogs.history_entry_dialog import HistoryEntryDialog

    database = importlib.import_module("services.database").db
    database.add_history_entry(entry_id="legacy", text="Clean.", raw_text="clean um",
                               timestamp="2026-01-01T09:00:00", model="base",
                               source_name="Quick Record")
    dialog = HistoryEntryDialog(_history().history_manager.get_entry_by_id("legacy"))
    dialog.raw_btn.click()
    dialog.use_version_button.click()
    stored = _history().history_manager.get_entry_by_id("legacy")
    assert (stored.text, stored.cleaned_text) == ("clean um", "Clean.")


def test_a_host_kept_entry_can_be_viewed_but_not_changed():
    from ui_qt.dialogs.history_entry_dialog import HistoryEntryDialog
    from ui_qt.widgets.history_sidebar import remote_history_entry

    remote = remote_history_entry({
        "id": "h1", "text": "Cleaned.", "raw_text": "cleaned", "timestamp": "2026-10-06T10:00:00+00:00",
        "model": "base", "stored_on": "devbox",
    })
    dialog = HistoryEntryDialog(remote)
    dialog.raw_btn.click()
    assert dialog.transcript_text.toPlainText() == "cleaned"
    assert dialog.use_version_button.isHidden()
    assert dialog.play_button.isHidden()


def test_the_dialog_lists_the_app():
    from ui_qt.dialogs.history_entry_dialog import HistoryEntryDialog

    dialog = HistoryEntryDialog(TranscriptionHistory.create(
        text="Hi.", model="base", app_name="Outlook", audio_duration=2.0,
    ))
    assert dialog.fact_labels["App"].text() == "Outlook"


def test_a_card_plays_and_stops_its_recording(tmp_path, device):
    entry = _recorded(tmp_path)
    card = _card(entry)
    button = card.playback.button
    assert button.isEnabled() and button.text() == "Play"
    assert card.playback.progress.isHidden()

    button.click()
    assert player_tests._wait(lambda: device.streams and device.streams[0].started)
    assert card.playback.is_playing and button.text() == "Stop"
    assert not card.playback.progress.isHidden()
    device.streams[0].pull(5512)
    card.playback._sync()
    assert card.playback.progress.fraction == pytest.approx(0.5, abs=0.01)
    assert card.playback.time_label.text() == "0:00 / 0:00"

    # A refresh rebuilds the card; the new one shows the same playback.
    rebuilt = _card(_history().history_manager.get_entry_by_id(entry.id))
    assert rebuilt.playback.is_playing and rebuilt.playback.button.text() == "Stop"

    button.click()
    assert not card.playback.is_playing and button.text() == "Play"
    assert device.streams[0].aborted
    QApplication.processEvents()


def test_playing_to_the_end_resets_the_card(tmp_path, device):
    card = _card(_recorded(tmp_path))
    card.playback.button.click()
    assert player_tests._wait(lambda: device.streams and device.streams[0].started)
    while device.streams[0].pull(4096):
        pass
    card.playback._sync()
    assert card.playback.button.text() == "Play" and card.playback.progress.isHidden()


def test_playback_is_refused_while_recording_or_in_a_meeting(tmp_path, device):
    playback = _playback()

    class Window(QWidget):
        is_recording = True

        def meeting_is_active(self):
            return False

    window = Window()
    card = _card(_recorded(tmp_path), parent=window)
    card.playback.button.click()
    assert card.playback.note == playback.RECORDING_BUSY
    assert device.streams == [] and not card.playback.is_playing

    window.is_recording = False
    window.meeting_is_active = lambda: True
    card.playback.button.click()
    assert card.playback.note == playback.MEETING_BUSY
    assert device.streams == []


def test_a_removed_recording_shows_a_disabled_play_with_the_reason(tmp_path, device):
    from ui_qt.dialogs.history_entry_dialog import HistoryEntryDialog

    entry = TranscriptionHistory.create(text="old", model="base", audio_file="recording_gone.wav")
    card = _card(entry)
    assert card.playback is None
    dialog = HistoryEntryDialog(entry)
    assert not dialog.play_button.isHidden()
    assert not dialog.play_button.isEnabled()
    assert dialog.play_button.toolTip() == _playback().RECORDING_REMOVED


def test_closing_the_dialog_stops_its_playback(tmp_path, device):
    from ui_qt.dialogs.history_entry_dialog import HistoryEntryDialog

    dialog = HistoryEntryDialog(_recorded(tmp_path))
    dialog.play_button.click()
    assert player_tests._wait(lambda: device.streams and device.streams[0].started)
    assert dialog.playback.is_playing
    dialog.reject()
    assert not dialog.playback.is_playing and device.streams[0].aborted


def test_a_host_kept_recording_is_fetched_before_it_plays(tmp_path, device, quiet_sync):
    from ui_qt.widgets.history_sidebar import remote_history_entry

    quiet_sync.audio = player_tests._wav(tmp_path / "cached.wav")
    remote = remote_history_entry({
        "id": "h1", "text": "On the host.", "timestamp": "2026-10-06T10:00:00+00:00",
        "model": "base", "stored_on": "devbox", "has_audio": True, "file_size": 22050,
    })
    card = _card(remote)
    card.playback.button.click()
    assert player_tests._wait(lambda: (QApplication.processEvents() or True) and bool(device.streams))
    assert player_tests._wait(lambda: device.streams[0].started)
    assert card.playback.is_playing
    assert _playback().audio_player.player().path == quiet_sync.audio


def test_collapsing_history_stops_playback(tmp_path, device):
    from ui_qt.widgets.history_sidebar import HistorySidebar

    sidebar = HistorySidebar()
    sidebar.expand()
    card = _card(_recorded(tmp_path))
    card.playback.button.click()
    assert player_tests._wait(lambda: device.streams and device.streams[0].started)
    sidebar.collapse()
    assert not card.playback.is_playing
    sidebar.deleteLater()


def test_chips_wrap_instead_of_eliding_to_nothing():
    entry = TranscriptionHistory.create(
        text="Polished.", model="base", source_name="Polish", entry_kind="transform",
        raw_text="draft", cleanup_model="gpt-4o-mini", cleanup_level="medium",
        app_name="Google Docs — Q3 planning notes",
    )
    card = _card(entry)
    flow = card.chips
    line = flow.minimumSizeHint().height()
    assert flow.heightForWidth(2000) == line
    assert flow.heightForWidth(200) > line
    card.setFixedWidth(240)
    card.show()
    QApplication.processEvents()
    width = flow.width()
    assert width < 240 and flow.height() > line
    for chip in (card.kind_chip, card.cleanup_chip, card.app_chip):
        assert chip.geometry().right() < width
        assert chip.width() == min(chip.sizeHint().width(), width)
