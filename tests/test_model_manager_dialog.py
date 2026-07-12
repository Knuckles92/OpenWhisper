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
        self.assertFalse(row.set_active_button.isVisibleTo(dialog))

    def test_loaded_model_delete_is_disabled(self):
        dialog = self._make_dialog(
            cached={BASE_REPO: _cached(BASE_REPO, 145_000_000)},
            loaded_model="base",
        )
        row = dialog.rows["base"]
        self.assertFalse(row.delete_button.isEnabled())
        self.assertIn("In use", row.delete_button.toolTip())

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
