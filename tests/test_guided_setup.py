"""Optional onboarding: eligibility, existing save paths, privacy and layout."""

from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QSignalBlocker, Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QAbstractButton, QApplication, QComboBox, QLineEdit

from services import guided_setup
from services.audio_devices import InputDevice
from services.settings import SettingsKey, SettingsManager, SettingsView
from ui_qt.dialogs import guided_setup as setup_ui
from ui_qt.dialogs import settings_dialog, settings_downloads, settings_models
from ui_qt.dialogs.settings_destinations import CLEANUP, GENERAL
from ui_qt.dialogs.settings_microphones import entry_token


@pytest.mark.parametrize("other_writes_first", [False, True])
def test_new_user_invitation_survives_first_launch_writes_and_restart(
    tmp_path, other_writes_first
):
    store = SettingsManager(str(tmp_path / "settings.json"))
    if other_writes_first:
        store.save_setting(SettingsKey.APP_STYLE_TONES, {"chat": "casual"})
    guided_setup.initialize(store)
    assert guided_setup.should_invite(store)
    store.save_setting(SettingsKey.SELECTED_MODEL, "parakeet")
    reopened = SettingsManager(store.settings_file)
    guided_setup.initialize(reopened)
    assert guided_setup.should_invite(reopened)
    assert reopened.get(SettingsKey.SELECTED_MODEL) == "parakeet"
    if other_writes_first:
        assert reopened.get(SettingsKey.APP_STYLE_TONES) == {"chat": "casual"}


def test_established_user_with_default_settings_is_not_new(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{}", encoding="utf-8")
    store = SettingsManager(str(path))
    guided_setup.initialize(store)
    assert store.get(SettingsKey.GUIDED_SETUP_STATE) == guided_setup.EXISTING
    assert not guided_setup.should_invite(store)


@pytest.mark.parametrize(
    "artifact",
    [
        "openwhisper.db",
        "transcription_history.json",
        "scratchpad.txt",
        "recordings/old.wav",
        "meetings/old.json",
    ],
)
def test_missing_settings_with_prior_data_does_not_invite(tmp_path, artifact):
    path = tmp_path / artifact
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("existing data", encoding="utf-8")
    store = SettingsManager(str(tmp_path / "settings.json"))
    guided_setup.initialize(store)
    assert not guided_setup.should_invite(store)
    assert path.read_text(encoding="utf-8") == "existing data"


def test_corrupt_existing_settings_are_neither_overwritten_nor_invited(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("broken JSON", encoding="utf-8")
    store = SettingsManager(str(path))
    guided_setup.initialize(store)
    assert not guided_setup.should_invite(store)
    assert path.read_text(encoding="utf-8") == "broken JSON"


def test_manager_repointed_to_another_file_is_not_new(tmp_path):
    store = SettingsManager(str(tmp_path / "first.json"))
    store.settings_file = str(tmp_path / "other.json")
    guided_setup.initialize(store)
    assert not guided_setup.should_invite(store)


@pytest.fixture
def make_setup(tmp_path, monkeypatch):
    store = SettingsManager(str(tmp_path / "settings.json"))
    for module in (
        settings_dialog,
        settings_models,
        settings_downloads,
        setup_ui,
        guided_setup,
    ):
        monkeypatch.setattr(module, "settings_manager", store)
    monkeypatch.setattr(settings_dialog.history_manager, "set_retention", MagicMock())
    for module in (settings_models, settings_downloads):
        monkeypatch.setattr(module, "scan_cached_models", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        "services.local_asr.cache.inventory", lambda *args, **kwargs: {}
    )
    monkeypatch.setattr(settings_dialog.AudioRecorder, "get_input_devices", lambda: [])
    instances = []

    def build(values=None):
        store.save_all_settings(
            {SettingsKey.SETTINGS_VIEW: SettingsView.ADVANCED, **(values or {})}
        )
        settings = settings_dialog.SettingsDialog(background_cache_scan=False)
        destination, all_settings = MagicMock(), MagicMock()
        setup = setup_ui.GuidedSetupDialog(
            settings,
            open_destination=destination,
            open_all_settings=all_settings,
            manager=store,
        )
        instances.append((setup, settings))
        return setup, settings, store

    yield build
    for setup, settings in instances:
        setup.hide()
        settings.hide()


@pytest.mark.parametrize("dismiss", ["skip", "close", "escape"])
def test_invitation_dismissal_is_durable_and_preserves_preferences(tmp_path, dismiss):
    store = SettingsManager(str(tmp_path / "settings.json"))
    values = {
        SettingsKey.TRANSCRIPT_CLEANUP_PROMPT: "My exact prompt",
        SettingsKey.AUTO_PASTE: False,
    }
    store.save_all_settings(values)
    guided_setup.initialize(store)
    invitation = setup_ui.GuidedSetupInvitation(manager=store)
    invitation.show()
    if dismiss == "skip":
        invitation.skip_button.click()
    elif dismiss == "close":
        invitation.close()
    else:
        QTest.keyClick(invitation, Qt.Key.Key_Escape)
    assert not invitation.isVisible()
    assert store.load_all_settings() == {
        **values,
        SettingsKey.GUIDED_SETUP_STATE: guided_setup.SKIPPED,
    }
    assert not guided_setup.should_invite(SettingsManager(store.settings_file))


def test_invitation_start_only_records_progress(tmp_path):
    store = SettingsManager(str(tmp_path / "settings.json"))
    invitation = setup_ui.GuidedSetupInvitation(manager=store)
    started = MagicMock()
    invitation.start_requested.connect(started)
    invitation.start_button.click()
    started.assert_called_once_with()
    assert store.load_all_settings() == {
        SettingsKey.GUIDED_SETUP_STATE: guided_setup.STARTED
    }


def test_failed_dismissal_keeps_invitation_reviewable_for_retry(tmp_path):
    store = SettingsManager(str(tmp_path / "settings.json"))
    invitation = setup_ui.GuidedSetupInvitation(manager=store)
    invitation.show()
    with patch.object(store, "save_setting", side_effect=OSError("Disk full")):
        invitation.skip_button.click()
    assert invitation.isVisible()
    assert "Disk full" in invitation.error.text()
    invitation.skip_button.click()
    assert not invitation.isVisible()


def test_open_and_continue_preserve_all_choices_and_custom_prompt(make_setup):
    setup, settings, store = make_setup(
        {
            SettingsKey.TRANSCRIPT_CLEANUP_ENABLED: False,
            SettingsKey.TRANSCRIPT_CLEANUP_PROMPT: "Keep my exact wording",
            SettingsKey.TRANSCRIPT_CLEANUP_PROVIDER: "openai",
            SettingsKey.TRANSCRIPT_CLEANUP_MODEL: "gpt-4o-mini",
            SettingsKey.AUTO_PASTE: False,
            SettingsKey.STREAMING_ENABLED: False,
            SettingsKey.HOTKEYS: {"record_toggle": "ctrl+alt+j"},
        }
    )
    before = store.load_all_settings()
    setup.next_button.click()
    assert setup.step == 1
    assert setup.cleanup_options.isHidden()
    assert setup.cleanup_off_note.isVisibleTo(setup.pages.widget(1))
    setup.next_button.click()
    assert setup.step == 2
    assert store.load_all_settings() == before
    assert CLEANUP not in settings._built_pages
    assert GENERAL not in settings._built_pages
    setup.next_button.click()
    assert store.load_all_settings() == {
        **before,
        SettingsKey.GUIDED_SETUP_STATE: guided_setup.COMPLETED,
    }
    assert not guided_setup.should_invite(SettingsManager(store.settings_file))


def test_cleanup_toggle_uses_existing_callback_and_hides_dependents(make_setup):
    setup, settings, store = make_setup()
    settings.on_cleanup_changed = MagicMock()
    setup.set_step(1)
    setup.cleanup_switch.click()
    assert store.get(SettingsKey.TRANSCRIPT_CLEANUP_ENABLED) is True
    assert not setup.cleanup_options.isHidden()
    settings.on_cleanup_changed.assert_called_once_with()
    setup.cleanup_switch.click()
    assert store.get(SettingsKey.TRANSCRIPT_CLEANUP_ENABLED) is False
    assert setup.cleanup_options.isHidden()
    assert settings.on_cleanup_changed.call_count == 2


def test_custom_prompt_confirmation_is_reused_and_declining_keeps_it(make_setup):
    setup, settings, store = make_setup(
        {
            SettingsKey.TRANSCRIPT_CLEANUP_ENABLED: True,
            SettingsKey.TRANSCRIPT_CLEANUP_PROMPT: "Use my exact prompt",
        }
    )
    setup.set_step(1)
    before = store.load_all_settings()
    assert not any(button.isChecked() for button in setup.levels.buttons)
    assert setup.preview.isHidden()
    with patch.object(
        settings, "_confirm_custom_prompt_removal", return_value=False
    ) as confirm:
        setup.levels.buttons[0].click()
        QApplication.processEvents()
    confirm.assert_called_once_with("light")
    assert store.load_all_settings() == before
    assert not any(button.isChecked() for button in setup.levels.buttons)
    with patch.object(
        settings, "_confirm_custom_prompt_removal", return_value=True
    ) as confirm:
        setup.levels.buttons[1].click()
        QApplication.processEvents()
    confirm.assert_called_once_with("medium")
    assert store.get(SettingsKey.TRANSCRIPT_CLEANUP_PROMPT) is None
    assert store.get(SettingsKey.TRANSCRIPT_CLEANUP_LEVEL) == "medium"
    assert setup.levels.buttons[1].isChecked()


def test_failed_save_restores_projection_and_source_for_retry(make_setup):
    setup, settings, store = make_setup({SettingsKey.AUTO_PASTE: False})
    setup.set_step(2)
    with patch.object(store, "save_setting", side_effect=OSError("Disk full")):
        setup.paste_switch.click()
    assert not setup.paste_switch.isChecked()
    assert not settings.auto_paste_check.isChecked()
    assert "Disk full" in setup.error.text()
    setup.paste_switch.click()
    assert setup.paste_switch.isChecked()
    assert store.get(SettingsKey.AUTO_PASTE) is True
    assert setup.error.isHidden()


def test_microphone_keeps_backups_and_invokes_live_handler(make_setup):
    setup, settings, store = make_setup()
    desk = InputDevice(
        index=11, name="Desk microphone", hostapi="MME", default_api=True
    )
    backup = InputDevice(
        index=12, name="Backup microphone", hostapi="MME", default_api=True
    )
    QTest.qWait(
        15
    )  # Finish the constructor's empty discovery before injecting devices.
    store.save_setting(SettingsKey.AUDIO_INPUT_PRIORITY, [backup.key])
    settings._microphones.load(store.load_all_settings())
    settings._apply_audio_devices(
        settings._audio_device_generation, "", [desk, backup], ""
    )
    QApplication.processEvents()
    setup.refresh()
    settings.on_audio_device_changed = MagicMock()
    setup.microphone.setCurrentIndex(setup.microphone.findData(entry_token(desk.key)))
    assert store.get(SettingsKey.AUDIO_INPUT_PRIORITY) == [desk.key, backup.key]
    settings.on_audio_device_changed.assert_called_once_with([desk.key, backup.key])


def test_shortcut_conflict_and_capture_cancellation_use_existing_path(make_setup):
    setup, settings, store = make_setup()
    settings.on_profile_hotkey_capture = MagicMock()
    before = store.load_hotkey_settings()
    setup.shortcut.begin_capture()
    setup.set_step(1)
    assert not setup.shortcut._capturing
    assert settings.on_profile_hotkey_capture.call_args_list[0].args == (True,)
    assert settings.on_profile_hotkey_capture.call_args_list[-1].args == (False,)
    with patch.object(
        settings,
        "_standard_hotkey_conflict",
        return_value="That shortcut is already used by Cancel",
    ):
        setup._shortcut_captured("esc")
    assert store.load_hotkey_settings() == before
    assert "already used" in setup.error.text()
    setup._shortcut_captured("ctrl+alt+j")
    assert store.load_hotkey_settings()["record_toggle"] == "ctrl+alt+j"


@pytest.mark.parametrize(
    "provider,url,local",
    [
        ("openai", None, False),
        ("openrouter", None, False),
        ("ollama", "http://localhost:11434/v1", True),
        ("ollama", "http://192.168.1.10:11434/v1", False),
        ("custom_company", "https://example.com/v1", False),
    ],
)
def test_privacy_follows_actual_endpoint_and_never_claims_connected(
    provider, url, local
):
    values = {
        SettingsKey.TRANSCRIPT_CLEANUP_PROVIDER: provider,
        SettingsKey.TRANSCRIPT_CLEANUP_MODEL: "my-model",
    }
    if provider == "ollama":
        values["ollama_base_url"] = url
    if provider.startswith("custom"):
        values[SettingsKey.TEXT_LLM_PROFILES] = [
            {
                "id": provider,
                "name": "Company endpoint",
                "kind": "custom",
                "base_url": url,
                "api_key_env": "COMPANY_KEY",
            }
        ]
    with patch.object(setup_ui, "resolve_credential", return_value="key"):
        name, status, privacy, remote = setup_ui.provider_summary(values)
    assert remote is not local
    assert name in privacy
    assert ("on this computer" in privacy) is local
    assert "No audio" in privacy
    assert "Connected" not in status
    assert "Connection not tested" in status


def test_missing_key_is_shown_without_network_probe():
    with patch.object(setup_ui, "resolve_credential", return_value=""):
        _, status, _, _ = setup_ui.provider_summary({})
    assert status == "API key needed"


def test_output_uses_existing_streaming_migration_and_callback(make_setup):
    setup, settings, store = make_setup(
        {
            SettingsKey.STREAMING_ENABLED: False,
            SettingsKey.STREAMING_OVERLAY_ENABLED: True,
        }
    )
    settings.on_streaming_settings_changed = MagicMock()
    setup.set_step(2)
    setup.live_switch.click()
    assert store.get(SettingsKey.STREAMING_ENABLED) is True
    assert SettingsKey.STREAMING_OVERLAY_ENABLED not in store.load_all_settings()
    settings.on_streaming_settings_changed.assert_called_once_with()


def test_all_settings_and_change_links_use_stable_destinations(make_setup):
    setup, settings, store = make_setup()
    setup._open(CLEANUP)
    setup.open_destination.assert_called_once_with(CLEANUP)
    assert setup.step == 0
    setup.all_settings_button.click()
    setup.open_all_settings.assert_called_once_with()
    assert store.get(SettingsKey.GUIDED_SETUP_STATE) == guided_setup.SKIPPED


def test_reactivation_projects_changes_from_all_settings(make_setup):
    setup, settings, store = make_setup()
    store.update_settings(
        {
            SettingsKey.TRANSCRIPT_CLEANUP_ENABLED: True,
            SettingsKey.AUTO_PASTE: False,
            SettingsKey.TRANSCRIPT_CLEANUP_LEVEL: "high",
        }
    )
    setup.refresh()
    assert setup.cleanup_switch.isChecked()
    assert not setup.paste_switch.isChecked()
    assert setup.levels.buttons[2].isChecked()


@pytest.mark.parametrize("width,scale", [(920, 100), (620, 130)])
def test_all_steps_fit_narrow_and_large_font_layouts(make_setup, width, scale):
    from ui_qt.utils.font_scale import (
        apply_ui_font_scale,
        current_ui_font_scale_percent,
    )
    from ui_qt.utils.theme_manager import ThemeManager

    app = QApplication.instance()
    previous_style, previous_font = app.styleSheet(), app.font()
    previous_scale = current_ui_font_scale_percent()
    try:
        apply_ui_font_scale(scale, app=app, theme_manager=ThemeManager())
        setup, _, _ = make_setup({SettingsKey.TRANSCRIPT_CLEANUP_ENABLED: True})
        setup.show()
        setup.resize(width, 540)
        for step in range(3):
            setup.set_step(step)
            QTest.qWait(30)
            assert setup.scroll.horizontalScrollBar().maximum() == 0
            assert setup.next_button.isVisible()
            assert setup.next_button.geometry().bottom() < setup.height()
            viewport = setup.scroll.viewport()
            for control in setup.pages.currentWidget().findChildren(
                (QAbstractButton, QComboBox, QLineEdit)
            ):
                if control.isVisible():
                    left = control.mapTo(viewport, control.rect().topLeft()).x()
                    assert left >= 0
                    assert left + control.width() <= viewport.width()
    finally:
        apply_ui_font_scale(previous_scale, app=app, theme_manager=ThemeManager())
        app.setStyleSheet(previous_style)
        app.setFont(previous_font)


def test_stale_microphone_projection_does_not_reset_to_system_default(make_setup):
    setup, settings, store = make_setup()
    QTest.qWait(15)
    unavailable = {"name": "Saved microphone", "hostapi": "MME"}
    store.save_setting(SettingsKey.AUDIO_INPUT_PRIORITY, [unavailable])
    with QSignalBlocker(setup.microphone):
        setup.microphone.addItem("No longer in inventory", "stale-token")
        setup.microphone.setCurrentIndex(setup.microphone.count() - 1)
    setup._choose_microphone(0)
    assert store.get(SettingsKey.AUDIO_INPUT_PRIORITY) == [unavailable]


def test_controller_reopens_singleton_without_changing_dictation_choices(make_setup):
    from types import SimpleNamespace

    from ui_qt.ui_controller import UIController

    setup, settings, store = make_setup({SettingsKey.AUTO_PASTE: False})
    ui = SimpleNamespace(
        _guided_setup_offer_timer=MagicMock(),
        _guided_setup_dialog=setup,
        _guided_setup_invitation=None,
        _raise_dialog=MagicMock(side_effect=lambda dialog: dialog.show()),
    )
    UIController.open_guided_setup(ui)
    assert setup.step == 0
    setup.set_step(2)
    setup.next_button.click()
    assert store.get(SettingsKey.GUIDED_SETUP_STATE) == guided_setup.COMPLETED
    UIController.open_guided_setup(ui)
    assert ui._guided_setup_dialog is setup
    assert setup.step == 0
    assert store.get(SettingsKey.AUTO_PASTE) is False
    assert not guided_setup.should_invite(store)


def test_invitation_waits_for_startup_modal_and_never_reappears_after_skip(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    from ui_qt.ui_controller import UIController

    store = SettingsManager(str(tmp_path / "settings.json"))
    guided_setup.initialize(store)
    monkeypatch.setattr(guided_setup, "settings_manager", store)
    ui = SimpleNamespace(
        _guided_setup_invitation=None,
        _guided_setup_offer_timer=MagicMock(),
        main_window=MagicMock(),
        _raise_dialog=MagicMock(),
    )
    ui.main_window.isVisible.return_value = True
    ui.main_window.isMinimized.return_value = False
    with patch.object(QApplication, "activeModalWidget", return_value=object()):
        UIController._show_guided_setup_invitation(ui)
    ui._guided_setup_offer_timer.start.assert_called_once_with(1000)
    ui._raise_dialog.assert_not_called()
    guided_setup.save_state(guided_setup.SKIPPED, store)
    with patch.object(QApplication, "activeModalWidget", return_value=None):
        UIController._show_guided_setup_invitation(ui)
    ui._raise_dialog.assert_not_called()


def test_failed_cleanup_save_restores_source_gates(make_setup):
    setup, settings, store = make_setup({SettingsKey.TRANSCRIPT_CLEANUP_ENABLED: False})
    setup.set_step(1)
    with patch.object(store, "save_setting", side_effect=OSError("Disk full")):
        setup.cleanup_switch.click()
    assert not setup.cleanup_switch.isChecked()
    assert setup.cleanup_options.isHidden()
    assert not settings.transcript_cleanup_check.isChecked()
    assert not settings.cleanup_level_tile.isEnabled()
    setup.cleanup_switch.click()
    assert settings.cleanup_level_tile.isEnabled()


def test_privacy_is_visible_beside_cleanup_at_default_size(make_setup):
    setup, _, _ = make_setup({SettingsKey.TRANSCRIPT_CLEANUP_ENABLED: True})
    setup.show()
    setup.resize(920, 850)
    setup.set_step(1)
    QTest.qWait(30)
    viewport = setup.scroll.viewport()
    top = setup.privacy.mapTo(viewport, setup.privacy.rect().topLeft()).y()
    assert top >= 0
    assert top + setup.privacy.height() <= viewport.height()
    assert "Transcript text is sent to" in setup.privacy.text()


def test_recording_shortcut_keyboard_capture_saves_and_keeps_audio_step(make_setup):
    setup, settings, store = make_setup()
    setup.show()
    setup._capture_shortcut()
    QTest.qWait(20)
    QTest.keyClick(
        setup.shortcut,
        Qt.Key.Key_J,
        Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier,
    )
    assert not setup.shortcut._capturing
    assert store.load_hotkey_settings()["record_toggle"] == "ctrl+alt+j"
    assert setup.step == 0


def test_enabling_cleanup_reveals_privacy_in_small_window(make_setup):
    setup, _, _ = make_setup({SettingsKey.TRANSCRIPT_CLEANUP_ENABLED: False})
    setup.show()
    setup.resize(620, 460)
    setup.set_step(1)
    setup.cleanup_switch.click()
    QTest.qWait(40)
    viewport = setup.scroll.viewport()
    top = setup.privacy.mapTo(viewport, setup.privacy.rect().topLeft()).y()
    assert top >= 0
    assert top + setup.privacy.height() <= viewport.height()
