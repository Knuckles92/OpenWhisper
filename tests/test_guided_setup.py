"""Optional onboarding: eligibility, existing save paths, privacy and layout."""

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QSignalBlocker, Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QAbstractButton, QApplication, QComboBox, QLineEdit

from services import guided_setup
from services.audio_devices import InputDevice
from services.hotkey_manager import format_hotkey_display
from services.settings import SettingsKey, SettingsManager
from ui_qt.dialogs import guided_setup as setup_ui
from ui_qt.dialogs import settings_dialog, settings_downloads, settings_models
from ui_qt.dialogs.settings_destinations import (
    API_KEYS,
    CLEANUP,
    GENERAL,
    VOICE_MODEL,
)
from ui_qt.dialogs.settings_microphones import entry_token
from ui_qt.ui_controller import UIController


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


@pytest.mark.parametrize(
    "settings,expected",
    [
        ({SettingsKey.SELECTED_MODEL: "api"}, None),
        ({SettingsKey.SELECTED_MODEL: "remote"}, None),
    ],
)
def test_speech_status_is_unknown_for_engines_without_a_local_model(settings, expected):
    assert guided_setup.speech_model_downloaded(settings) is expected


def test_speech_status_counts_a_missing_runtime_as_not_downloaded():
    with patch("services.local_asr.catalog.missing_runtime", return_value="asr-nvidia-cpu"), patch(
        "services.hf_access.is_model_cached", return_value=True
    ):
        assert guided_setup.speech_model_downloaded({SettingsKey.SELECTED_MODEL: "parakeet"}) is False


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
    monkeypatch.setattr("services.local_asr.cache.inventory", lambda *args, **kwargs: {})
    monkeypatch.setattr(settings_dialog.AudioRecorder, "get_input_devices", lambda: [])
    speech = {"ready": True}
    monkeypatch.setattr(guided_setup, "speech_model_downloaded", lambda settings: speech["ready"])
    instances = []

    def build(values=None, *, welcome=False):
        store.save_all_settings({**(values or {})})
        built = []

        def get_settings():
            built.append(settings_dialog.SettingsDialog(background_cache_scan=False))
            return built[-1]

        setup = setup_ui.GuidedSetupDialog(
            get_settings,
            open_destination=MagicMock(),
            open_all_settings=MagicMock(),
            manager=store,
        )
        setup.speech = speech
        setup.begin(welcome=welcome)
        instances.append((setup, built))
        settings = built[0] if built else None
        return setup, settings, store

    yield build
    for setup, built in instances:
        setup.hide()
        for settings in built:
            settings.hide()


# ---- welcome page ----


def test_welcome_page_does_not_build_settings(make_setup):
    setup, settings, store = make_setup(welcome=True)
    assert settings is None
    assert setup.page == setup_ui.WELCOME
    assert setup.step_row.isHidden()
    assert setup.next_button.text() == "Get started"
    assert setup.skip_button.text() == "Not now"
    assert store.get(SettingsKey.GUIDED_SETUP_STATE) is None


@pytest.mark.parametrize("dismiss", ["not_now", "close", "escape"])
def test_welcome_dismissal_is_durable_and_preserves_preferences(make_setup, dismiss):
    values = {SettingsKey.TRANSCRIPT_CLEANUP_PROMPT: "My exact prompt", SettingsKey.AUTO_PASTE: False}
    setup, _, store = make_setup(values, welcome=True)
    before = store.load_all_settings()
    setup.show()
    if dismiss == "not_now":
        setup.skip_button.click()
    elif dismiss == "close":
        setup.close()
    else:
        QTest.keyClick(setup, Qt.Key.Key_Escape)
    assert not setup.isVisible()
    assert store.load_all_settings() == {**before, SettingsKey.GUIDED_SETUP_STATE: guided_setup.SKIPPED}
    assert not guided_setup.should_invite(SettingsManager(store.settings_file))


def test_get_started_records_progress_and_binds_settings_once(make_setup):
    setup, settings, store = make_setup(welcome=True)
    setup.next_button.click()
    assert setup.page == setup_ui.RECORD
    assert store.get(SettingsKey.GUIDED_SETUP_STATE) == guided_setup.STARTED
    first = setup.settings
    setup.show_page(setup_ui.CLEAN_UP)
    assert setup.settings is first


def test_failed_dismissal_keeps_welcome_reviewable_for_retry(make_setup):
    setup, _, store = make_setup(welcome=True)
    setup.show()
    with patch.object(store, "save_setting", side_effect=OSError("Disk full")):
        setup.skip_button.click()
    assert setup.isVisible()
    assert "Disk full" in setup.error.text()
    setup.skip_button.click()
    assert not setup.isVisible()


# ---- steps ----


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
    assert before[SettingsKey.GUIDED_SETUP_STATE] == guided_setup.STARTED
    assert setup.back_button.isHidden()
    setup.next_button.click()
    assert setup.page == setup_ui.CLEAN_UP
    assert setup.cleanup_options.isHidden()
    assert "no text is sent" in setup.cleanup_detail.text()
    setup.next_button.click()
    assert setup.page == setup_ui.OUTPUT
    assert setup.next_button.text() == "Finish"
    assert store.load_all_settings() == before
    assert CLEANUP not in settings._built_pages
    assert GENERAL not in settings._built_pages
    setup.next_button.click()
    assert setup.page == setup_ui.READY
    assert store.load_all_settings() == {
        **before,
        SettingsKey.GUIDED_SETUP_STATE: guided_setup.COMPLETED,
    }
    assert not guided_setup.should_invite(SettingsManager(store.settings_file))


def test_cleanup_toggle_uses_existing_callback_and_hides_dependents(make_setup):
    setup, settings, store = make_setup()
    settings.on_cleanup_changed = MagicMock()
    setup.show_page(setup_ui.CLEAN_UP)
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
    setup.show_page(setup_ui.CLEAN_UP)
    before = store.load_all_settings()
    assert not any(button.isChecked() for button in setup.levels.buttons)
    assert setup.preview.isHidden()
    with patch.object(settings, "_confirm_custom_prompt_removal", return_value=False) as confirm:
        setup.levels.buttons[0].click()
        QApplication.processEvents()
    confirm.assert_called_once_with("light")
    assert store.load_all_settings() == before
    assert not any(button.isChecked() for button in setup.levels.buttons)
    with patch.object(settings, "_confirm_custom_prompt_removal", return_value=True) as confirm:
        setup.levels.buttons[1].click()
        QApplication.processEvents()
    confirm.assert_called_once_with("medium")
    assert store.get(SettingsKey.TRANSCRIPT_CLEANUP_PROMPT) is None
    assert store.get(SettingsKey.TRANSCRIPT_CLEANUP_LEVEL) == "medium"
    assert setup.levels.buttons[1].isChecked()


def test_failed_save_restores_projection_and_source_for_retry(make_setup):
    setup, settings, store = make_setup({SettingsKey.AUTO_PASTE: False})
    setup.show_page(setup_ui.OUTPUT)
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
    desk = InputDevice(index=11, name="Desk microphone", hostapi="MME", default_api=True)
    backup = InputDevice(index=12, name="Backup microphone", hostapi="MME", default_api=True)
    QTest.qWait(15)  # Finish the constructor's empty discovery before injecting devices.
    store.save_setting(SettingsKey.AUDIO_INPUT_PRIORITY, [backup.key])
    settings._microphones.load(store.load_all_settings())
    settings._apply_audio_devices(settings._audio_device_generation, "", [desk, backup], "")
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
    setup.show_page(setup_ui.CLEAN_UP)
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
    "hotkey,mode,expected",
    [
        ("kp *", "toggle", "again to stop. It's on the number pad."),
        ("ctrl+alt+r", "toggle", "again to stop."),
        ("ctrl+alt+r", "push_hold", "Hold it while you speak"),
    ],
)
def test_shortcut_help_follows_trigger_mode_and_names_the_number_pad(hotkey, mode, expected):
    text = setup_ui.shortcut_help(hotkey, {SettingsKey.RECORDING_TRIGGER_MODE: mode})
    assert expected in text
    assert ("number pad" in text) is hotkey.startswith("kp ")


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
def test_privacy_follows_actual_endpoint_and_never_claims_connected(provider, url, local):
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
        name, status, privacy, remote, needs_key = setup_ui.provider_summary(values)
    assert remote is not local
    assert not needs_key
    assert name in privacy
    assert ("on this computer" in privacy) is local
    assert "No audio" in privacy
    assert "Connected" not in status
    assert "Connection not tested" in status


def test_missing_key_is_shown_without_network_probe():
    with patch.object(setup_ui, "resolve_credential", return_value=""):
        _, status, _, _, needs_key = setup_ui.provider_summary({})
    assert status == "API key needed"
    assert needs_key


def test_missing_key_links_to_api_keys_and_a_saved_key_to_cleanup(make_setup):
    setup, _, _ = make_setup({SettingsKey.TRANSCRIPT_CLEANUP_ENABLED: True})
    setup.show_page(setup_ui.CLEAN_UP)
    with patch.object(setup_ui, "resolve_credential", return_value=""):
        setup.refresh()
    assert setup.provider_button.text() == "Add key"
    setup.provider_button.click()
    setup.open_destination.assert_called_with(API_KEYS)
    with patch.object(setup_ui, "resolve_credential", return_value="key"):
        setup.refresh()
    assert setup.provider_button.text() == "Change"
    setup.provider_button.click()
    setup.open_destination.assert_called_with(CLEANUP)


def test_output_uses_existing_streaming_migration_and_callback(make_setup):
    setup, settings, store = make_setup(
        {
            SettingsKey.STREAMING_ENABLED: False,
            SettingsKey.STREAMING_OVERLAY_ENABLED: True,
        }
    )
    settings.on_streaming_settings_changed = MagicMock()
    setup.show_page(setup_ui.OUTPUT)
    setup.live_switch.click()
    assert store.get(SettingsKey.STREAMING_ENABLED) is True
    assert SettingsKey.STREAMING_OVERLAY_ENABLED not in store.load_all_settings()
    settings.on_streaming_settings_changed.assert_called_once_with()


def test_all_settings_and_change_links_use_stable_destinations(make_setup):
    setup, settings, store = make_setup()
    setup._open(CLEANUP)
    setup.open_destination.assert_called_once_with(CLEANUP)
    assert setup.page == setup_ui.RECORD
    setup.all_settings_button.click()
    setup.open_all_settings.assert_called_once_with()
    assert store.get(SettingsKey.GUIDED_SETUP_STATE) == guided_setup.SKIPPED


@pytest.mark.parametrize("ready", [True, False])
def test_speech_engine_status_is_honest_and_links_to_the_model_page(make_setup, ready):
    setup, _, _ = make_setup()
    setup.speech["ready"] = ready
    setup.show_page(setup_ui.RECORD)
    text = setup.speech_summary.text()
    assert ("Downloaded and ready" in text) is ready
    assert ("Not downloaded yet" in text) is not ready
    assert setup.speech_button.text() == ("Change" if ready else "Get it now")
    setup.speech_button.click()
    setup.open_destination.assert_called_with(VOICE_MODEL)
    setup.show_page(setup_ui.READY)
    assert setup.ready_speech.isHidden() is ready


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


# ---- the ready page ----


def test_ready_page_shows_the_shortcut_and_takes_dictation(make_setup):
    setup, _, store = make_setup({SettingsKey.HOTKEYS: {"record_toggle": "ctrl+alt+j"}})
    setup.show()
    setup.show_page(setup_ui.OUTPUT)
    setup.next_button.click()
    QTest.qWait(20)
    assert setup.page == setup_ui.READY
    shown = format_hotkey_display("ctrl+alt+j")  # ⌃⌥J on macOS
    assert setup.keycap.text() == shown
    assert shown in setup.try_box.placeholderText()
    assert setup.property("openwhisperHotkeyWindow") is True
    assert setup.try_box.hasFocus()
    assert setup.next_button.text() == "Done"
    assert setup.skip_button.isHidden()
    setup.next_button.click()
    assert not setup.isVisible()
    assert store.get(SettingsKey.GUIDED_SETUP_STATE) == guided_setup.COMPLETED


@pytest.mark.parametrize("paste,copy,expected", [
    (True, True, "wherever the cursor is"),
    (False, True, "V after you speak"),
    (False, False, "only appears in History"),
])
def test_ready_page_explains_where_the_try_it_text_goes(make_setup, paste, copy, expected):
    setup, _, _ = make_setup({SettingsKey.AUTO_PASTE: paste, SettingsKey.COPY_CLIPBOARD: copy})
    setup.show_page(setup_ui.READY)
    assert expected in setup.try_note.text()


def test_closing_a_finished_setup_keeps_it_completed(make_setup):
    setup, _, store = make_setup()
    setup.show()
    setup.show_page(setup_ui.OUTPUT)
    setup.next_button.click()
    setup.close()
    assert store.get(SettingsKey.GUIDED_SETUP_STATE) == guided_setup.COMPLETED
    setup.begin(welcome=False)
    setup.show()
    setup.skip_button.click()
    assert store.get(SettingsKey.GUIDED_SETUP_STATE) == guided_setup.COMPLETED


# ---- layout ----


@pytest.mark.parametrize("width,scale", [(720, 100), (540, 130)])
def test_all_pages_fit_narrow_and_large_font_layouts(make_setup, isolated_app_theme, width, scale):
    from ui_qt.utils.font_scale import apply_ui_font_scale
    from ui_qt.utils.theme_manager import ThemeManager

    apply_ui_font_scale(scale, app=QApplication.instance(), theme_manager=ThemeManager())
    setup, _, _ = make_setup({SettingsKey.TRANSCRIPT_CLEANUP_ENABLED: True})
    setup.show()
    setup.resize(width, 540)
    for page in range(setup_ui.WELCOME, setup_ui.READY + 1):
        setup.show_page(page)
        QTest.qWait(30)
        assert setup.scroll.horizontalScrollBar().maximum() == 0
        assert setup.next_button.isVisible()
        assert setup.next_button.geometry().bottom() < setup.height()
        viewport = setup.scroll.viewport()
        for control in setup.pages.currentWidget().findChildren((QAbstractButton, QComboBox, QLineEdit)):
            if control.isVisible():
                left = control.mapTo(viewport, control.rect().topLeft()).x()
                assert left >= 0
                assert left + control.width() <= viewport.width()


@pytest.mark.parametrize("page", ["record", "output", "ready"])
def test_short_pages_do_not_scroll_at_the_default_size(make_setup, page):
    setup, _, _ = make_setup({SettingsKey.TRANSCRIPT_CLEANUP_ENABLED: True})
    setup.show()
    setup.resize(720, 690)
    setup.show_page({"record": setup_ui.RECORD, "output": setup_ui.OUTPUT, "ready": setup_ui.READY}[page])
    QTest.qWait(30)
    assert setup.scroll.verticalScrollBar().maximum() == 0


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


def test_failed_cleanup_save_restores_source_gates(make_setup):
    setup, settings, store = make_setup({SettingsKey.TRANSCRIPT_CLEANUP_ENABLED: False})
    setup.show_page(setup_ui.CLEAN_UP)
    with patch.object(store, "save_setting", side_effect=OSError("Disk full")):
        setup.cleanup_switch.click()
    assert not setup.cleanup_switch.isChecked()
    assert setup.cleanup_options.isHidden()
    assert not settings.transcript_cleanup_check.isChecked()
    assert not settings.cleanup_level_tile.isEnabled()
    setup.cleanup_switch.click()
    assert settings.cleanup_level_tile.isEnabled()


def test_recording_shortcut_keyboard_capture_saves_and_keeps_record_step(make_setup):
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
    # Qt reports ControlModifier for the Command key on macOS.
    expected = "cmd+alt+j" if sys.platform == "darwin" else "ctrl+alt+j"
    assert store.load_hotkey_settings()["record_toggle"] == expected
    assert setup.page == setup_ui.RECORD


@pytest.mark.parametrize("size", [(720, 690), (540, 460)])
def test_enabling_cleanup_reveals_privacy(make_setup, size):
    setup, _, _ = make_setup({SettingsKey.TRANSCRIPT_CLEANUP_ENABLED: False})
    setup.show()
    setup.resize(*size)
    setup.show_page(setup_ui.CLEAN_UP)
    setup.cleanup_switch.click()
    QTest.qWait(40)
    viewport = setup.scroll.viewport()
    top = setup.privacy.mapTo(viewport, setup.privacy.rect().topLeft()).y()
    assert top >= 0
    assert top + setup.privacy.height() <= viewport.height()
    assert "Transcript text is sent to" in setup.privacy.text()


# ---- controller ----


def _controller(setup=None):
    return SimpleNamespace(
        _guided_setup_offer_timer=MagicMock(),
        _guided_setup_dialog=setup,
        main_window=MagicMock(),
        _raise_dialog=MagicMock(side_effect=lambda dialog: dialog.show()),
    )


def test_controller_offer_opens_on_welcome_and_menu_opens_first_step(make_setup):
    setup, _, store = make_setup(welcome=True)
    store.save_setting(SettingsKey.GUIDED_SETUP_STATE, guided_setup.PENDING)
    ui = _controller(setup)
    ui._show_guided_setup = lambda welcome: UIController._show_guided_setup(ui, welcome=welcome)
    ui.main_window.isVisible.return_value = True
    ui.main_window.isMinimized.return_value = False
    with patch.object(guided_setup, "settings_manager", store), patch.object(
        QApplication, "activeModalWidget", return_value=None
    ):
        UIController._offer_guided_setup_now(ui)
    assert setup.isVisible() and setup.page == setup_ui.WELCOME
    setup.skip_button.click()
    UIController.open_guided_setup(ui)
    assert setup.page == setup_ui.RECORD
    assert store.get(SettingsKey.GUIDED_SETUP_STATE) == guided_setup.STARTED


def test_controller_reopens_singleton_without_changing_dictation_choices(make_setup):
    setup, settings, store = make_setup({SettingsKey.AUTO_PASTE: False})
    ui = _controller(setup)
    ui._show_guided_setup = lambda welcome: UIController._show_guided_setup(ui, welcome=welcome)
    UIController.open_guided_setup(ui)
    assert setup.page == setup_ui.RECORD
    setup.show_page(setup_ui.OUTPUT)
    setup.next_button.click()
    assert store.get(SettingsKey.GUIDED_SETUP_STATE) == guided_setup.COMPLETED
    setup.next_button.click()
    UIController.open_guided_setup(ui)
    assert ui._guided_setup_dialog is setup
    assert setup.page == setup_ui.RECORD
    assert store.get(SettingsKey.AUTO_PASTE) is False
    assert store.get(SettingsKey.GUIDED_SETUP_STATE) == guided_setup.COMPLETED
    assert not guided_setup.should_invite(store)


def test_offer_waits_for_startup_modal_and_never_reappears_after_skip(tmp_path, monkeypatch):
    store = SettingsManager(str(tmp_path / "settings.json"))
    guided_setup.initialize(store)
    monkeypatch.setattr(guided_setup, "settings_manager", store)
    ui = _controller()
    ui._show_guided_setup = MagicMock()
    ui.main_window.isVisible.return_value = True
    ui.main_window.isMinimized.return_value = False
    with patch.object(QApplication, "activeModalWidget", return_value=object()):
        UIController._offer_guided_setup_now(ui)
    ui._guided_setup_offer_timer.start.assert_called_once_with(1000)
    ui._show_guided_setup.assert_not_called()
    guided_setup.save_state(guided_setup.SKIPPED, store)
    with patch.object(QApplication, "activeModalWidget", return_value=None):
        UIController._offer_guided_setup_now(ui)
    ui._show_guided_setup.assert_not_called()


@pytest.mark.parametrize("visible,minimized", [(False, False), (True, True)])
def test_offer_waits_for_a_visible_main_window(tmp_path, monkeypatch, visible, minimized):
    store = SettingsManager(str(tmp_path / "settings.json"))
    guided_setup.initialize(store)
    monkeypatch.setattr(guided_setup, "settings_manager", store)
    ui = _controller()
    ui._show_guided_setup = MagicMock()
    ui.main_window.isVisible.return_value = visible
    ui.main_window.isMinimized.return_value = minimized
    with patch.object(QApplication, "activeModalWidget", return_value=None):
        UIController._offer_guided_setup_now(ui)
    ui._show_guided_setup.assert_not_called()
    ui._guided_setup_offer_timer.start.assert_called_once_with(1000)
