"""Qt tests for the Model Manager dialog and its model rows."""
import os
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QFrame, QMessageBox

from services.components import ComponentInfo, ComponentState
from services.hf_access import CachedModelInfo
from services.settings import (
    MeetingAgentCore,
    MeetingSpeakerIdBackend,
    SettingsKey,
    SettingsManager,
    TranscriptCleanupModelSort,
    TranscriptCleanupProvider,
    default_transcript_cleanup_model,
)
from ui_qt.dialogs import model_manager_dialog as dialog_module
from ui_qt.dialogs import settings_dialog as settings_dialog_module
from ui_qt.dialogs.model_manager_dialog import ModelManagerDialog
from ui_qt.utils.theme_manager import ThemeManager
from ui_qt.widgets import Button
from ui_qt.widgets.component_row_widget import ComponentRowWidget
from ui_qt.widgets import text_model_picker as picker_module


def _cached(repo_id, size_bytes):
    return CachedModelInfo(
        repo_id=repo_id,
        size_bytes=size_bytes,
        path=f"/hub/models--{repo_id.replace('/', '--')}",
        revision_hashes=("abc",),
    )


BASE_REPO = "Systran/faster-whisper-base"
TINY_REPO = "Systran/faster-whisper-tiny"


class _FakeSettings:
    """In-memory settings store that matches the Model Manager call surface."""

    def __init__(self, values):
        self.values = values

    def get(self, key, default=None):
        return self.values.get(key, default)

    def save_setting(self, key, value):
        self.values[key] = value

    def load_all_settings(self):
        return dict(self.values)

    def save_all_settings(self, settings):
        self.values.clear()
        self.values.update(settings)

    def load_model_selection(self):
        return self.values.get(SettingsKey.SELECTED_MODEL, "local_whisper")


class _DialogTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _settings_values(self, active_model="base", extra=None):
        values = {
            SettingsKey.WHISPER_MODEL: active_model,
            SettingsKey.WHISPER_DEVICE: "auto",
            SettingsKey.WHISPER_COMPUTE_TYPE: "auto",
            SettingsKey.SELECTED_MODEL: "local_whisper",
            SettingsKey.MEETING_WHISPER_MODEL: "auto",
            SettingsKey.MEETING_LANGUAGE: "auto",
            SettingsKey.MEETING_LLM_PROVIDER: "openrouter",
            SettingsKey.MEETING_LLM_MODEL: "deepseek/test-model",
            SettingsKey.MEETING_AGENT_CORE: MeetingAgentCore.DIRECT,
            SettingsKey.MEETING_SPEAKER_ID_BACKEND: MeetingSpeakerIdBackend.LOCAL,
            SettingsKey.TRANSCRIPT_CLEANUP_PROVIDER: "openai",
            SettingsKey.TRANSCRIPT_CLEANUP_MODEL: "gpt-test",
            SettingsKey.TRANSCRIPT_CLEANUP_MODEL_SORT: (
                TranscriptCleanupModelSort.ALPHABETICAL
            ),
        }
        if extra:
            values.update(extra)
        return values

    def _make_dialog(
        self,
        cached=None,
        active_model="base",
        loaded_model=None,
        env_blocked=False,
        extra_settings=None,
        api_keys=None,
        focus_library=False,
    ):
        values = self._settings_values(active_model, extra_settings)
        fake_settings = _FakeSettings(values)
        patchers = [
            patch.object(
                dialog_module, "scan_cached_models", return_value=cached or {}
            ),
            patch.object(dialog_module, "settings_manager", fake_settings),
            patch.object(
                dialog_module,
                "is_hf_hub_offline_env_set",
                return_value=env_blocked,
            ),
            patch.object(
                picker_module,
                "find_api_key",
                side_effect=lambda selected_provider: (api_keys or {}).get(
                    selected_provider
                ),
            ),
        ]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)
        dialog = ModelManagerDialog(get_loaded_model=lambda: loaded_model)
        if focus_library:
            dialog.show_library_tab()
        return dialog, values


class TestModelRows(_DialogTestCase):
    """Per-row status, size, and action availability."""

    def _make_dialog(self, **kwargs):
        kwargs.setdefault("focus_library", True)
        return super()._make_dialog(**kwargs)

    def test_dialog_uses_compact_resizable_default(self):
        previous_stylesheet = self.app.styleSheet()
        self.app.setStyleSheet(ThemeManager().stylesheet)
        self.addCleanup(self.app.setStyleSheet, previous_stylesheet)
        dialog, _values = self._make_dialog()
        dialog.show()
        self.app.processEvents()

        self.assertEqual((dialog.width(), dialog.height()), (900, 620))
        self.assertEqual(
            (dialog.minimumWidth(), dialog.minimumHeight()), (720, 480)
        )
        self.assertTrue(dialog.isSizeGripEnabled())
        self.assertTrue(
            dialog.windowFlags() & Qt.WindowType.WindowMaximizeButtonHint
        )
        self.assertFalse(
            dialog.windowFlags() & Qt.WindowType.MSWindowsFixedSizeDialogHint
        )

        dialog.resize(800, 560)
        self.app.processEvents()

        self.assertEqual((dialog.width(), dialog.height()), (800, 560))

        dialog.resize(740, 500)
        self.app.processEvents()

        self.assertEqual((dialog.width(), dialog.height()), (740, 500))

    def test_tall_tabs_scroll_instead_of_growing_dialog(self):
        dialog, _values = self._make_dialog()

        self.assertTrue(dialog.library_scroll_area.widgetResizable())
        self.assertTrue(dialog.ondemand_scroll_area.widgetResizable())
        self.assertTrue(dialog.meeting_scroll_area.widgetResizable())
        self.assertIsNotNone(dialog.library_scroll_area.widget())
        self.assertIsNotNone(dialog.ondemand_scroll_area.widget())
        self.assertIsNotNone(dialog.meeting_scroll_area.widget())
        self.assertGreater(
            dialog.library_scroll_area.widget().minimumSizeHint().height(),
            dialog.library_scroll_area.minimumSizeHint().height(),
        )
        self.assertGreater(
            dialog.ondemand_scroll_area.widget().minimumSizeHint().height(),
            dialog.ondemand_scroll_area.minimumSizeHint().height(),
        )
        self.assertGreater(
            dialog.meeting_scroll_area.widget().minimumSizeHint().height(),
            dialog.meeting_scroll_area.minimumSizeHint().height(),
        )

    def test_catalog_excludes_auto(self):
        dialog, _values = self._make_dialog()
        self.assertNotIn("auto", dialog.rows)
        self.assertIn("base", dialog.rows)

    def test_uncached_row_offers_download_with_estimate(self):
        dialog, _values = self._make_dialog()
        row = dialog.rows["tiny"]
        self.assertTrue(row.download_button.isVisibleTo(dialog))
        self.assertFalse(row.delete_button.isVisibleTo(dialog))
        self.assertFalse(row.set_active_button.isVisibleTo(dialog))
        self.assertEqual(row.badge.text(), "Not downloaded")
        self.assertEqual(row.size_label.text(), "~76 MB")
    def test_cached_row_shows_real_size_and_delete(self):
        dialog, _values = self._make_dialog(
            cached={TINY_REPO: _cached(TINY_REPO, 76_000_000)}
        )
        row = dialog.rows["tiny"]
        self.assertFalse(row.download_button.isVisibleTo(dialog))
        self.assertTrue(row.delete_button.isVisibleTo(dialog))
        self.assertTrue(row.delete_button.isEnabled())
        self.assertFalse(row.set_active_button.isVisibleTo(dialog))
        self.assertEqual(row.badge.text(), "Downloaded")
        self.assertEqual(row.size_label.text(), "76 MB")

    def test_library_rows_hide_set_active_and_show_usage(self):
        dialog, _values = self._make_dialog(
            cached={BASE_REPO: _cached(BASE_REPO, 145_000_000)},
            active_model="base",
        )
        row = dialog.rows["base"]
        self.assertEqual(row.badge.text(), "Downloaded")
        self.assertFalse(row.set_active_button.isVisibleTo(dialog))
        self.assertEqual(row.usage_label.text(), "On-demand")
        self.assertTrue(row.usage_label.isVisibleTo(dialog))
        self.assertFalse(dialog.rows["tiny"].usage_label.isVisibleTo(dialog))

    def test_loaded_model_delete_is_disabled(self):
        dialog, _values = self._make_dialog(
            cached={BASE_REPO: _cached(BASE_REPO, 145_000_000)},
            loaded_model="base",
        )
        row = dialog.rows["base"]
        self.assertFalse(row.delete_button.isEnabled())
        self.assertIn("In use", row.delete_button.toolTip())

    def test_refresh_moves_delete_lock_when_loaded_model_changes(self):
        """After a Set Active reload, Delete must follow the newly loaded model."""
        loaded = {"name": "base"}
        dialog, _values = self._make_dialog(
            cached={
                BASE_REPO: _cached(BASE_REPO, 145_000_000),
                TINY_REPO: _cached(TINY_REPO, 76_000_000),
            },
            active_model="tiny",
            loaded_model="base",
        )
        dialog._get_loaded_model = lambda: loaded["name"]
        dialog.refresh()
        self.assertFalse(dialog.rows["base"].delete_button.isEnabled())
        self.assertTrue(dialog.rows["tiny"].delete_button.isEnabled())

        loaded["name"] = "tiny"
        dialog.refresh()
        self.assertTrue(dialog.rows["base"].delete_button.isEnabled())
        self.assertFalse(dialog.rows["tiny"].delete_button.isEnabled())

    def test_stats_count_and_disk_usage(self):
        dialog, _values = self._make_dialog(
            cached={
                BASE_REPO: _cached(BASE_REPO, 145_000_000),
                TINY_REPO: _cached(TINY_REPO, 76_000_000),
            }
        )
        self.assertEqual(dialog.downloaded_stat.value.text(), "2")
        self.assertEqual(dialog.disk_stat.value.text(), "221 MB")


class TestDownloadingState(_DialogTestCase):
    """Indeterminate download state: badge + one download at a time."""

    def test_downloading_row_and_other_downloads_blocked(self):
        dialog, _values = self._make_dialog()
        dialog.set_downloading("tiny")

        self.assertEqual(dialog.rows["tiny"].badge.text(), "Downloading…")
        self.assertFalse(dialog.rows["tiny"].download_button.isEnabled())
        # Only one download at a time: other rows' Download disabled too.
        self.assertFalse(dialog.rows["small"].download_button.isEnabled())

        dialog.finish_download("tiny", success=True)
        self.assertTrue(dialog.rows["small"].download_button.isEnabled())

    def test_failed_download_reports_in_message(self):
        dialog, _values = self._make_dialog()
        dialog.set_downloading("tiny")
        dialog.finish_download("tiny", success=False)
        self.assertIn("failed", dialog.message_label.text())


class TestEnvBlocked(_DialogTestCase):
    """HF_HUB_OFFLINE disables downloads but not deletion."""

    def _make_dialog(self, **kwargs):
        kwargs.setdefault("focus_library", True)
        return super()._make_dialog(**kwargs)

    def test_banner_shown_and_downloads_disabled(self):
        dialog, _values = self._make_dialog(
            cached={BASE_REPO: _cached(BASE_REPO, 145_000_000)},
            env_blocked=True,
        )
        self.assertTrue(dialog.env_banner.isVisibleTo(dialog))
        self.assertFalse(dialog.rows["tiny"].download_button.isEnabled())
        self.assertTrue(dialog.rows["base"].delete_button.isEnabled())


class TestFilter(_DialogTestCase):
    """Filter box hides non-matching rows and shows the empty state."""

    def _make_dialog(self, **kwargs):
        kwargs.setdefault("focus_library", True)
        return super()._make_dialog(**kwargs)

    def test_filter_matches_name_and_repo(self):
        dialog, _values = self._make_dialog()
        dialog.filter_edit.setText("tiny")
        self.assertTrue(dialog.rows["tiny"].isVisibleTo(dialog))
        self.assertTrue(dialog.rows["tiny.en"].isVisibleTo(dialog))
        self.assertFalse(dialog.rows["base"].isVisibleTo(dialog))

    def test_no_match_shows_empty_state(self):
        dialog, _values = self._make_dialog()
        dialog.filter_edit.setText("no-such-model")
        self.assertTrue(dialog.empty_label.isVisibleTo(dialog))
        dialog.filter_edit.setText("")
        self.assertFalse(dialog.empty_label.isVisibleTo(dialog))

    def test_status_filter_downloaded_only(self):
        dialog, _values = self._make_dialog(
            cached={BASE_REPO: _cached(BASE_REPO, 145_000_000)}
        )
        dialog.status_filter_combo.setCurrentIndex(
            dialog.status_filter_combo.findData("downloaded")
        )
        self.assertTrue(dialog.rows["base"].isVisibleTo(dialog))
        self.assertFalse(dialog.rows["tiny"].isVisibleTo(dialog))

    def test_status_filter_not_downloaded_only(self):
        dialog, _values = self._make_dialog(
            cached={BASE_REPO: _cached(BASE_REPO, 145_000_000)}
        )
        dialog.status_filter_combo.setCurrentIndex(
            dialog.status_filter_combo.findData("not_downloaded")
        )
        self.assertFalse(dialog.rows["base"].isVisibleTo(dialog))
        self.assertTrue(dialog.rows["tiny"].isVisibleTo(dialog))

    def test_status_filter_combines_with_search(self):
        dialog, _values = self._make_dialog(
            cached={
                BASE_REPO: _cached(BASE_REPO, 145_000_000),
                TINY_REPO: _cached(TINY_REPO, 76_000_000),
            }
        )
        dialog.status_filter_combo.setCurrentIndex(
            dialog.status_filter_combo.findData("downloaded")
        )
        dialog.filter_edit.setText("tiny")
        self.assertTrue(dialog.rows["tiny"].isVisibleTo(dialog))
        self.assertFalse(dialog.rows["base"].isVisibleTo(dialog))
        self.assertFalse(dialog.rows["tiny.en"].isVisibleTo(dialog))

    def test_status_filter_all_shows_everything(self):
        dialog, _values = self._make_dialog(
            cached={BASE_REPO: _cached(BASE_REPO, 145_000_000)}
        )
        dialog.status_filter_combo.setCurrentIndex(
            dialog.status_filter_combo.findData("downloaded")
        )
        dialog.status_filter_combo.setCurrentIndex(
            dialog.status_filter_combo.findData("all")
        )
        self.assertTrue(dialog.rows["base"].isVisibleTo(dialog))
        self.assertTrue(dialog.rows["tiny"].isVisibleTo(dialog))


class TestCompactButtons(_DialogTestCase):
    """Header/footer buttons must not clip their labels."""

    def test_open_folder_button_fits_label(self):
        dialog, _values = self._make_dialog()
        open_folder = next(
            (
                button
                for button in dialog.findChildren(Button)
                if button.text() == "Open Folder"
            ),
            None,
        )
        self.assertIsNotNone(open_folder)
        open_folder.ensurePolished()
        needed = open_folder.sizeHint().width()
        self.assertGreaterEqual(open_folder.maximumWidth(), needed)
        self.assertGreaterEqual(open_folder.minimumWidth(), needed)
        self.assertEqual(open_folder.minimumWidth(), open_folder.maximumWidth())


class TestSorting(_DialogTestCase):
    """Built-in sort choices make common catalog scans one step."""

    @staticmethod
    def _row_order(dialog):
        order = []
        for index in range(dialog.list_layout.count()):
            widget = dialog.list_layout.itemAt(index).widget()
            if widget in dialog.rows.values():
                order.append(widget.model_name)
        return order

    def test_default_keeps_active_model_in_place(self):
        """Recommended sort must not pin the active model to the top."""
        dialog, _values = self._make_dialog(active_model="medium")
        self.assertNotEqual(self._row_order(dialog)[0], "medium")
        # Same ordering as size within the not-downloaded group: tiny first.
        self.assertEqual(self._row_order(dialog)[0], "tiny")

    def test_downloaded_first_groups_cached_models(self):
        dialog, _values = self._make_dialog(
            cached={BASE_REPO: _cached(BASE_REPO, 145_000_000)}
        )
        dialog.sort_combo.setCurrentIndex(
            dialog.sort_combo.findData("downloaded")
        )
        self.assertEqual(self._row_order(dialog)[0], "base")

    def test_smallest_first_uses_catalog_estimates(self):
        dialog, _values = self._make_dialog(active_model="medium")
        dialog.sort_combo.setCurrentIndex(dialog.sort_combo.findData("size"))
        self.assertEqual(self._row_order(dialog)[0], "tiny")

    def test_name_sort_is_alphabetical(self):
        dialog, _values = self._make_dialog()
        dialog.sort_combo.setCurrentIndex(dialog.sort_combo.findData("name"))
        order = self._row_order(dialog)
        self.assertEqual(order, sorted(order, key=str.casefold))


class TestActions(_DialogTestCase):
    """Row actions route through the dialog callbacks."""

    def test_download_click_invokes_callback(self):
        dialog, _values = self._make_dialog()
        requested = []
        dialog.on_download_requested = requested.append
        dialog.rows["tiny"].download_button.click()
        self.assertEqual(requested, ["tiny"])

    def test_delete_confirm_default_no_does_nothing(self):
        dialog, _values = self._make_dialog(
            cached={BASE_REPO: _cached(BASE_REPO, 145_000_000)}
        )
        requested = []
        dialog.on_delete_requested = requested.append
        with patch.object(
            QMessageBox,
            "question",
            return_value=QMessageBox.StandardButton.No,
        ):
            dialog.rows["base"].delete_button.click()
        self.assertEqual(requested, [])

    def test_delete_confirm_yes_invokes_callback(self):
        dialog, _values = self._make_dialog(
            cached={BASE_REPO: _cached(BASE_REPO, 145_000_000)}
        )
        requested = []
        dialog.on_delete_requested = requested.append
        with patch.object(
            QMessageBox,
            "question",
            return_value=QMessageBox.StandardButton.Yes,
        ):
            dialog.rows["base"].delete_button.click()
        self.assertEqual(requested, ["base"])

    def test_ondemand_picker_invokes_set_active_callback(self):
        dialog, _values = self._make_dialog(
            cached={TINY_REPO: _cached(TINY_REPO, 76_000_000)},
            active_model="base",
        )
        requested = []
        dialog.on_set_active_requested = requested.append
        index = dialog.ondemand_whisper_picker.model_combo.findData("tiny")
        dialog.ondemand_whisper_picker.model_combo.setCurrentIndex(index)
        self.assertEqual(requested, ["tiny"])


class TestTextModelManager(_DialogTestCase):
    """Text models use one provider-to-model selection flow."""

    def _make_text_dialog(
        self, provider="openai", model="gpt-test", api_keys=None
    ):
        return self._make_dialog(
            extra_settings={
                SettingsKey.TRANSCRIPT_CLEANUP_PROVIDER: provider,
                SettingsKey.TRANSCRIPT_CLEANUP_MODEL: model,
                SettingsKey.TRANSCRIPT_CLEANUP_MODEL_SORT: (
                    TranscriptCleanupModelSort.MOST_POPULAR
                ),
            },
            api_keys=api_keys,
        )

    def test_provider_credential_status_shows_when_key_is_found(self):
        dialog, _values = self._make_text_dialog(
            provider=TranscriptCleanupProvider.OPENROUTER,
            api_keys={TranscriptCleanupProvider.OPENROUTER: "test-key"},
        )

        status = dialog.text_model_picker.provider_requirement
        self.assertEqual(status.text(), "OPENROUTER_API_KEY found")
        self.assertTrue(status.property("available"))

    def test_provider_credential_status_keeps_requirement_when_key_is_missing(self):
        dialog, _values = self._make_text_dialog(
            provider=TranscriptCleanupProvider.OPENROUTER
        )

        status = dialog.text_model_picker.provider_requirement
        self.assertEqual(status.text(), "Requires OPENROUTER_API_KEY")
        self.assertFalse(status.property("available"))

    def test_ondemand_meeting_and_library_have_separate_tabs(self):
        dialog, _values = self._make_text_dialog()
        labels = [dialog.tabs.tabText(i) for i in range(dialog.tabs.count())]
        self.assertEqual(labels, ["On-demand", "Meeting Mode", "Library"])
        self.assertFalse(dialog.tabs.tabIcon(0).isNull())
        self.assertFalse(dialog.tabs.tabIcon(1).isNull())
        self.assertFalse(dialog.tabs.tabIcon(2).isNull())

    def test_text_flow_uses_distinct_numbered_section_cards(self):
        dialog, _values = self._make_text_dialog()
        cards = [
            frame
            for frame in dialog.text_model_picker.findChildren(QFrame)
            if frame.objectName() == "textModelSectionCard"
        ]

        self.assertEqual(len(cards), 2)
        self.assertEqual(
            dialog.text_model_picker.current_model_value.text(), "gpt-test"
        )
        self.assertGreaterEqual(
            dialog.text_model_picker.active_summary_card.minimumHeight(), 56
        )
        self.assertFalse(
            dialog.text_model_picker.active_summary_icon.pixmap().isNull()
        )
        self.assertFalse(dialog.text_model_picker.refresh_button.icon().isNull())

    def test_text_models_use_one_labeled_provider_selector(self):
        dialog, _values = self._make_text_dialog()
        labels = [
            dialog.text_model_picker.provider_combo.itemText(i)
            for i in range(dialog.text_model_picker.provider_combo.count())
        ]
        self.assertEqual(labels, ["OpenAI", "OpenRouter"])
        self.assertEqual(
            dialog.text_model_picker.provider,
            TranscriptCleanupProvider.OPENAI,
        )
        self.assertFalse(hasattr(dialog, "text_provider_tabs"))
        self.assertEqual(
            dialog.text_model_picker.active_summary.text(),
            "Active now: OpenAI · gpt-test",
        )

    def test_switching_provider_updates_the_same_model_picker(self):
        dialog, _values = self._make_text_dialog()

        index = dialog.text_model_picker.provider_combo.findData(
            TranscriptCleanupProvider.OPENROUTER
        )
        dialog.text_model_picker.provider_combo.setCurrentIndex(index)

        self.assertEqual(
            dialog.text_model_picker.provider,
            TranscriptCleanupProvider.OPENROUTER,
        )
        self.assertEqual(dialog.text_model_picker.provider_title.text(), "OpenRouter")
        self.assertEqual(
            dialog.text_model_picker.model_combo.currentText(),
            default_transcript_cleanup_model("openrouter"),
        )
        self.assertFalse(dialog.text_model_picker.sort_combo.isHidden())

    def test_active_now_badge_only_shows_for_active_provider(self):
        dialog, _values = self._make_text_dialog(
            provider=TranscriptCleanupProvider.OPENROUTER,
            model="openrouter/free",
        )
        picker = dialog.text_model_picker

        self.assertFalse(picker.active_summary_card.isHidden())
        self.assertEqual(
            picker.active_summary.text(),
            "Active now: OpenRouter · openrouter/free",
        )

        openai_index = picker.provider_combo.findData(
            TranscriptCleanupProvider.OPENAI
        )
        picker.provider_combo.setCurrentIndex(openai_index)

        self.assertTrue(picker.active_summary_card.isHidden())
        self.assertEqual(picker.current_model_value.text(), "openrouter/free")

        picker.provider_combo.setCurrentIndex(
            picker.provider_combo.findData(TranscriptCleanupProvider.OPENROUTER)
        )
        self.assertFalse(picker.active_summary_card.isHidden())

    def test_set_active_persists_provider_and_model(self):
        dialog, values = self._make_text_dialog()
        picker = dialog.text_model_picker
        index = picker.provider_combo.findData(
            TranscriptCleanupProvider.OPENROUTER
        )
        picker.provider_combo.setCurrentIndex(index)
        picker.model_combo.setCurrentText("anthropic/claude-test")

        dialog._activate_text_model(TranscriptCleanupProvider.OPENROUTER)

        self.assertEqual(
            values[SettingsKey.TRANSCRIPT_CLEANUP_PROVIDER], "openrouter"
        )
        self.assertEqual(
            values[SettingsKey.TRANSCRIPT_CLEANUP_MODEL],
            "anthropic/claude-test",
        )
        self.assertEqual(picker.activate_button.text(), "Active")
        self.assertFalse(picker.activate_button.isEnabled())

    def test_catalog_result_populates_matching_provider(self):
        dialog, _values = self._make_text_dialog(model="gpt-4o-mini")
        models = ["gpt-4.1", "gpt-4o-mini", "o4-mini"]

        dialog._on_text_models_loaded(
            TranscriptCleanupProvider.OPENAI,
            TranscriptCleanupModelSort.ALPHABETICAL,
            models,
            "",
        )

        picker = dialog.text_model_picker
        self.assertEqual(picker.model_combo.count(), len(models))
        self.assertEqual(picker.model_combo.currentText(), "gpt-4o-mini")
        self.assertEqual(picker.status_label.text(), "3 models available")


class TestCleanupSettingsOwnership(_DialogTestCase):
    """Cleanup Settings must not overwrite Model Manager selections."""

    def test_saving_cleanup_settings_preserves_text_model_keys(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            isolated = SettingsManager(os.path.join(temp_dir, "settings.json"))
            isolated.save_all_settings(
                {
                    SettingsKey.TRANSCRIPT_CLEANUP_PROVIDER: "openrouter",
                    SettingsKey.TRANSCRIPT_CLEANUP_MODEL: "provider/model-test",
                    SettingsKey.TRANSCRIPT_CLEANUP_MODEL_SORT: (
                        TranscriptCleanupModelSort.NEWEST
                    ),
                }
            )
            with (
                patch.object(settings_dialog_module, "settings_manager", isolated),
                patch.object(
                    settings_dialog_module.history_manager,
                    "set_max_recordings",
                ),
            ):
                dialog = settings_dialog_module.SettingsDialog()
                dialog._save_settings()

            saved = isolated.load_all_settings()
            self.assertEqual(
                saved[SettingsKey.TRANSCRIPT_CLEANUP_PROVIDER], "openrouter"
            )
            self.assertEqual(
                saved[SettingsKey.TRANSCRIPT_CLEANUP_MODEL],
                "provider/model-test",
            )
            self.assertEqual(
                saved[SettingsKey.TRANSCRIPT_CLEANUP_MODEL_SORT],
                TranscriptCleanupModelSort.NEWEST,
            )

    def test_cleanup_tab_links_to_model_manager(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            isolated = SettingsManager(os.path.join(temp_dir, "settings.json"))
            with (
                patch.object(settings_dialog_module, "settings_manager", isolated),
                patch.object(
                    settings_dialog_module.history_manager,
                    "set_max_recordings",
                ),
            ):
                dialog = settings_dialog_module.SettingsDialog()
                self.assertIsNone(dialog.open_model_manager_on_close)

                dialog.open_model_manager_btn.click()

                self.assertEqual(dialog.open_model_manager_on_close, "text")
                self.assertEqual(
                    dialog.result(),
                    dialog.DialogCode.Accepted,
                )

    def test_saving_meeting_settings_preserves_model_keys(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            isolated = SettingsManager(os.path.join(temp_dir, "settings.json"))
            isolated.save_all_settings(
                {
                    SettingsKey.MEETING_WHISPER_MODEL: "tiny",
                    SettingsKey.MEETING_LLM_PROVIDER: "openai",
                    SettingsKey.MEETING_LLM_MODEL: "gpt-4o-mini",
                    SettingsKey.MEETING_LANGUAGE: "fr",
                    SettingsKey.MEETING_AGENT_CORE: MeetingAgentCore.DIRECT,
                    SettingsKey.MEETING_SPEAKER_ID_BACKEND: (
                        MeetingSpeakerIdBackend.OPENAI
                    ),
                }
            )
            with (
                patch.object(settings_dialog_module, "settings_manager", isolated),
                patch.object(
                    settings_dialog_module.history_manager,
                    "set_max_recordings",
                ),
            ):
                dialog = settings_dialog_module.SettingsDialog()
                summary = dialog.meeting_model_summary.text()
                self.assertIn("Whisper · tiny", summary)
                self.assertIn("Spoken language · French", summary)
                self.assertIn("OpenAI · gpt-4o-mini", summary)
                self.assertIn("Agent core · Direct", summary)
                self.assertIn("Speaker ID · OpenAI", summary)
                dialog._save_settings()

            saved = isolated.load_all_settings()
            self.assertEqual(saved[SettingsKey.MEETING_WHISPER_MODEL], "tiny")
            self.assertEqual(saved[SettingsKey.MEETING_LLM_PROVIDER], "openai")
            self.assertEqual(
                saved[SettingsKey.MEETING_LLM_MODEL], "gpt-4o-mini"
            )
            self.assertEqual(saved[SettingsKey.MEETING_LANGUAGE], "fr")
            self.assertEqual(
                saved[SettingsKey.MEETING_AGENT_CORE], MeetingAgentCore.DIRECT
            )
            self.assertEqual(
                saved[SettingsKey.MEETING_SPEAKER_ID_BACKEND],
                MeetingSpeakerIdBackend.OPENAI,
            )

    def test_meeting_tab_links_to_model_manager(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            isolated = SettingsManager(os.path.join(temp_dir, "settings.json"))
            with (
                patch.object(settings_dialog_module, "settings_manager", isolated),
                patch.object(
                    settings_dialog_module.history_manager,
                    "set_max_recordings",
                ),
            ):
                dialog = settings_dialog_module.SettingsDialog()
                self.assertIsNone(dialog.open_model_manager_on_close)

                dialog.open_meeting_model_manager_btn.click()

                self.assertEqual(dialog.open_model_manager_on_close, "meeting")
                self.assertEqual(
                    dialog.result(),
                    dialog.DialogCode.Accepted,
                )


class TestMeetingModelManager(_DialogTestCase):
    """Meeting tab owns Whisper ASR, extras, and intelligence selection."""

    def _make_meeting_dialog(
        self,
        whisper="auto",
        provider="openrouter",
        model="deepseek/test-model",
        cached=None,
        extra=None,
    ):
        settings = {
            SettingsKey.MEETING_WHISPER_MODEL: whisper,
            SettingsKey.MEETING_LLM_PROVIDER: provider,
            SettingsKey.MEETING_LLM_MODEL: model,
        }
        if extra:
            settings.update(extra)
        return self._make_dialog(cached=cached, extra_settings=settings)

    def test_meeting_picker_offers_auto(self):
        dialog, _values = self._make_meeting_dialog()
        names = [
            dialog.meeting_whisper_picker.model_combo.itemData(i)
            for i in range(dialog.meeting_whisper_picker.model_combo.count())
        ]
        self.assertIn("auto", names)

    def test_meeting_picker_persists_whisper_model(self):
        dialog, values = self._make_meeting_dialog(
            whisper="auto",
            cached={TINY_REPO: _cached(TINY_REPO, 76_000_000)},
        )
        index = dialog.meeting_whisper_picker.model_combo.findData("tiny")
        dialog.meeting_whisper_picker.model_combo.setCurrentIndex(index)
        self.assertEqual(values[SettingsKey.MEETING_WHISPER_MODEL], "tiny")
        self.assertEqual(dialog.rows["tiny"].usage_label.text(), "Meetings")

    def test_meeting_llm_activation_persists_provider_and_model(self):
        dialog, values = self._make_meeting_dialog()
        picker = dialog.meeting_model_picker
        index = picker.provider_combo.findData(
            TranscriptCleanupProvider.OPENAI
        )
        picker.provider_combo.setCurrentIndex(index)
        picker.model_combo.setCurrentText("gpt-4o-mini")

        dialog._activate_meeting_llm_model(TranscriptCleanupProvider.OPENAI)

        self.assertEqual(values[SettingsKey.MEETING_LLM_PROVIDER], "openai")
        self.assertEqual(values[SettingsKey.MEETING_LLM_MODEL], "gpt-4o-mini")
        self.assertEqual(picker.activate_button.text(), "Active")

    def test_meeting_language_and_core_persist(self):
        dialog, values = self._make_meeting_dialog()
        language_index = dialog.meeting_language_combo.findData("en")
        dialog.meeting_language_combo.setCurrentIndex(language_index)
        core_index = dialog.meeting_agent_core_combo.findData(
            MeetingAgentCore.DIRECT
        )
        dialog.meeting_agent_core_combo.setCurrentIndex(core_index)

        self.assertEqual(values[SettingsKey.MEETING_LANGUAGE], "en")
        self.assertEqual(
            values[SettingsKey.MEETING_AGENT_CORE], MeetingAgentCore.DIRECT
        )

    def test_usage_chip_combines_both_modes(self):
        dialog, _values = self._make_dialog(
            cached={BASE_REPO: _cached(BASE_REPO, 145_000_000)},
            active_model="base",
            extra_settings={SettingsKey.MEETING_WHISPER_MODEL: "base"},
        )
        self.assertEqual(
            dialog.rows["base"].usage_label.text(), "On-demand · Meetings"
        )


class TestOnDemandEngine(_DialogTestCase):
    """On-demand recording engine routes through the main-window path."""

    def test_engine_combo_invokes_backend_callback(self):
        dialog, _values = self._make_dialog()
        requested = []
        dialog.on_backend_changed = requested.append
        index = dialog.engine_combo.findData("api_whisper")
        dialog.engine_combo.setCurrentIndex(index)
        self.assertEqual(requested, ["API: Whisper"])
        self.assertFalse(dialog.ondemand_whisper_picker.isEnabled())


class TestModelManagerTabFocus(_DialogTestCase):
    """Model Manager can open directly on On-demand, Meeting, or Library."""

    def test_show_text_tab_selects_ondemand_and_text_card(self):
        dialog, _values = self._make_dialog()
        self.assertIs(dialog.tabs.currentWidget(), dialog.ondemand_tab)

        dialog.show_text_tab()

        self.assertIs(dialog.tabs.currentWidget(), dialog.ondemand_tab)

    def test_show_meeting_tab_selects_meeting(self):
        dialog, _values = self._make_dialog()
        self.assertIs(dialog.tabs.currentWidget(), dialog.ondemand_tab)

        dialog.show_meeting_tab()

        self.assertIs(dialog.tabs.currentWidget(), dialog.meeting_tab)

    def test_show_library_tab_selects_library(self):
        dialog, _values = self._make_dialog()
        dialog.show_library_tab()
        self.assertIs(dialog.tabs.currentWidget(), dialog.library_tab)


class TestComponentRows(_DialogTestCase):
    """GPU component states must expose an honest, usable action."""

    @staticmethod
    def _info(state, download_bytes=0, reason=""):
        return ComponentInfo(
            component_id="gpu-accel",
            display_name="GPU Acceleration",
            summary="CUDA runtime",
            state=state,
            installed_version=None,
            available_version="test",
            download_bytes=download_bytes,
            install_bytes=0,
            reason=reason,
        )

    def test_missing_gpu_component_has_enabled_install_button(self):
        row = ComponentRowWidget("gpu-accel")
        row.update_state(
            self._info(ComponentState.NOT_INSTALLED, download_bytes=1_000_000),
            installing=False,
        )

        self.assertFalse(row.install_button.isHidden())
        self.assertTrue(row.install_button.isEnabled())
        self.assertEqual(row.install_button.text(), "Install")

    def test_existing_cuda_setup_is_not_reported_missing(self):
        row = ComponentRowWidget("gpu-accel")
        row.update_state(
            self._info(
                ComponentState.EXTERNAL,
                reason="CUDA libraries are already available.",
            ),
            installing=False,
        )

        self.assertEqual(row.badge.text(), "Available")
        self.assertEqual(row.size_label.text(), "Existing setup")
        self.assertTrue(row.install_button.isHidden())
        self.assertTrue(row.remove_button.isHidden())


if __name__ == "__main__":
    unittest.main()
