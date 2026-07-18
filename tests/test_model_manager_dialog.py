"""Qt tests for the Model Manager dialog and its model rows."""
import os
import types
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QMessageBox

from services.hf_access import CachedModelInfo
from ui_qt.dialogs import model_manager_dialog as dialog_module
from ui_qt.dialogs.model_manager_dialog import ModelManagerDialog
from ui_qt.widgets import Button


def _cached(repo_id, size_bytes):
    return CachedModelInfo(
        repo_id=repo_id,
        size_bytes=size_bytes,
        path=f"/hub/models--{repo_id.replace('/', '--')}",
        revision_hashes=("abc",),
    )


BASE_REPO = "Systran/faster-whisper-base"
TINY_REPO = "Systran/faster-whisper-tiny"


class _DialogTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _make_dialog(
        self,
        cached=None,
        active_model="base",
        loaded_model=None,
        env_blocked=False,
    ):
        fake_settings = types.SimpleNamespace(
            get=lambda key, default=None: active_model
        )
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
        ]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)
        return ModelManagerDialog(get_loaded_model=lambda: loaded_model)


class TestModelRows(_DialogTestCase):
    """Per-row status, size, and action availability."""

    def test_catalog_excludes_auto(self):
        dialog = self._make_dialog()
        self.assertNotIn("auto", dialog.rows)
        self.assertIn("base", dialog.rows)

    def test_uncached_row_offers_download_with_estimate(self):
        dialog = self._make_dialog()
        row = dialog.rows["tiny"]
        self.assertTrue(row.download_button.isVisibleTo(dialog))
        self.assertFalse(row.delete_button.isVisibleTo(dialog))
        self.assertFalse(row.set_active_button.isVisibleTo(dialog))
        self.assertEqual(row.badge.text(), "Not downloaded")
        self.assertEqual(row.size_label.text(), "~76 MB")

    def test_cached_row_shows_real_size_and_delete(self):
        dialog = self._make_dialog(
            cached={TINY_REPO: _cached(TINY_REPO, 76_000_000)}
        )
        row = dialog.rows["tiny"]
        self.assertFalse(row.download_button.isVisibleTo(dialog))
        self.assertTrue(row.delete_button.isVisibleTo(dialog))
        self.assertTrue(row.delete_button.isEnabled())
        self.assertTrue(row.set_active_button.isVisibleTo(dialog))
        self.assertEqual(row.badge.text(), "Downloaded")
        self.assertEqual(row.size_label.text(), "76 MB")

    def test_active_cached_row_hides_set_active(self):
        dialog = self._make_dialog(
            cached={BASE_REPO: _cached(BASE_REPO, 145_000_000)},
            active_model="base",
        )
        row = dialog.rows["base"]
        self.assertEqual(row.badge.text(), "Active")
        self.assertTrue(row.property("active"))
        self.assertFalse(row.set_active_button.isVisibleTo(dialog))
        self.assertFalse(dialog.rows["tiny"].property("active"))

    def test_loaded_model_delete_is_disabled(self):
        dialog = self._make_dialog(
            cached={BASE_REPO: _cached(BASE_REPO, 145_000_000)},
            loaded_model="base",
        )
        row = dialog.rows["base"]
        self.assertFalse(row.delete_button.isEnabled())
        self.assertIn("In use", row.delete_button.toolTip())

    def test_refresh_moves_delete_lock_when_loaded_model_changes(self):
        """After a Set Active reload, Delete must follow the newly loaded model."""
        loaded = {"name": "base"}
        fake_settings = types.SimpleNamespace(get=lambda key, default=None: "tiny")
        patchers = [
            patch.object(
                dialog_module,
                "scan_cached_models",
                return_value={
                    BASE_REPO: _cached(BASE_REPO, 145_000_000),
                    TINY_REPO: _cached(TINY_REPO, 76_000_000),
                },
            ),
            patch.object(dialog_module, "settings_manager", fake_settings),
            patch.object(
                dialog_module, "is_hf_hub_offline_env_set", return_value=False
            ),
        ]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)

        dialog = ModelManagerDialog(get_loaded_model=lambda: loaded["name"])
        self.assertFalse(dialog.rows["base"].delete_button.isEnabled())
        self.assertTrue(dialog.rows["tiny"].delete_button.isEnabled())

        loaded["name"] = "tiny"
        dialog.refresh()
        self.assertTrue(dialog.rows["base"].delete_button.isEnabled())
        self.assertFalse(dialog.rows["tiny"].delete_button.isEnabled())

    def test_stats_count_and_disk_usage(self):
        dialog = self._make_dialog(
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
        dialog = self._make_dialog()
        dialog.set_downloading("tiny")

        self.assertEqual(dialog.rows["tiny"].badge.text(), "Downloading…")
        self.assertFalse(dialog.rows["tiny"].download_button.isEnabled())
        # Only one download at a time: other rows' Download disabled too.
        self.assertFalse(dialog.rows["small"].download_button.isEnabled())

        dialog.finish_download("tiny", success=True)
        self.assertTrue(dialog.rows["small"].download_button.isEnabled())

    def test_failed_download_reports_in_message(self):
        dialog = self._make_dialog()
        dialog.set_downloading("tiny")
        dialog.finish_download("tiny", success=False)
        self.assertIn("failed", dialog.message_label.text())


class TestEnvBlocked(_DialogTestCase):
    """HF_HUB_OFFLINE disables downloads but not deletion."""

    def test_banner_shown_and_downloads_disabled(self):
        dialog = self._make_dialog(
            cached={BASE_REPO: _cached(BASE_REPO, 145_000_000)},
            env_blocked=True,
        )
        self.assertTrue(dialog.env_banner.isVisibleTo(dialog))
        self.assertFalse(dialog.rows["tiny"].download_button.isEnabled())
        self.assertTrue(dialog.rows["base"].delete_button.isEnabled())


class TestFilter(_DialogTestCase):
    """Filter box hides non-matching rows and shows the empty state."""

    def test_filter_matches_name_and_repo(self):
        dialog = self._make_dialog()
        dialog.filter_edit.setText("tiny")
        self.assertTrue(dialog.rows["tiny"].isVisibleTo(dialog))
        self.assertTrue(dialog.rows["tiny.en"].isVisibleTo(dialog))
        self.assertFalse(dialog.rows["base"].isVisibleTo(dialog))

    def test_no_match_shows_empty_state(self):
        dialog = self._make_dialog()
        dialog.filter_edit.setText("no-such-model")
        self.assertTrue(dialog.empty_label.isVisibleTo(dialog))
        dialog.filter_edit.setText("")
        self.assertFalse(dialog.empty_label.isVisibleTo(dialog))

    def test_status_filter_downloaded_only(self):
        dialog = self._make_dialog(
            cached={BASE_REPO: _cached(BASE_REPO, 145_000_000)}
        )
        dialog.status_filter_combo.setCurrentIndex(
            dialog.status_filter_combo.findData("downloaded")
        )
        self.assertTrue(dialog.rows["base"].isVisibleTo(dialog))
        self.assertFalse(dialog.rows["tiny"].isVisibleTo(dialog))

    def test_status_filter_not_downloaded_only(self):
        dialog = self._make_dialog(
            cached={BASE_REPO: _cached(BASE_REPO, 145_000_000)}
        )
        dialog.status_filter_combo.setCurrentIndex(
            dialog.status_filter_combo.findData("not_downloaded")
        )
        self.assertFalse(dialog.rows["base"].isVisibleTo(dialog))
        self.assertTrue(dialog.rows["tiny"].isVisibleTo(dialog))

    def test_status_filter_combines_with_search(self):
        dialog = self._make_dialog(
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
        dialog = self._make_dialog(
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
        dialog = self._make_dialog()
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
        dialog = self._make_dialog(active_model="medium")
        self.assertNotEqual(self._row_order(dialog)[0], "medium")
        # Same ordering as size within the not-downloaded group: tiny first.
        self.assertEqual(self._row_order(dialog)[0], "tiny")

    def test_downloaded_first_groups_cached_models(self):
        dialog = self._make_dialog(
            cached={BASE_REPO: _cached(BASE_REPO, 145_000_000)}
        )
        dialog.sort_combo.setCurrentIndex(
            dialog.sort_combo.findData("downloaded")
        )
        self.assertEqual(self._row_order(dialog)[0], "base")

    def test_smallest_first_uses_catalog_estimates(self):
        dialog = self._make_dialog(active_model="medium")
        dialog.sort_combo.setCurrentIndex(dialog.sort_combo.findData("size"))
        self.assertEqual(self._row_order(dialog)[0], "tiny")

    def test_name_sort_is_alphabetical(self):
        dialog = self._make_dialog()
        dialog.sort_combo.setCurrentIndex(dialog.sort_combo.findData("name"))
        order = self._row_order(dialog)
        self.assertEqual(order, sorted(order, key=str.casefold))


class TestActions(_DialogTestCase):
    """Row actions route through the dialog callbacks."""

    def test_download_click_invokes_callback(self):
        dialog = self._make_dialog()
        requested = []
        dialog.on_download_requested = requested.append
        dialog.rows["tiny"].download_button.click()
        self.assertEqual(requested, ["tiny"])

    def test_delete_confirm_default_no_does_nothing(self):
        dialog = self._make_dialog(
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
        dialog = self._make_dialog(
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

    def test_set_active_invokes_callback(self):
        dialog = self._make_dialog(
            cached={TINY_REPO: _cached(TINY_REPO, 76_000_000)},
            active_model="base",
        )
        requested = []
        dialog.on_set_active_requested = requested.append
        dialog.rows["tiny"].set_active_button.click()
        self.assertEqual(requested, ["tiny"])


if __name__ == "__main__":
    unittest.main()
