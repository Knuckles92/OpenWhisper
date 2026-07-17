"""Qt tests for model-tile activation and the technical profile dialog."""

import os
import types
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from services.model_catalog import get_model_details
from ui_qt.dialogs import model_details_dialog as details_module
from ui_qt.dialogs import model_manager_dialog as manager_module
from ui_qt.dialogs.model_details_dialog import ModelDetailsDialog
from ui_qt.dialogs.model_manager_dialog import ModelManagerDialog
from ui_qt.widgets.model_row_widget import ModelRowWidget


class _QtTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])


class TestModelDetailsDialog(_QtTestCase):
    """Rendering and explicit source-link behavior."""

    def test_representative_profiles_render_expected_facts(self):
        expected = {
            "base": ("OpenAI Whisper", "74 million", "Multilingual"),
            "base.en": ("OpenAI Whisper", "74 million", "English only"),
            "distil-large-v3": (
                "Distil-Whisper",
                "756 million",
                "English only",
            ),
            "turbo": ("OpenAI Whisper", "809 million", "Multilingual"),
        }
        for model_name, (family, parameters, languages) in expected.items():
            with self.subTest(model=model_name):
                dialog = ModelDetailsDialog(get_model_details(model_name))
                self.assertTrue(dialog.isModal())
                self.assertEqual(dialog.fact_labels["Family"].text(), family)
                self.assertEqual(
                    dialog.fact_labels["Parameters"].text(), parameters
                )
                self.assertEqual(
                    dialog.fact_labels["Languages"].text(), languages
                )
                self.assertIn("CTranslate2", dialog.fact_labels["Local format"].text())
                self.assertEqual(dialog.fact_labels["License"].text(), "MIT")

    def test_source_buttons_open_only_the_requested_urls(self):
        details = get_model_details("distil-medium.en")
        dialog = ModelDetailsDialog(details)
        with patch.object(
            details_module.QDesktopServices,
            "openUrl",
            return_value=True,
        ) as open_url:
            dialog.repository_button.click()
            dialog.origin_button.click()

        self.assertEqual(open_url.call_count, 2)
        self.assertEqual(
            open_url.call_args_list[0].args[0].toString(),
            details.repository_url,
        )
        self.assertEqual(
            open_url.call_args_list[1].args[0].toString(),
            details.origin_url,
        )

    def test_dialog_construction_remains_offline_under_hf_override(self):
        with patch.dict(os.environ, {"HF_HUB_OFFLINE": "1"}), patch(
            "huggingface_hub.HfApi",
            side_effect=AssertionError("network metadata must not be requested"),
        ) as hf_api:
            dialog = ModelDetailsDialog(get_model_details("tiny"))
        self.assertEqual(dialog.fact_labels["Origin"].text(), "openai/whisper-tiny")
        hf_api.assert_not_called()


class TestModelTileActivation(_QtTestCase):
    """Tile body and keyboard activation stay separate from row actions."""

    def test_tile_body_click_requests_details(self):
        row = ModelRowWidget("base")
        row.resize(720, 64)
        row.show()
        requested = []
        row.details_requested.connect(requested.append)

        QTest.mouseClick(
            row,
            Qt.MouseButton.LeftButton,
            pos=QPoint(5, 5),
        )
        self.assertEqual(requested, ["base"])
        row.close()

    def test_enter_and_space_request_details(self):
        row = ModelRowWidget("tiny")
        requested = []
        row.details_requested.connect(requested.append)

        QTest.keyClick(row, Qt.Key.Key_Return)
        QTest.keyClick(row, Qt.Key.Key_Space)
        self.assertEqual(requested, ["tiny", "tiny"])

    def test_action_buttons_do_not_request_details(self):
        row = ModelRowWidget("small")
        details_requested = []
        downloads_requested = []
        active_requested = []
        deletes_requested = []
        row.details_requested.connect(details_requested.append)
        row.download_clicked.connect(downloads_requested.append)
        row.set_active_clicked.connect(active_requested.append)
        row.delete_clicked.connect(deletes_requested.append)

        row.download_button.click()
        row.set_active_button.click()
        row.delete_button.click()

        self.assertEqual(downloads_requested, ["small"])
        self.assertEqual(active_requested, ["small"])
        self.assertEqual(deletes_requested, ["small"])
        self.assertEqual(details_requested, [])


class TestManagerDetailsRouting(_QtTestCase):
    """The manager opens the correct modal child for a selected tile."""

    def test_manager_opens_selected_model_details(self):
        fake_settings = types.SimpleNamespace(get=lambda key, default=None: "base")
        with patch.object(
            manager_module,
            "scan_cached_models",
            return_value={},
        ), patch.object(
            manager_module,
            "settings_manager",
            fake_settings,
        ), patch.object(
            manager_module,
            "is_hf_hub_offline_env_set",
            return_value=True,
        ), patch.object(manager_module, "ModelDetailsDialog") as dialog_class:
            manager = ModelManagerDialog()
            manager.rows["distil-large-v3"].details_requested.emit(
                "distil-large-v3"
            )

        shown_details = dialog_class.call_args.args[0]
        self.assertEqual(shown_details.model_name, "distil-large-v3")
        self.assertIs(dialog_class.call_args.kwargs["parent"], manager)
        dialog_class.return_value.exec.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
