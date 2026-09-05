"""Qt tests for the Downloads window: catalog, filters, actions, inspector.

Split out of ``test_model_manager_dialog`` when the sixteen-row Whisper catalog
moved out of the assignment surface.
"""
import os
from unittest.mock import patch

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QLabel,
    QMessageBox,
    QScrollArea,
    QWidget,
)

from services.component_catalog import PI_HOME_URL, get_component_details
from services.components import ComponentId, ComponentInfo, ComponentState
from services.hf_access import CachedModelInfo, get_hf_cache_dir
from services.settings import SettingsKey
from ui_qt.dialogs import downloads_dialog as dialog_module
from ui_qt.dialogs import component_details_dialog as component_dialog_module
from ui_qt.dialogs.component_details_dialog import ComponentDetailsDialog
from ui_qt.dialogs.downloads_dialog import BatchDownloadDialog, DownloadsDialog
from ui_qt.utils.theme_manager import ThemeManager
from ui_qt.widgets import Button
from ui_qt.widgets.component_row_widget import ComponentRowWidget
from ui_qt.widgets.model_row_widget import ModelRowWidget


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


class _DialogTestCase:
    @pytest.fixture(scope="class", autouse=True)
    @classmethod
    def _qapp(cls):
        cls.app = QApplication.instance() or QApplication([])

    @pytest.fixture(autouse=True)
    def _patch_stack(self):
        self._started = []
        yield
        for patcher in reversed(self._started):
            patcher.stop()

    def _make_dialog(
        self,
        cached=None,
        active_model="base",
        meeting_model="auto",
        loaded_model=None,
        env_blocked=False,
    ):
        values = {
            SettingsKey.WHISPER_MODEL: active_model,
            SettingsKey.SELECTED_MODEL: "local_whisper",
            SettingsKey.MEETING_WHISPER_MODEL: meeting_model,
        }
        patchers = [
            patch.object(
                dialog_module, "scan_cached_models", return_value=cached or {}
            ),
            patch.object(
                dialog_module, "settings_manager", _FakeSettings(values)
            ),
            patch.object(
                dialog_module,
                "is_hf_hub_offline_env_set",
                return_value=env_blocked,
            ),
            # The optional-backend inventory reads the real local cache; a
            # machine with Parakeet or Moonshine installed would otherwise
            # change counts, sort order, and status-filter results.
            patch("services.local_asr.cache.inventory", return_value={}),
        ]
        for patcher in patchers:
            patcher.start()
            self._started.append(patcher)
        return DownloadsDialog(
            get_loaded_model=lambda: loaded_model,
            background_cache_scan=False,
        ), values


class TestWindowShell(_DialogTestCase):
    """The window is resizable; only the two content columns scroll."""

    def test_default_and_minimum_size_allow_a_row_beside_the_inspector(self):
        dialog, _values = self._make_dialog()
        assert (dialog.width(), dialog.height()) == (1060, 680)
        assert (dialog.minimumWidth(), dialog.minimumHeight()) == (980, 560)
        assert dialog.isSizeGripEnabled()
        assert not dialog.isModal()

    def test_rows_are_not_clipped_at_the_minimum_window_width(self):
        previous_stylesheet = self.app.styleSheet()
        self.app.setStyleSheet(ThemeManager().stylesheet)
        try:
            dialog, _values = self._make_dialog()
            dialog.show()
            dialog.resize(dialog.MINIMUM_SIZE)
            self.app.processEvents()

            container = dialog.library_scroll_area.widget()
            assert (
                container.minimumSizeHint().width()
                <= dialog.library_scroll_area.viewport().width()
            )
        finally:
            self.app.setStyleSheet(previous_stylesheet)

    def test_only_the_catalog_and_inspector_body_scroll(self):
        dialog, _values = self._make_dialog()
        scrollers = dialog.findChildren(QScrollArea)
        assert set(scrollers) == {
            dialog.library_scroll_area,
            dialog.inspector_detail_scroll,
        }
        assert dialog.library_scroll_area.widgetResizable()
        assert dialog.inspector_detail_scroll.widgetResizable()

    def test_rows_share_the_right_edge_with_the_toolbar(self):
        """The list's scroll bar must not pull its rows in past the filters."""
        previous_stylesheet = self.app.styleSheet()
        self.app.setStyleSheet(ThemeManager().stylesheet)
        try:
            dialog, _values = self._make_dialog()
            dialog.show()
            self.app.processEvents()

            def right_edge(widget):
                return widget.mapTo(dialog, QPoint(widget.width(), 0)).x()

            assert dialog.library_scroll_area.verticalScrollBar().isVisible()
            assert (
                abs(right_edge(dialog.rows["tiny"]) - right_edge(dialog.sort_combo))
                <= 1
            )
        finally:
            self.app.setStyleSheet(previous_stylesheet)

    def test_catalog_column_and_selection_bar_have_transparent_surface(self):
        """The catalog column and selection bar must not paint a mismatched box."""
        previous_stylesheet = self.app.styleSheet()
        self.app.setStyleSheet(ThemeManager().stylesheet)
        try:
            dialog, _values = self._make_dialog()
            dialog.show()
            self.app.processEvents()

            bar = dialog.findChild(QWidget, "downloadsSelectionBar")
            col = dialog.findChild(QWidget, "downloadsCatalogColumn")
            assert bar is not None
            assert col is not None
            assert not bar.autoFillBackground()
            assert not col.autoFillBackground()
            theme = ThemeManager().stylesheet
            assert (
                "QWidget#downloadsCatalogColumn,\n"
                "QWidget#downloadsSelectionBar,"
            ) in theme
            catalog_rule = theme.split("QWidget#downloadsCatalogColumn", 1)[1]
            catalog_rule = catalog_rule.split("}", 1)[0]
            assert "background-color: transparent" in catalog_rule
        finally:
            self.app.setStyleSheet(previous_stylesheet)

    def test_open_folder_button_fits_its_label(self):
        dialog, _values = self._make_dialog()
        open_folder = next(
            button
            for button in dialog.findChildren(Button)
            if button.text() == "Open folder"
        )
        open_folder.ensurePolished()
        needed = open_folder.sizeHint().width()
        assert open_folder.minimumWidth() >= needed
        assert open_folder.minimumWidth() == open_folder.maximumWidth()


class TestModelRows(_DialogTestCase):
    """Per-row status, size, and action availability."""

    def test_catalog_excludes_auto(self):
        dialog, _values = self._make_dialog()
        assert "auto" not in dialog.rows
        assert "base" in dialog.rows

    def test_uncached_row_offers_download_with_estimate(self):
        dialog, _values = self._make_dialog()
        row = dialog.rows["tiny"]
        assert row.download_button.isVisibleTo(dialog)
        assert not row.delete_button.isVisibleTo(dialog)
        assert row.badge.text() == "Not downloaded"
        assert row.size_label.text() == "~76 MB"

    def test_cached_row_shows_real_size_and_delete(self):
        dialog, _values = self._make_dialog(
            cached={TINY_REPO: _cached(TINY_REPO, 76_000_000)}
        )
        row = dialog.rows["tiny"]
        assert not row.download_button.isVisibleTo(dialog)
        assert row.delete_button.isEnabled()
        assert row.badge.text() == "Downloaded"
        assert row.size_label.text() == "76 MB"

    def test_assignment_is_reported_but_not_offered(self):
        """Set Active belongs to Model Manager; this window only shows usage."""
        dialog, _values = self._make_dialog(
            cached={BASE_REPO: _cached(BASE_REPO, 145_000_000)},
            active_model="base",
        )
        row = dialog.rows["base"]
        assert not row.set_active_button.isVisibleTo(dialog)
        assert row.usage_label.text() == "On-demand"
        assert row.usage_label.isVisibleTo(dialog)
        assert not dialog.rows["tiny"].usage_label.isVisibleTo(dialog)

    def test_usage_chip_combines_both_modes(self):
        dialog, _values = self._make_dialog(
            cached={BASE_REPO: _cached(BASE_REPO, 145_000_000)},
            active_model="base",
            meeting_model="base",
        )
        assert dialog.rows["base"].usage_label.text() == "On-demand · Meetings"

    def test_loaded_model_delete_is_disabled(self):
        dialog, _values = self._make_dialog(
            cached={BASE_REPO: _cached(BASE_REPO, 145_000_000)},
            loaded_model="base",
        )
        row = dialog.rows["base"]
        assert not row.delete_button.isEnabled()
        assert "In use" in row.delete_button.toolTip()

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
        assert not dialog.rows["base"].delete_button.isEnabled()
        assert dialog.rows["tiny"].delete_button.isEnabled()

        loaded["name"] = "tiny"
        dialog.refresh()
        assert dialog.rows["base"].delete_button.isEnabled()
        assert not dialog.rows["tiny"].delete_button.isEnabled()

    def test_header_reports_count_disk_usage_and_folder(self):
        dialog, _values = self._make_dialog(
            cached={
                BASE_REPO: _cached(BASE_REPO, 145_000_000),
                TINY_REPO: _cached(TINY_REPO, 76_000_000),
            }
        )
        text = dialog.stats_label.text()
        assert text.startswith("2 of 22 speech models")
        assert "221 MB used" in text


class TestDownloadingState(_DialogTestCase):
    """Indeterminate download state: badge + one download at a time."""

    def test_downloading_row_and_other_downloads_blocked(self):
        dialog, _values = self._make_dialog()
        dialog.set_downloading("tiny")

        assert dialog.rows["tiny"].badge.text() == "Downloading…"
        assert not dialog.rows["tiny"].download_button.isEnabled()
        assert not dialog.rows["small"].download_button.isEnabled()

        dialog.finish_download("tiny", success=True)
        assert dialog.rows["small"].download_button.isEnabled()

    def test_failed_download_reports_in_message(self):
        dialog, _values = self._make_dialog()
        dialog.set_downloading("tiny")
        dialog.finish_download("tiny", success=False)
        assert "failed" in dialog.message_label.text()

    def test_download_progress_updates_row_and_footer(self):
        dialog, _values = self._make_dialog()
        dialog.set_downloading("tiny")
        dialog.set_download_progress("tiny", 40_000_000, 80_000_000)

        row = dialog.rows["tiny"]
        assert row.progress.isVisibleTo(dialog)
        assert row.progress.value() == 50
        assert "of" in row.size_label.text()
        assert "tiny" in dialog.message_label.text()


class TestEnvBlocked(_DialogTestCase):
    """HF_HUB_OFFLINE disables downloads but not deletion."""

    def test_banner_shown_and_downloads_disabled(self):
        dialog, _values = self._make_dialog(
            cached={BASE_REPO: _cached(BASE_REPO, 145_000_000)},
            env_blocked=True,
        )
        assert dialog.env_banner.isVisibleTo(dialog)
        assert not dialog.rows["tiny"].download_button.isEnabled()
        assert dialog.rows["base"].delete_button.isEnabled()


class TestFilter(_DialogTestCase):
    """Search and status filters hide rows and show the empty state."""

    def test_filter_matches_name_and_repo(self):
        dialog, _values = self._make_dialog()
        dialog.filter_edit.setText("tiny")
        assert dialog.rows["tiny"].isVisibleTo(dialog)
        assert dialog.rows["tiny.en"].isVisibleTo(dialog)
        assert not dialog.rows["base"].isVisibleTo(dialog)

    def test_no_match_shows_empty_state(self):
        dialog, _values = self._make_dialog()
        dialog.filter_edit.setText("no-such-model")
        assert dialog.empty_label.isVisibleTo(dialog)
        dialog.filter_edit.setText("")
        assert not dialog.empty_label.isVisibleTo(dialog)

    def test_status_filter_downloaded_only(self):
        dialog, _values = self._make_dialog(
            cached={BASE_REPO: _cached(BASE_REPO, 145_000_000)}
        )
        dialog.status_filter_combo.setCurrentIndex(
            dialog.status_filter_combo.findData("downloaded")
        )
        assert dialog.rows["base"].isVisibleTo(dialog)
        assert not dialog.rows["tiny"].isVisibleTo(dialog)

    def test_status_filter_not_downloaded_only(self):
        dialog, _values = self._make_dialog(
            cached={BASE_REPO: _cached(BASE_REPO, 145_000_000)}
        )
        dialog.status_filter_combo.setCurrentIndex(
            dialog.status_filter_combo.findData("not_downloaded")
        )
        assert not dialog.rows["base"].isVisibleTo(dialog)
        assert dialog.rows["tiny"].isVisibleTo(dialog)

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
        assert dialog.rows["tiny"].isVisibleTo(dialog)
        assert not dialog.rows["base"].isVisibleTo(dialog)
        assert not dialog.rows["tiny.en"].isVisibleTo(dialog)

    def test_status_filter_all_shows_everything(self):
        dialog, _values = self._make_dialog(
            cached={BASE_REPO: _cached(BASE_REPO, 145_000_000)}
        )
        dialog.status_filter_combo.setCurrentIndex(
            dialog.status_filter_combo.findData("all")
        )
        assert dialog.rows["base"].isVisibleTo(dialog)
        assert dialog.rows["tiny"].isVisibleTo(dialog)


    def test_backend_filter_shows_one_family(self):
        dialog, _values = self._make_dialog()
        dialog.backend_filter_combo.setCurrentIndex(
            dialog.backend_filter_combo.findData("parakeet")
        )
        assert dialog.rows["parakeet-v3"].isVisibleTo(dialog)
        assert not dialog.rows["qwen-0.6b"].isVisibleTo(dialog)
        assert not dialog.rows["base"].isVisibleTo(dialog)

    def test_backend_filter_whisper_covers_distilled_checkpoints(self):
        dialog, _values = self._make_dialog()
        dialog.backend_filter_combo.setCurrentIndex(
            dialog.backend_filter_combo.findData("local_whisper")
        )
        whisper_rows = [
            name for name, row in dialog.rows.items() if row.isVisibleTo(dialog)
        ]
        assert "base" in whisper_rows
        assert any(name.startswith("distil-") for name in whisper_rows)
        assert not any(name.startswith(("parakeet", "qwen", "nemotron", "moonshine")) for name in whisper_rows)

    def test_backend_filter_lists_every_backend_once(self):
        from services.local_asr.catalog import BACKENDS
        dialog, _values = self._make_dialog()
        combo = dialog.backend_filter_combo
        ids = [combo.itemData(i) for i in range(combo.count())]
        assert ids == ["all", "local_whisper", *BACKENDS]
        assert combo.currentData() == "all"

    def test_backend_filter_combines_with_status_and_search(self):
        dialog, _values = self._make_dialog(
            cached={BASE_REPO: _cached(BASE_REPO, 145_000_000)}
        )
        dialog.backend_filter_combo.setCurrentIndex(
            dialog.backend_filter_combo.findData("moonshine")
        )
        dialog.status_filter_combo.setCurrentIndex(
            dialog.status_filter_combo.findData("not_downloaded")
        )
        assert dialog.rows["moonshine-small"].isVisibleTo(dialog)
        assert dialog.rows["moonshine-medium"].isVisibleTo(dialog)
        assert not dialog.rows["base"].isVisibleTo(dialog)
        dialog.filter_edit.setText("medium")
        assert dialog.rows["moonshine-medium"].isVisibleTo(dialog)
        assert not dialog.rows["moonshine-small"].isVisibleTo(dialog)
        assert not dialog.rows["medium"].isVisibleTo(dialog)
        dialog.status_filter_combo.setCurrentIndex(
            dialog.status_filter_combo.findData("downloaded")
        )
        assert dialog.empty_label.isVisibleTo(dialog)

    def test_backend_filter_all_restores_every_row(self):
        dialog, _values = self._make_dialog()
        combo = dialog.backend_filter_combo
        combo.setCurrentIndex(combo.findData("nemotron"))
        combo.setCurrentIndex(combo.findData("all"))
        assert all(row.isVisibleTo(dialog) for row in dialog.rows.values())


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

    def test_default_keeps_assigned_model_in_place(self):
        """Recommended sort must not pin the assigned model to the top."""
        dialog, _values = self._make_dialog(active_model="medium")
        assert self._row_order(dialog)[0] == "tiny"

    def test_downloaded_first_groups_cached_models(self):
        dialog, _values = self._make_dialog(
            cached={BASE_REPO: _cached(BASE_REPO, 145_000_000)}
        )
        dialog.sort_combo.setCurrentIndex(dialog.sort_combo.findData("downloaded"))
        assert self._row_order(dialog)[0] == "base"

    def test_smallest_first_uses_catalog_estimates(self):
        dialog, _values = self._make_dialog(active_model="medium")
        dialog.sort_combo.setCurrentIndex(dialog.sort_combo.findData("size"))
        assert self._row_order(dialog)[0] == "tiny"

    def test_name_sort_is_alphabetical(self):
        dialog, _values = self._make_dialog()
        dialog.sort_combo.setCurrentIndex(dialog.sort_combo.findData("name"))
        order = self._row_order(dialog)
        assert order == sorted(order, key=str.casefold)

    def test_backend_sort_keeps_each_family_contiguous(self):
        """Whisper first, then the optional backends in filter-combo order."""
        dialog, _values = self._make_dialog(active_model="medium")
        dialog.sort_combo.setCurrentIndex(dialog.sort_combo.findData("backend"))
        backends = [dialog.rows[name].backend for name in self._row_order(dialog)]
        first_seen = list(dict.fromkeys(backends))
        assert first_seen == ["local_whisper", "parakeet", "qwen_asr", "nemotron", "moonshine"]
        assert backends == sorted(backends, key=first_seen.index)
        # Inside a family the recommended order still applies.
        assert self._row_order(dialog)[0] == "tiny"


class TestActions(_DialogTestCase):
    """Row actions route through the dialog callbacks."""

    def test_download_click_invokes_callback(self):
        dialog, _values = self._make_dialog()
        requested = []
        dialog.on_download_requested = requested.append
        dialog.rows["tiny"].download_button.click()
        assert requested == ["tiny"]

    def test_delete_confirm_default_no_does_nothing(self):
        dialog, _values = self._make_dialog(
            cached={BASE_REPO: _cached(BASE_REPO, 145_000_000)}
        )
        requested = []
        dialog.on_delete_requested = requested.append
        with patch.object(
            QMessageBox, "question", return_value=QMessageBox.StandardButton.No
        ):
            dialog.rows["base"].delete_button.click()
        assert requested == []

    def test_delete_confirm_yes_invokes_callback(self):
        dialog, _values = self._make_dialog(
            cached={BASE_REPO: _cached(BASE_REPO, 145_000_000)}
        )
        requested = []
        dialog.on_delete_requested = requested.append
        with patch.object(
            QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes
        ):
            dialog.rows["base"].delete_button.click()
        assert requested == ["base"]

    def test_delete_result_is_reported_in_the_message_line(self):
        dialog, _values = self._make_dialog()
        dialog.show_delete_result("base", success=False, error="locked")
        assert "locked" in dialog.message_label.text()


class TestInspector(_DialogTestCase):
    """The side inspector replaced the separate Model Details dialog."""

    def test_inspector_is_empty_until_a_model_is_selected(self):
        dialog, _values = self._make_dialog()
        assert dialog.inspector_name.text() == "—"
        assert not dialog.inspector_repo_button.isVisibleTo(dialog)
        assert "Select a model" in dialog.inspector_description.text()

    def test_selecting_a_row_shows_its_bundled_profile(self):
        dialog, _values = self._make_dialog()
        dialog.select_model("base")

        assert dialog.inspector_name.text() == "base"
        assert dialog.inspector_repo_button.isVisibleTo(dialog)
        assert dialog.inspector_description.text()
        assert dialog.fact_labels["Repository"].text() == BASE_REPO
        assert dialog.fact_labels["Local format"].text()
        assert dialog.inspector_tradeoffs.text().startswith("•")

    def test_tag_pill_shows_its_full_text_under_the_theme(self):
        """The themed pill pads the label; that padding must not clip the text."""
        previous_stylesheet = self.app.styleSheet()
        self.app.setStyleSheet(ThemeManager().stylesheet)
        try:
            dialog, _values = self._make_dialog()
            dialog.show()
            dialog.select_model("parakeet-v3")
            self.app.processEvents()

            tags = dialog.inspector_tags
            assert tags.text()
            assert QLabel.text(tags) == tags.text()
            assert (
                tags.contentsRect().width()
                >= tags.fontMetrics().horizontalAdvance(tags.text())
            )
        finally:
            self.app.setStyleSheet(previous_stylesheet)

    def test_row_click_signal_selects_that_model(self):
        dialog, _values = self._make_dialog()
        dialog.rows["small"].details_requested.emit("small")

        assert dialog.inspector_name.text() == "small"
        assert dialog.rows["small"].property("selected")
        assert not dialog.rows["base"].property("selected")

    def test_auto_has_no_profile_to_show(self):
        dialog, _values = self._make_dialog()
        dialog.select_model("auto")
        assert dialog.inspector_name.text() == "—"

    def test_inspector_reports_the_selected_model_usage(self):
        dialog, _values = self._make_dialog(
            cached={BASE_REPO: _cached(BASE_REPO, 145_000_000)},
            active_model="base",
        )
        dialog.select_model("base")
        dialog.refresh()
        assert dialog.inspector_usage.text() == "On-demand"
        assert dialog.inspector_usage.isVisibleTo(dialog)

    def test_representative_profiles_render_expected_facts(self):
        dialog, _values = self._make_dialog()
        expected = {
            "base": ("OpenAI Whisper", "74 million", "Multilingual"),
            "base.en": ("OpenAI Whisper", "74 million", "English only"),
            "distil-large-v3": ("Distil-Whisper", "756 million", "English only"),
            "turbo": ("OpenAI Whisper", "809 million", "Multilingual"),
        }
        for model_name, (family, parameters, languages) in expected.items():
            dialog.select_model(model_name)
            assert dialog.fact_labels["Family"].text() == family, model_name
            assert dialog.fact_labels["Parameters"].text() == parameters, model_name
            assert dialog.fact_labels["Languages"].text() == languages, model_name
            assert "CTranslate2" in dialog.fact_labels["Local format"].text()
            assert dialog.fact_labels["License"].text() == "MIT"

    def test_profile_needs_no_network_under_the_hf_override(self):
        with (
            patch.dict(os.environ, {"HF_HUB_OFFLINE": "1"}),
            patch(
                "huggingface_hub.HfApi",
                side_effect=AssertionError("network metadata must not be requested"),
            ) as hf_api,
        ):
            dialog, _values = self._make_dialog()
            dialog.select_model("tiny")

        assert dialog.fact_labels["Origin"].text() == "openai/whisper-tiny"
        hf_api.assert_not_called()

    def test_repository_links_open_the_bundled_urls(self):
        dialog, _values = self._make_dialog()
        dialog.select_model("base")
        opened = []
        with patch.object(
            dialog_module.QDesktopServices,
            "openUrl",
            side_effect=lambda url: opened.append(url.toString()),
        ):
            dialog.inspector_repo_button.click()
            dialog.inspector_origin_button.click()
        assert opened[0].endswith(BASE_REPO)
        assert opened[1].startswith("http")


class TestRowActivation(_DialogTestCase):
    """Selecting a row must stay separate from its actions."""

    def test_row_body_click_selects_the_model(self):
        row = ModelRowWidget("base")
        row.resize(720, 64)
        row.show()
        requested = []
        row.details_requested.connect(requested.append)

        QTest.mouseClick(row, Qt.MouseButton.LeftButton, pos=QPoint(5, 5))

        assert requested == ["base"]
        row.close()

    def test_enter_and_space_select_the_model(self):
        row = ModelRowWidget("tiny")
        requested = []
        row.details_requested.connect(requested.append)

        QTest.keyClick(row, Qt.Key.Key_Return)
        QTest.keyClick(row, Qt.Key.Key_Space)

        assert requested == ["tiny", "tiny"]

    def test_action_buttons_do_not_select_the_model(self):
        row = ModelRowWidget("small")
        selected = []
        downloads = []
        deletes = []
        row.details_requested.connect(selected.append)
        row.download_clicked.connect(downloads.append)
        row.delete_clicked.connect(deletes.append)

        row.download_button.click()
        row.delete_button.click()

        assert downloads == ["small"]
        assert deletes == ["small"]
        assert selected == []


class TestBatchSelection(_DialogTestCase):
    """Multi-model selection: checkboxes, running total, confirmation."""

    def test_uncached_rows_offer_selection_cached_do_not(self):
        dialog, _values = self._make_dialog(
            cached={BASE_REPO: _cached(BASE_REPO, 145_000_000)}
        )
        assert dialog.rows["tiny"].select_checkbox.isVisibleTo(dialog)
        assert not dialog.rows["base"].select_checkbox.isVisibleTo(dialog)

    def test_toggling_updates_summary_and_button(self):
        dialog, _values = self._make_dialog()
        dialog.rows["tiny"].select_checkbox.setChecked(True)
        assert "1 selected" in dialog.selection_summary.text()
        assert "~76 MB" in dialog.selection_summary.text()
        assert dialog.download_selected_button.text() == "Download 1 model…"

        dialog.rows["base"].select_checkbox.setChecked(True)
        assert "2 selected" in dialog.selection_summary.text()
        assert "~221 MB" in dialog.selection_summary.text()
        assert dialog.download_selected_button.text() == "Download 2 models…"

    def test_clear_empties_the_selection(self):
        dialog, _values = self._make_dialog()
        dialog.rows["tiny"].select_checkbox.setChecked(True)
        dialog._on_clear_selection_clicked()
        assert not dialog.rows["tiny"].select_checkbox.isChecked()
        assert dialog.selection_summary.text() == ""
        assert not dialog.download_selected_button.isEnabled()

    def test_select_all_checks_only_missing_models(self):
        dialog, _values = self._make_dialog(
            cached={BASE_REPO: _cached(BASE_REPO, 145_000_000)}
        )
        dialog._on_select_all_clicked()
        assert dialog.rows["tiny"].select_checkbox.isChecked()
        assert not dialog.rows["base"].select_checkbox.isChecked()

    def test_download_selected_confirms_and_requests_in_catalog_order(self):
        requested = []
        dialog, _values = self._make_dialog()
        dialog.on_batch_download_requested = requested.append
        dialog.rows["base"].select_checkbox.setChecked(True)
        dialog.rows["tiny"].select_checkbox.setChecked(True)

        with patch.object(
            BatchDownloadDialog,
            "exec",
            return_value=QDialog.DialogCode.Accepted,
        ):
            dialog.download_selected_button.click()

        assert requested == [["tiny", "base"]]

    def test_canceled_confirmation_requests_nothing(self):
        requested = []
        dialog, _values = self._make_dialog()
        dialog.on_batch_download_requested = requested.append
        dialog.rows["tiny"].select_checkbox.setChecked(True)

        with patch.object(
            BatchDownloadDialog,
            "exec",
            return_value=QDialog.DialogCode.Rejected,
        ):
            dialog.download_selected_button.click()

        assert requested == []

    def test_confirmation_lists_sizes_total_and_destination(self):
        box = BatchDownloadDialog(["tiny", "base"])
        assert "tiny  \u00b7  ~76 MB" in box.model_list_label.text()
        assert "base  \u00b7  ~145 MB" in box.model_list_label.text()
        assert box.total_label.text() == "Estimated total: ~221 MB"
        assert box.destination_path_label.text() == get_hf_cache_dir()


class TestBatchLifecycle(_DialogTestCase):
    """Queue bookkeeping between begin_batch and finish_batch."""

    def test_begin_batch_locks_selection_and_downloads(self):
        dialog, _values = self._make_dialog()
        dialog.begin_batch(["tiny", "base"])

        assert dialog.stop_batch_button.isVisibleTo(dialog)
        assert not dialog.rows["small"].select_checkbox.isEnabled()
        assert not dialog.rows["small"].download_button.isEnabled()
        assert not dialog.select_all_button.isEnabled()
        assert not dialog.download_all_button.isEnabled()

    def test_progress_counts_each_model(self):
        dialog, _values = self._make_dialog()
        dialog.begin_batch(["tiny", "base"])

        dialog.set_downloading("tiny")
        assert "Downloading model 1 of 2: tiny" in dialog.message_label.text()
        assert dialog.rows["base"].badge.text() == "Queued"

        dialog.finish_download("tiny", success=True)
        dialog.set_downloading("base")
        assert "Downloading model 2 of 2: base" in dialog.message_label.text()

    def test_finish_batch_summarizes_failures_and_unlocks(self):
        dialog, _values = self._make_dialog()
        dialog.begin_batch(["tiny", "base"])
        dialog.set_downloading("tiny")
        dialog.finish_download("tiny", success=True)
        dialog.set_downloading("base")
        dialog.finish_download("base", success=False)

        dialog.finish_batch(1, 2)

        assert "failed" in dialog.message_label.text()
        assert not dialog.stop_batch_button.isVisibleTo(dialog)
        assert dialog.rows["small"].download_button.isEnabled()
        assert dialog.select_all_button.isEnabled()

    def test_stopped_queue_is_reported(self):
        dialog, _values = self._make_dialog()
        dialog.begin_batch(["tiny", "base", "small"])
        dialog.set_downloading("tiny")
        dialog.finish_download("tiny", success=True)

        dialog.finish_batch(1, 3)

        assert "Stopped" in dialog.message_label.text()
        assert not dialog.stop_batch_button.isVisibleTo(dialog)

    def test_single_download_message_is_unchanged(self):
        dialog, _values = self._make_dialog()
        dialog.set_downloading("tiny")
        assert 'Downloading "tiny"' in dialog.message_label.text()
        assert not dialog.stop_batch_button.isVisibleTo(dialog)

    def test_env_blocked_disables_selection_actions(self):
        dialog, _values = self._make_dialog(env_blocked=True)
        assert not dialog.rows["tiny"].select_checkbox.isEnabled()
        assert not dialog.select_all_button.isEnabled()
        assert not dialog.download_all_button.isEnabled()
        assert not dialog.download_selected_button.isEnabled()


class TestComponents(_DialogTestCase):
    """Optional components install from this window, not from Settings."""

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

    def test_missing_component_has_enabled_install_button(self):
        row = ComponentRowWidget("gpu-accel")
        row.update_state(
            self._info(ComponentState.NOT_INSTALLED, download_bytes=1_000_000),
            installing=False,
        )

        assert not row.install_button.isHidden()
        assert row.install_button.isEnabled()
        assert row.install_button.text() == "Install"

    def test_existing_cuda_setup_is_not_reported_missing(self):
        row = ComponentRowWidget("gpu-accel")
        row.update_state(
            self._info(
                ComponentState.EXTERNAL,
                reason="CUDA libraries are already available.",
            ),
            installing=False,
        )

        assert row.badge.text() == "Available"
        assert row.size_label.text() == "Existing setup"
        assert row.install_button.isHidden()
        assert row.remove_button.isHidden()

    def test_component_progress_and_result_reach_the_row_and_message(self):
        dialog, _values = self._make_dialog()
        if not dialog._component_rows:
            pytest.skip("no installable components on this platform")
        component_id = next(iter(dialog._component_rows))

        dialog.set_component_progress(component_id, "Downloading", 1, 2)
        dialog.finish_component_install(component_id, True, "Installed")

        assert dialog.message_label.text() == "Installed"

    def test_component_removal_asks_before_deleting(self):
        dialog, _values = self._make_dialog()
        if not dialog._component_rows:
            pytest.skip("no installable components on this platform")
        component_id = next(iter(dialog._component_rows))
        requested = []
        dialog.component_remove_requested.connect(requested.append)

        with patch.object(
            QMessageBox, "question", return_value=QMessageBox.StandardButton.No
        ):
            dialog._confirm_component_removal(component_id)
        assert requested == []

        with patch.object(
            QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes
        ):
            dialog._confirm_component_removal(component_id)
        assert requested == [component_id]

    def test_row_click_opens_the_component_profile(self):
        dialog, _values = self._make_dialog()
        if not dialog._component_rows:
            pytest.skip("no installable components on this platform")
        component_id = next(iter(dialog._component_rows))
        opened = []

        original_init = ComponentDetailsDialog.__init__

        def _init(self, requested_id, parent=None):
            opened.append(requested_id)
            original_init(self, requested_id, parent)

        with (
            patch.object(ComponentDetailsDialog, "__init__", _init),
            patch.object(ComponentDetailsDialog, "exec", return_value=0),
        ):
            dialog._component_rows[component_id].details_requested.emit(
                component_id
            )

        assert opened == [component_id]
        assert not dialog._component_rows[component_id].property("selected")

    def test_component_body_click_requests_details(self):
        row = ComponentRowWidget("gpu-accel")
        row.resize(720, 72)
        row.show()
        requested = []
        row.details_requested.connect(requested.append)

        QTest.mouseClick(row, Qt.MouseButton.LeftButton, pos=QPoint(5, 5))

        assert requested == ["gpu-accel"]
        row.close()

    def test_enter_and_space_open_component_details(self):
        row = ComponentRowWidget("meeting-agent")
        requested = []
        row.details_requested.connect(requested.append)

        QTest.keyClick(row, Qt.Key.Key_Return)
        QTest.keyClick(row, Qt.Key.Key_Space)

        assert requested == ["meeting-agent", "meeting-agent"]

    def test_action_buttons_do_not_open_component_details(self):
        row = ComponentRowWidget("gpu-accel")
        selected = []
        installs = []
        removes = []
        row.details_requested.connect(selected.append)
        row.install_clicked.connect(installs.append)
        row.remove_clicked.connect(removes.append)
        row.update_state(
            self._info(ComponentState.BROKEN, download_bytes=1_000_000),
            installing=False,
        )

        row.install_button.click()
        row.remove_button.click()

        assert installs == ["gpu-accel"]
        assert removes == ["gpu-accel"]
        assert selected == []

    def test_component_profile_renders_bundled_facts_and_links(self):
        details = get_component_details(ComponentId.MEETING_AGENT)
        popup = ComponentDetailsDialog(ComponentId.MEETING_AGENT)
        opened = []
        with patch.object(
            component_dialog_module.QDesktopServices,
            "openUrl",
            side_effect=lambda url: opened.append(url.toString()),
        ):
            popup.source_button.click()
            popup.origin_button.click()

        assert popup.name_label.text() == details.display_name
        assert popup.description_label.text() == details.description
        assert popup.fact_labels["Origin"].text() == details.origin_name
        assert popup.fact_labels["Payload"].text()
        assert popup.tradeoffs_label.text().startswith("•")
        assert popup.source_button.toolTip() == details.source_url
        assert popup.origin_button.toolTip() == details.origin_url
        assert opened == [details.source_url, PI_HOME_URL]
        popup.close()
