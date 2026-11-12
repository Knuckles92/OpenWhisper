"""Saving the dictation engine is what tells every view of it."""
import json
import threading

import pytest

from services.settings import SettingsKey, SettingsManager, settings_manager


def _manager(tmp_path, saved=None):
    path = tmp_path / "openwhisper_settings.json"
    if saved is not None:
        path.write_text(json.dumps(saved), encoding="utf-8")
    return SettingsManager(str(path))


class TestWatch:
    def test_a_save_that_changes_a_watched_key_is_heard(self, tmp_path):
        manager = _manager(tmp_path)
        heard = []
        manager.watch([SettingsKey.WHISPER_MODEL, SettingsKey.WHISPER_DEVICE], heard.append)

        manager.save_setting(SettingsKey.WHISPER_MODEL, "small")
        manager.update_settings({SettingsKey.WHISPER_DEVICE: "cpu", "ui_theme": "dark"})

        assert heard == [{SettingsKey.WHISPER_MODEL}, {SettingsKey.WHISPER_DEVICE}]

    def test_the_new_value_is_saved_before_the_watcher_runs(self, tmp_path):
        manager = _manager(tmp_path)
        seen = []
        manager.watch([SettingsKey.WHISPER_MODEL],
                      lambda _keys: seen.append(manager.get(SettingsKey.WHISPER_MODEL)))
        manager.save_setting(SettingsKey.WHISPER_MODEL, "small")
        assert seen == ["small"]

    def test_saving_the_same_value_or_another_key_is_not_heard(self, tmp_path):
        manager = _manager(tmp_path, {SettingsKey.WHISPER_MODEL: "small"})
        heard = []
        manager.watch([SettingsKey.WHISPER_MODEL], heard.append)

        manager.save_setting(SettingsKey.WHISPER_MODEL, "small")
        manager.save_setting("ui_theme", "dark")

        assert heard == []

    def test_a_change_inside_a_saved_mapping_is_heard(self, tmp_path):
        manager = _manager(tmp_path, {SettingsKey.LOCAL_ASR_MODELS: {"parakeet": "parakeet-v3"}})
        heard = []
        manager.watch([SettingsKey.LOCAL_ASR_MODELS], heard.append)

        def choose(settings):
            settings[SettingsKey.LOCAL_ASR_MODELS]["parakeet"] = "orukeet-v0.1"

        manager.mutate_settings(choose)
        assert heard == [{SettingsKey.LOCAL_ASR_MODELS}]

    def test_saving_none_over_an_unset_key_and_removing_a_key_are_changes(self, tmp_path):
        manager = _manager(tmp_path)
        heard = []
        manager.watch([SettingsKey.WHISPER_MODEL], heard.append)

        manager.save_setting(SettingsKey.WHISPER_MODEL, None)
        manager.update_settings({}, remove=(SettingsKey.WHISPER_MODEL,))

        assert len(heard) == 2

    def test_replacing_every_setting_is_heard(self, tmp_path):
        manager = _manager(tmp_path, {SettingsKey.SELECTED_MODEL: "parakeet"})
        heard = []
        manager.watch([SettingsKey.SELECTED_MODEL], heard.append)

        manager.save_all_settings({SettingsKey.SELECTED_MODEL: "parakeet", "ui_theme": "dark"})
        manager.save_all_settings({SettingsKey.SELECTED_MODEL: "api"})

        assert heard == [{SettingsKey.SELECTED_MODEL}]

    def test_a_stopped_watch_hears_nothing(self, tmp_path):
        manager = _manager(tmp_path)
        heard = []
        stop = manager.watch([SettingsKey.WHISPER_MODEL], heard.append)
        stop()
        stop()
        manager.save_setting(SettingsKey.WHISPER_MODEL, "small")
        assert heard == []

    def test_a_failing_watcher_neither_blocks_the_save_nor_the_next_watcher(self, tmp_path):
        manager = _manager(tmp_path)
        heard = []

        def fail(_keys):
            raise RuntimeError("view is gone")

        manager.watch([SettingsKey.WHISPER_MODEL], fail)
        manager.watch([SettingsKey.WHISPER_MODEL], heard.append)

        manager.save_setting(SettingsKey.WHISPER_MODEL, "small")

        assert manager.get(SettingsKey.WHISPER_MODEL) == "small"
        assert heard == [{SettingsKey.WHISPER_MODEL}]

    def test_a_failed_save_is_not_heard(self, tmp_path):
        manager = _manager(tmp_path)
        heard = []
        manager.watch([SettingsKey.WHISPER_MODEL], heard.append)

        def fail(settings):
            settings[SettingsKey.WHISPER_MODEL] = "small"
            raise ValueError("no")

        with pytest.raises(ValueError):
            manager.mutate_settings(fail)
        assert heard == []

    def test_a_watcher_may_save_without_deadlock(self, tmp_path):
        manager = _manager(tmp_path)
        manager.watch([SettingsKey.WHISPER_MODEL],
                      lambda _keys: manager.save_setting("ui_theme", "dark"))
        worker = threading.Thread(
            target=manager.save_setting, args=(SettingsKey.WHISPER_MODEL, "small"), daemon=True)
        worker.start()
        worker.join(5)
        assert not worker.is_alive()
        assert manager.get("ui_theme") == "dark"


@pytest.fixture
def tabs(qapp):
    from ui_qt.widgets.quick_record_tab import QuickRecordTab
    from ui_qt.widgets.upload_file_tab import UploadFileTab

    settings_manager.update_settings({
        SettingsKey.SELECTED_MODEL: "local_whisper",
        SettingsKey.WHISPER_MODEL: "base",
        SettingsKey.WHISPER_DEVICE: "auto",
    })
    record, upload = QuickRecordTab(), UploadFileTab()
    for tab in (record, upload):
        tab.sync_engine_selection()
    yield record, upload
    for tab in (record, upload):
        tab.close()


class TestTabsFollowTheSavedEngine:
    def test_a_saved_backend_shows_on_every_tab(self, tabs):
        settings_manager.save_model_selection("parakeet")
        for tab in tabs:
            assert tab.current_backend() == "Parakeet"
            assert tab.local_engine.model_combo.currentData() == "parakeet-v3"

    def test_a_backend_without_local_fields_hides_them_on_every_tab(self, tabs):
        settings_manager.save_model_selection("api")
        for tab in tabs:
            assert tab.current_backend() == "API"
            assert tab.local_engine.isHidden()
            assert not tab.api_server.isHidden()

    def test_a_model_and_device_chosen_on_one_tab_show_on_the_other(self, tabs):
        record, upload = tabs
        reloads = []
        record.engine_settings_changed.connect(lambda: reloads.append("record"))
        upload.engine_settings_changed.connect(lambda: reloads.append("upload"))

        record.local_engine.model_combo.setCurrentText("small")
        device = record.local_engine.device_combo
        device.setCurrentIndex(device.findData("cpu"))

        assert upload.local_engine.model_combo.currentText() == "small"
        assert upload.local_engine.device_combo.currentData() == "cpu"
        # Only the tab that was used asks for the engine reload.
        assert reloads == ["record", "record"]

    def test_a_device_the_controller_reverts_shows_on_every_tab(self, tabs):
        settings_manager.save_setting(SettingsKey.WHISPER_DEVICE, "cpu")
        for tab in tabs:
            assert tab.local_engine.device_combo.currentData() == "cpu"

    def test_a_save_from_a_worker_thread_reaches_the_tabs(self, tabs, qapp):
        worker = threading.Thread(
            target=settings_manager.save_setting, args=(SettingsKey.WHISPER_MODEL, "tiny"))
        worker.start()
        worker.join(5)
        qapp.processEvents()
        for tab in tabs:
            assert tab.local_engine.model_combo.currentText() == "tiny"

    def test_a_saved_engine_does_not_announce_a_choice(self, tabs):
        chosen = []
        for tab in tabs:
            tab.model_changed.connect(chosen.append)
        settings_manager.save_model_selection("moonshine")
        assert chosen == []

    def test_an_unrelated_save_leaves_the_fields_alone(self, tabs):
        record, _upload = tabs
        record.local_engine.model_combo.blockSignals(True)
        record.local_engine.model_combo.setCurrentText("small")
        record.local_engine.model_combo.blockSignals(False)
        settings_manager.save_setting("ui_theme", "dark")
        assert record.local_engine.model_combo.currentText() == "small"


def test_the_main_window_tabs_follow_a_choice_once_it_is_saved(qapp):
    from unittest.mock import patch

    from ui_qt.main_window import MainWindow

    with patch("ui_qt.dialogs.meeting_intro_dialog.maybe_show_meeting_mode_intro"):
        window = MainWindow()
    try:
        window._ensure_screen("upload")
        record, upload = window.existing_transcription_tabs()
        # The controller saves a choice it accepts; stand in for it.
        from config import config

        window.model_changed.connect(
            lambda name: settings_manager.save_model_selection(config.MODEL_VALUE_MAP[name]))

        record.choose_backend("Moonshine")

        assert upload.current_backend() == "Moonshine"
        assert not upload.local_engine.isHidden()
    finally:
        window.close()
