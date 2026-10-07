"""Settings must say when TypeSafe features cannot run for want of a key.

Every TypeSafe caller degrades to "no judgment" on purpose, so a missing key
looks exactly like a quiet meeting. These tests pin the places that break the
silence: the Fast judgments notice, the rail badge, the sensitivity-gate
warning, and the Test button that used to do nothing at all for TypeSafe.
"""
import os
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from services import credentials
from services.settings import SettingsKey, SettingsManager
from ui_qt.dialogs import settings_dialog as settings_dialog_module
from services.typesafe import OPENROUTER_ROUTE
from ui_qt.dialogs.settings_dialog import (
    MEETING_FAST,
    SettingsDialog,
    TYPESAFE_CREDENTIAL_ENV,
)

TYPESAFE_KEY = "ts-live-0123456789abcdefghij"
OPENROUTER_ENV = OPENROUTER_ROUTE.credential_env
OPENROUTER_KEY = "sk-or-v1-0123456789abcdefghij"
#: Cleared so a real key on the developer's machine cannot decide the outcome.
SHADOWING_ENV = {TYPESAFE_CREDENTIAL_ENV: "", OPENROUTER_ENV: ""}


class TypeSafeMessagingTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.manager = SettingsManager(os.path.join(self._temp.name, "settings.json"))
        self.store = credentials.CredentialStore(credentials.memory_backend)
        self._previous_store = credentials.set_store(self.store)
        self._patches = [
            patch.object(settings_dialog_module, "settings_manager", self.manager),
            patch.object(
                credentials, "env_file_path",
                lambda: os.path.join(self._temp.name, ".env"),
            ),
            patch.dict(os.environ, SHADOWING_ENV),
        ]
        for patcher in self._patches:
            patcher.start()
        os.environ.pop(TYPESAFE_CREDENTIAL_ENV, None)
        os.environ.pop(OPENROUTER_ENV, None)
        self.dialog = None

    def tearDown(self):
        if self.dialog is not None:
            self.dialog.close()
            self.dialog.deleteLater()
            self.app.processEvents()
        for patcher in reversed(self._patches):
            patcher.stop()
        credentials.set_store(self._previous_store)
        self._temp.cleanup()

    def _open(self, **settings):
        self.manager.save_all_settings(dict(settings))
        self.dialog = SettingsDialog()
        return self.dialog


class TestFastJudgmentsNotice(TypeSafeMessagingTestCase):
    def test_notice_names_the_missing_key_when_features_are_on(self):
        dialog = self._open(**{SettingsKey.TYPESAFE_ENABLED: True})
        self.assertFalse(dialog.typesafe_key_notice.isHidden())
        text = dialog.typesafe_key_notice.description_label.text()
        self.assertIn(TYPESAFE_CREDENTIAL_ENV, text)
        self.assertIn("API keys", text)

    def test_notice_explains_that_turning_switches_on_will_not_help(self):
        dialog = self._open(**{SettingsKey.TYPESAFE_ENABLED: False})
        self.assertFalse(dialog.typesafe_key_notice.isHidden())
        self.assertIn(
            "no effect", dialog.typesafe_key_notice.description_label.text()
        )

    def test_notice_disappears_once_a_key_exists(self):
        self.store.set(TYPESAFE_CREDENTIAL_ENV, TYPESAFE_KEY)
        dialog = self._open(**{SettingsKey.TYPESAFE_ENABLED: True})
        self.assertTrue(dialog.typesafe_key_notice.isHidden())

    def test_saving_a_key_clears_the_notice_without_reopening(self):
        dialog = self._open(**{SettingsKey.TYPESAFE_ENABLED: True})
        self.assertFalse(dialog.typesafe_key_notice.isHidden())
        dialog.focus_api_keys(TYPESAFE_CREDENTIAL_ENV)
        dialog.api_key_edit.setText(TYPESAFE_KEY)
        dialog._save_api_key()
        self.assertTrue(dialog.typesafe_key_notice.isHidden())

    def test_the_notice_button_opens_the_typesafe_credential(self):
        dialog = self._open(**{SettingsKey.TYPESAFE_ENABLED: True})
        dialog.open_typesafe_key_btn.click()
        self.assertEqual(
            dialog.api_key_combo.currentData(), TYPESAFE_CREDENTIAL_ENV
        )


class TestFastJudgmentsRailBadge(TypeSafeMessagingTestCase):
    def _badge(self, dialog):
        return dialog._meeting_fast_rail_value()

    def test_off_reads_off(self):
        dialog = self._open(**{SettingsKey.TYPESAFE_ENABLED: False})
        self.assertEqual(self._badge(dialog), "Off")

    def test_enabled_without_a_key_reads_no_key(self):
        dialog = self._open(**{SettingsKey.TYPESAFE_ENABLED: True})
        self.assertEqual(self._badge(dialog), "No key")

    def test_enabled_with_a_key_counts_active_features(self):
        self.store.set(TYPESAFE_CREDENTIAL_ENV, TYPESAFE_KEY)
        dialog = self._open(**{
            SettingsKey.TYPESAFE_ENABLED: True,
            SettingsKey.TYPESAFE_TOPIC_SHIFT_ENABLED: True,
            SettingsKey.TYPESAFE_VOICE_COMMANDS_ENABLED: False,
            SettingsKey.TYPESAFE_CITATIONS_ENABLED: False,
            SettingsKey.TYPESAFE_SEMANTIC_SEARCH_ENABLED: False,
            SettingsKey.TYPESAFE_QUESTION_RADAR_ENABLED: False,
            SettingsKey.TYPESAFE_HIGHLIGHTS_ENABLED: False,
        })
        self.assertEqual(self._badge(dialog), "On · 1")

    def test_the_rail_shows_the_badge(self):
        dialog = self._open(**{SettingsKey.TYPESAFE_ENABLED: True})
        dialog._refresh_rail_values()
        self.assertEqual(dialog.rail.value(MEETING_FAST), "No key")

    def test_toggling_a_feature_updates_the_badge_live(self):
        # Persisting a setting refreshes the rail, so the tiles need no
        # bespoke handler; this pins that the refresh path really runs.
        self.store.set(TYPESAFE_CREDENTIAL_ENV, TYPESAFE_KEY)
        dialog = self._open(**{
            SettingsKey.TYPESAFE_ENABLED: True,
            SettingsKey.TYPESAFE_TOPIC_SHIFT_ENABLED: False,
            SettingsKey.TYPESAFE_VOICE_COMMANDS_ENABLED: False,
            SettingsKey.TYPESAFE_CITATIONS_ENABLED: False,
            SettingsKey.TYPESAFE_SEMANTIC_SEARCH_ENABLED: False,
            SettingsKey.TYPESAFE_QUESTION_RADAR_ENABLED: False,
            SettingsKey.TYPESAFE_HIGHLIGHTS_ENABLED: False,
        })
        self.assertEqual(dialog.rail.value(MEETING_FAST), "On · no features")
        dialog.typesafe_feature_tiles["highlights"].checkbox.setChecked(True)
        self.assertEqual(dialog.rail.value(MEETING_FAST), "On · 1")
        dialog.typesafe_enabled_check.setChecked(False)
        self.assertEqual(dialog.rail.value(MEETING_FAST), "Off")

    def test_removing_a_key_flips_the_badge_back(self):
        self.store.set(TYPESAFE_CREDENTIAL_ENV, TYPESAFE_KEY)
        dialog = self._open(**{SettingsKey.TYPESAFE_ENABLED: True})
        dialog.focus_api_keys(TYPESAFE_CREDENTIAL_ENV)
        with patch.object(
            settings_dialog_module.QMessageBox, "question",
            return_value=settings_dialog_module.QMessageBox.StandardButton.Yes,
        ):
            dialog._remove_api_key()
        self.assertEqual(dialog.rail.value(MEETING_FAST), "No key")
        self.assertFalse(dialog.typesafe_key_notice.isHidden())


class TestSavedFeatureFlags(TypeSafeMessagingTestCase):
    def test_master_off_keeps_saved_choices_and_restores_them_live(self):
        flags = {
            SettingsKey.TYPESAFE_CITATIONS_ENABLED: True,
            SettingsKey.TYPESAFE_SEMANTIC_SEARCH_ENABLED: False,
            SettingsKey.TYPESAFE_QUESTION_RADAR_ENABLED: True,
            SettingsKey.TYPESAFE_HIGHLIGHTS_ENABLED: True,
        }
        self.store.set(TYPESAFE_CREDENTIAL_ENV, TYPESAFE_KEY)
        dialog = self._open(**{SettingsKey.TYPESAFE_ENABLED: False, **flags})
        for enabled in (False, True, False, True):
            dialog.typesafe_enabled_check.setChecked(enabled)
            saved = self.manager.load_all_settings()
            for feature, tile in dialog.typesafe_feature_tiles.items():
                key = f"typesafe_{feature}_enabled"
                self.assertEqual(tile.checkbox.isChecked(), flags[key])
                self.assertEqual(saved[key], flags[key])
                self.assertEqual(tile.isEnabled(), enabled)
        self.assertEqual(dialog.rail.value(MEETING_FAST), "On · 4")

    def test_unset_features_stay_unchecked_when_master_is_enabled(self):
        dialog = self._open(**{SettingsKey.TYPESAFE_ENABLED: False})
        dialog.typesafe_enabled_check.setChecked(True)
        self.assertTrue(all(not tile.checkbox.isChecked()
                            for tile in dialog.typesafe_feature_tiles.values()))


class TestTypeSafeKeyTesting(TypeSafeMessagingTestCase):
    """TypeSafe has no text-model profile, so Test used to return silently."""

    def test_test_button_verifies_through_the_typesafe_probe(self):
        dialog = self._open()
        dialog.focus_api_keys(TYPESAFE_CREDENTIAL_ENV)
        dialog.api_key_edit.setText(TYPESAFE_KEY)
        with patch.object(
            settings_dialog_module, "typesafe_verify_key",
            return_value=(True, "api.typesafe.ai accepted the key."),
        ) as verify, patch.object(
            settings_dialog_module.threading, "Thread"
        ) as thread:
            dialog._test_api_key()
            thread.assert_called_once()
            thread.call_args.kwargs["target"]()
        verify.assert_called_once_with(TYPESAFE_KEY)
        self.app.processEvents()
        self.assertIn("works", dialog.message_label.text())

    def test_a_rejected_key_is_reported(self):
        dialog = self._open()
        dialog.focus_api_keys(TYPESAFE_CREDENTIAL_ENV)
        dialog.api_key_edit.setText(TYPESAFE_KEY)
        with patch.object(
            settings_dialog_module, "typesafe_verify_key",
            return_value=(False, "api.typesafe.ai rejected the key (HTTP 401)."),
        ), patch.object(settings_dialog_module.threading, "Thread") as thread:
            dialog._test_api_key()
            thread.call_args.kwargs["target"]()
        self.app.processEvents()
        message = dialog.message_label.text()
        self.assertIn("TypeSafe API key failed", message)
        self.assertIn("401", message)

    def test_text_model_keys_still_use_the_openai_probe(self):
        dialog = self._open()
        dialog.focus_api_keys("OPENAI_API_KEY")
        dialog.api_key_edit.setText("sk-proj-abcdefghijklmnopqrstuvwxyz")
        with patch.object(
            settings_dialog_module, "verify_api_key", return_value=(True, "ok"),
        ) as verify, patch.object(
            settings_dialog_module, "typesafe_verify_key",
        ) as typesafe_verify, patch.object(
            settings_dialog_module.threading, "Thread"
        ) as thread:
            dialog._test_api_key()
            thread.call_args.kwargs["target"]()
        verify.assert_called_once()
        typesafe_verify.assert_not_called()


class TestJevConnection(TypeSafeMessagingTestCase):
    """Fast judgments can run on the OpenRouter key instead of a TypeSafe key."""

    def _provider(self):
        return self.manager.load_all_settings().get(SettingsKey.TYPESAFE_PROVIDER)

    def test_typesafe_is_selected_until_openrouter_is_chosen(self):
        dialog = self._open(**{SettingsKey.TYPESAFE_ENABLED: True})
        self.assertEqual(dialog.typesafe_route_bar.currentIndex(), 0)
        self.assertIn("api.typesafe.ai", dialog.typesafe_destination_label.text())
        self.assertIsNone(self._provider())

    def test_an_openrouter_key_alone_offers_a_one_click_switch(self):
        self.store.set(OPENROUTER_ENV, OPENROUTER_KEY)
        dialog = self._open(**{SettingsKey.TYPESAFE_ENABLED: True})
        self.assertFalse(dialog.typesafe_key_notice.isHidden())
        self.assertFalse(dialog.typesafe_switch_route_btn.isHidden())
        self.assertEqual(dialog.typesafe_switch_route_btn.text(), "Use OpenRouter")
        self.assertIn("OpenRouter API key can run the same model",
                      dialog.typesafe_key_notice.description_label.text())

        dialog.typesafe_switch_route_btn.click()

        self.assertEqual(self._provider(), "openrouter")
        self.assertEqual(dialog.typesafe_route_bar.currentIndex(), 1)
        self.assertTrue(dialog.typesafe_key_notice.isHidden())
        self.assertIn("openrouter.ai", dialog.typesafe_destination_label.text())
        self.assertIn("passes them to TypeSafe", dialog.typesafe_destination_label.text())
        # Semantic topic changes are on by default once the master switch is.
        self.assertEqual(dialog.rail.value(MEETING_FAST), "On · 1 · OpenRouter")

    def test_no_switch_is_offered_when_neither_route_has_a_key(self):
        dialog = self._open(**{SettingsKey.TYPESAFE_ENABLED: True})
        self.assertTrue(dialog.typesafe_switch_route_btn.isHidden())

    def test_choosing_openrouter_without_its_key_names_that_key(self):
        self.store.set(TYPESAFE_CREDENTIAL_ENV, TYPESAFE_KEY)
        dialog = self._open(**{SettingsKey.TYPESAFE_ENABLED: True})
        self.assertTrue(dialog.typesafe_key_notice.isHidden())
        dialog.typesafe_route_bar.buttons[1].click()
        self.assertEqual(self._provider(), "openrouter")
        self.assertFalse(dialog.typesafe_key_notice.isHidden())
        self.assertEqual(dialog.typesafe_key_notice.title_label.text(),
                         "No OpenRouter API key")
        # The TypeSafe key still works, so switching back is offered.
        self.assertEqual(dialog.typesafe_switch_route_btn.text(), "Use TypeSafe")
        self.assertEqual(dialog.rail.value(MEETING_FAST), "No key")
        dialog.open_typesafe_key_btn.click()
        self.assertEqual(dialog.api_key_combo.currentData(), OPENROUTER_ENV)

    def test_segments_show_which_routes_have_a_key(self):
        self.store.set(OPENROUTER_ENV, OPENROUTER_KEY)
        dialog = self._open(**{SettingsKey.TYPESAFE_PROVIDER: "openrouter"})
        details = [button._detail.text() for button in dialog.typesafe_route_bar.buttons]
        self.assertEqual(details, ["Direct · no key", "Same model · key set"])
        self.assertIn("No TypeSafe account needed",
                      dialog.typesafe_route_tile.description_label.text())

    def test_saving_an_openrouter_key_updates_the_connection_live(self):
        dialog = self._open(**{SettingsKey.TYPESAFE_ENABLED: True,
                               SettingsKey.TYPESAFE_PROVIDER: "openrouter"})
        self.assertFalse(dialog.typesafe_key_notice.isHidden())
        dialog.focus_api_keys(OPENROUTER_ENV)
        dialog.api_key_edit.setText(OPENROUTER_KEY)
        dialog._save_api_key()
        self.assertTrue(dialog.typesafe_key_notice.isHidden())
        self.assertEqual(dialog.typesafe_route_bar.buttons[1]._detail.text(),
                         "Same model · key set")

    def test_test_button_verifies_the_chosen_route(self):
        self.store.set(OPENROUTER_ENV, OPENROUTER_KEY)
        dialog = self._open(**{SettingsKey.TYPESAFE_PROVIDER: "openrouter"})
        with patch.object(
            settings_dialog_module, "typesafe_verify_key",
            return_value=(True, "openrouter.ai accepted the key and typesafe/jev-1.13 answered in 0.15 s."),
        ) as verify, patch.object(settings_dialog_module.threading, "Thread") as thread:
            dialog.typesafe_route_test_btn.click()
            self.assertFalse(dialog.typesafe_route_test_btn.isEnabled())
            thread.call_args.kwargs["target"]()
        verify.assert_called_once_with(OPENROUTER_KEY, route=OPENROUTER_ROUTE)
        self.app.processEvents()
        self.assertTrue(dialog.typesafe_route_test_btn.isEnabled())
        self.assertIn("typesafe/jev-1.13 answered", dialog.typesafe_route_status.text())
        self.assertEqual(dialog.typesafe_route_status.property("tone"), "success")

    def test_testing_without_a_key_says_so_without_a_request(self):
        dialog = self._open(**{SettingsKey.TYPESAFE_PROVIDER: "openrouter"})
        with patch.object(settings_dialog_module, "typesafe_verify_key") as verify:
            dialog.typesafe_route_test_btn.click()
        verify.assert_not_called()
        self.assertIn("No OpenRouter API key to test", dialog.typesafe_route_status.text())
        self.assertEqual(dialog.typesafe_route_status.property("tone"), "warning")

    def test_a_result_for_the_route_left_behind_is_dropped(self):
        self.store.set(TYPESAFE_CREDENTIAL_ENV, TYPESAFE_KEY)
        dialog = self._open()
        with patch.object(
            settings_dialog_module, "typesafe_verify_key",
            return_value=(False, "api.typesafe.ai rejected the key (HTTP 401)."),
        ), patch.object(settings_dialog_module.threading, "Thread") as thread:
            dialog.typesafe_route_test_btn.click()
            dialog.typesafe_route_bar.buttons[1].click()
            thread.call_args.kwargs["target"]()
        self.app.processEvents()
        self.assertTrue(dialog.typesafe_route_status.isHidden())
        self.assertTrue(dialog.typesafe_route_test_btn.isEnabled())

    def test_api_keys_page_says_openrouter_can_run_fast_judgments(self):
        dialog = self._open()
        dialog.focus_api_keys(OPENROUTER_ENV)
        self.assertIn("Fast judgments", dialog.api_key_uses_caption.text())
        dialog.focus_api_keys(TYPESAFE_CREDENTIAL_ENV)
        uses = dialog.api_key_uses_caption.text()
        self.assertIn("OpenRouter key", uses)
        self.assertNotIn("sensitive-dictation", uses)


if __name__ == "__main__":
    unittest.main()
