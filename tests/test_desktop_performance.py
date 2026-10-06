"""Behavior and work-reduction contracts for deferred desktop rendering."""
import json
import random
import threading
import time
from unittest.mock import patch

import pytest
from PyQt6 import sip
from PyQt6.QtCore import QCoreApplication, QRect, QTimer
from PyQt6.QtGui import QImage, QPainter
from PyQt6.QtWidgets import QApplication

from services.models import TranscriptionHistory
from services.settings import SettingsManager
from ui_qt.dialogs.settings_dialog import OVERVIEW, RECORDING, SettingsDialog
from ui_qt.waveform_styles.particle_style import ParticleStyle
from ui_qt.widgets.history_sidebar import HistorySidebar, HistoryItemWidget
from ui_qt.widgets.past_meetings_panel import PastMeetingsPanel, PastMeetingItem


def _pump_until(predicate):
    deadline = time.monotonic() + 2.0
    while not predicate() and time.monotonic() < deadline:
        QCoreApplication.processEvents()
        threading.Event().wait(0.001)
    assert predicate()


def test_settings_read_cache_isolated_nested_values_and_external_changes(tmp_path):
    target = tmp_path / "settings.json"
    manager = SettingsManager(str(target))
    manager.save_all_settings({"nested": {"items": [1]}})
    real_open = open
    reads = []
    def tracked_open(path, mode="r", *args, **kwargs):
        if str(path) == str(target) and mode == "r":
            reads.append(path)
        return real_open(path, mode, *args, **kwargs)
    with patch("builtins.open", tracked_open):
        loaded = manager.load_all_settings()
        loaded["nested"]["items"].append(2)
        assert manager.load_all_settings() == {"nested": {"items": [1]}}
        assert len(reads) == 1
        other = tmp_path / "replacement.json"
        other.write_text(json.dumps({"external": True}), encoding="utf-8")
        other.replace(target)
        assert manager.load_all_settings() == {"external": True}
        target.write_text("malformed", encoding="utf-8")
        assert manager.load_all_settings() == {}
        with pytest.raises(json.JSONDecodeError):
            manager.save_setting("must_not_replace_bad_data", True)
        assert target.read_text(encoding="utf-8") == "malformed"
        target.unlink()
        assert manager.load_all_settings() == {}


def test_settings_first_show_and_search_do_not_build_hidden_destinations(monkeypatch):
    from services.settings import SettingsKey, SettingsView, settings_manager

    settings_manager.save_setting(SettingsKey.SETTINGS_VIEW, SettingsView.ADVANCED)
    monkeypatch.setattr("ui_qt.dialogs.settings_models.scan_cached_models", lambda **_kwargs: {})
    monkeypatch.setattr("ui_qt.dialogs.settings_downloads.scan_cached_models", lambda **_kwargs: {})
    monkeypatch.setattr("services.local_asr.cache.inventory", lambda: {})
    def unexpected_devices():
        pytest.fail("Overview must not query an audio driver")
    monkeypatch.setattr("ui_qt.dialogs.settings_dialog.AudioRecorder.get_input_devices", unexpected_devices)
    dialog = SettingsDialog(background_cache_scan=False)
    dialog.show()
    QApplication.instance().processEvents()
    assert dialog._built_pages == {OVERVIEW}
    assert not dialog.downloads._ui_built
    entries = dialog._search_index()
    assert any(entry.title == "Custom prompt (optional)" for entry in entries)
    assert any(entry.model_name == "base" for entry in entries)
    assert dialog._built_pages == {OVERVIEW}
    assert not dialog.downloads._ui_built
    assert len(dialog.findChildren(HistoryItemWidget)) == 0


def test_recording_driver_result_preserves_selection_and_gui_progress(monkeypatch):
    from services.audio_devices import InputDevice
    from services.settings import SettingsKey, settings_manager
    from ui_qt.dialogs.settings_microphones import entry_token

    entered = threading.Event()
    release = threading.Event()
    usb = InputDevice(index=7, name="USB microphone", hostapi="MME", default_api=True)
    desk, lapel = ({"name": name, "hostapi": "MME"} for name in ("Desk microphone", "Lapel microphone"))
    settings_manager.save_setting(SettingsKey.AUDIO_INPUT_PRIORITY, [desk, lapel])
    def slow_devices():
        entered.set()
        assert release.wait(2)
        return [usb]
    monkeypatch.setattr("ui_qt.dialogs.settings_dialog.AudioRecorder.get_input_devices", slow_devices)
    monkeypatch.setattr("ui_qt.dialogs.settings_models.scan_cached_models", lambda **_kwargs: {})
    monkeypatch.setattr("ui_qt.dialogs.settings_downloads.scan_cached_models", lambda **_kwargs: {})
    monkeypatch.setattr("services.local_asr.cache.inventory", lambda: {})
    dialog = SettingsDialog(background_cache_scan=False)
    try:
        dialog.select_destination(RECORDING)
        assert entered.wait(1)
        combo = dialog.audio_device_combo
        combo.setCurrentIndex(combo.findData(entry_token(lapel)))
        ticks = []
        QTimer.singleShot(0, lambda: ticks.append(True))
        QApplication.instance().processEvents()
        assert ticks == [True]
        assert not release.is_set()
        release.set()
        _pump_until(lambda: combo.findData(entry_token(usb.key)) >= 0)
        assert combo.currentData() == entry_token(lapel)
    finally:
        release.set()


def test_initializing_another_settings_page_preserves_unfinished_prompt(monkeypatch):
    from ui_qt.dialogs.settings_destinations import CLEANUP, GENERAL
    from services.settings import SettingsKey, settings_manager

    monkeypatch.setattr("ui_qt.dialogs.settings_models.scan_cached_models", lambda **_: {})
    monkeypatch.setattr("ui_qt.dialogs.settings_downloads.scan_cached_models", lambda **_: {})
    monkeypatch.setattr("services.local_asr.cache.inventory", lambda: {})
    settings_manager.save_setting(SettingsKey.TRANSCRIPT_CLEANUP_PROMPT, "Saved prompt")
    dialog = SettingsDialog(background_cache_scan=False)
    dialog.ensure_page(CLEANUP)
    dialog.cleanup_prompt_edit.setPlainText("Unfinished prompt draft")
    dialog.ensure_page(GENERAL)
    assert dialog.cleanup_prompt_edit.toPlainText() == "Unfinished prompt draft"
    assert settings_manager.get(SettingsKey.TRANSCRIPT_CLEANUP_PROMPT) == "Saved prompt"


def test_history_refresh_reuses_unchanged_cards_and_updates_one_changed_entry():
    sidebar = HistorySidebar()
    entries = [TranscriptionHistory.create(text=f"Entry {i}", model="local_whisper") for i in range(3)]
    sidebar._history_load_generation = 1
    sidebar._apply_history_results(1, "", entries, "")
    first = [sidebar._history_cards[entry.id][1] for entry in entries]
    clones = [TranscriptionHistory(**{key: value for key, value in vars(entry).items() if not key.startswith("_")}) for entry in entries]
    sidebar._apply_history_results(1, "", clones, "")
    assert [sidebar._history_cards[entry.id][1] for entry in entries] == first
    changed = clones[1]
    changed.text = "Corrected transcription"
    sidebar._apply_history_results(1, "", [entries[0], changed, entries[2]], "")
    assert sidebar._history_cards[entries[0].id][1] is first[0]
    assert sidebar._history_cards[entries[1].id][1] is not first[1]
    assert sidebar._history_cards[entries[2].id][1] is first[2]
    assert sidebar.history_list_layout.count() == 3


def test_delayed_history_query_can_finish_after_qt_owner_is_deleted(monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    def delayed_query(_limit=None, **_kwargs):
        entered.set()
        assert release.wait(2)
        return []
    monkeypatch.setattr("ui_qt.widgets.history_sidebar.history_manager.get_history", delayed_query)
    sidebar = HistorySidebar()
    sidebar._load_history()
    assert entered.wait(1)
    sip.delete(sidebar)
    release.set()
    for worker in threading.enumerate():
        if worker.name == "history-sidebar-load":
            worker.join(2)
            assert not worker.is_alive()


def test_meeting_refresh_reuses_cards_and_preserves_selected_card():
    meetings = [{"id": str(i), "title": f"Meeting {i}", "status": "ended", "content_summary": {"has_transcript": True}} for i in range(3)]
    panel = PastMeetingsPanel(meeting_provider=lambda: meetings)
    panel.refresh()
    cards = {key: item[1] for key, item in panel._meeting_cards.items()}
    panel.set_selected_meeting_id("1")
    panel.refresh()
    assert {key: item[1] for key, item in panel._meeting_cards.items()} == cards
    assert panel._meeting_cards["1"][1].property("selected") is True
    meetings[0]["title"] = "Changed title"
    panel.refresh()
    assert panel._meeting_cards["0"][1] is not cards["0"]
    assert panel._meeting_cards["1"][1] is cards["1"]
    assert len(panel.findChildren(PastMeetingItem)) == 3


def test_particle_paints_do_not_advance_any_simulation_state():
    image = QImage(320, 100, QImage.Format.Format_ARGB32)
    painter = QPainter(image)
    try:
        for state in ("recording", "processing", "transcribing", "canceling"):
            style = ParticleStyle(320, 100, {})
            style.advance(state, 1 / 30)
            before = ([dict(vars(p)) for p in style.particles],
                      [dict(vars(p)) for p in style.cancel_particles], style.animation_time)
            draw = getattr(style, f"draw_{state}_state")
            for _ in range(3):
                draw(painter, QRect(0, 0, 320, 100))
            assert before == ([dict(vars(p)) for p in style.particles],
                              [dict(vars(p)) for p in style.cancel_particles], style.animation_time)
    finally:
        painter.end()


def test_equal_elapsed_particle_ticks_match_across_frame_rates_and_clamp_stalls():
    outcomes = []
    for fps in (30, 60):
        random.seed(123)
        style = ParticleStyle(320, 100, {"max_particles": 1000})
        style.update_audio_levels([0.5])
        for _ in range(fps):
            style.advance("recording", 1 / fps)
        outcomes.append([dict(vars(p)) for p in style.particles])
    assert outcomes[0] == outcomes[1]
    before = style.animation_time
    style.advance("recording", 100.0)
    assert style.animation_time - before == pytest.approx(0.1)
