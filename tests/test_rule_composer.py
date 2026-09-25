"""Learned-rule composer: the activity strip, dictation lifecycle, and copy."""
import os
import threading
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QDialog, QListWidget, QWidget, QVBoxLayout

from ui_qt.dialogs.settings_dialog import CLEANUP_PROFILES, CLEANUP_RULES, SettingsDialog
from ui_qt.widgets.rule_activity import (
    IDLE,
    LISTENING,
    POLISHING,
    TRANSCRIBING,
    ItemGlow,
    LevelTrace,
    RuleActivityStrip,
    level_to_height,
)


class _Clock:
    """A QElapsedTimer stand-in whose time the test sets."""

    def __init__(self, ms: int = 0):
        self.ms = ms
        self.valid = False

    def start(self):
        self.valid = True

    def restart(self):
        self.valid = True

    def elapsed(self):
        return self.ms

    def isValid(self):
        return self.valid


class _FakeRecorder:
    """Stands in for AudioRecorder so no microphone is opened."""

    instances = []

    @staticmethod
    def get_input_devices():
        return [(3, "USB microphone")]

    def __init__(self, device_id=None, output_file=None):
        self.device_id = device_id
        self.output_file = output_file
        self.level_callback = None
        self.canceled = 0
        self.cleaned = 0
        self.is_recording = False
        self.last_start_error = None
        _FakeRecorder.instances.append(self)

    def set_audio_level_callback(self, callback):
        self.level_callback = callback

    def start_recording(self):
        self.is_recording = True
        return True

    def stop_recording(self):
        self.is_recording = False
        return True

    def wait_for_stop_completion(self, timeout=None):
        return True

    def has_recording_data(self):
        return True

    def save_recording(self, filename=None):
        with open(filename, "wb") as handle:
            handle.write(b"RIFF")
        return True

    def clear_recording_data(self):
        pass

    def cancel_recording(self):
        self.canceled += 1
        self.is_recording = False

    def cleanup(self):
        self.cleaned += 1
        self.is_recording = False


class _FakeReviewDialog:
    """Records how the review dialog was opened and answers for the user."""

    calls = []
    answer = QDialog.DialogCode.Rejected
    rule = ""

    def __init__(self, rule, original=None, notice=None, parent=None, dictated=False):
        _FakeReviewDialog.calls.append(
            {"rule": rule, "original": original, "notice": notice, "dictated": dictated}
        )

    def exec(self):
        return _FakeReviewDialog.answer

    def rule_text(self):
        return _FakeReviewDialog.rule


def _pump_until(predicate, timeout: float = 3.0) -> bool:
    app = QApplication.instance()
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    app.processEvents()
    return predicate()


class TestLevelTrace(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_level_scale_is_logarithmic_between_floor_and_ceiling(self):
        self.assertEqual(level_to_height(0.0), 0.0)
        self.assertEqual(level_to_height(10 ** (-60 / 20)), 0.0)
        self.assertAlmostEqual(level_to_height(10 ** (-36 / 20)), 0.5)
        self.assertEqual(level_to_height(10 ** (-12 / 20)), 1.0)
        self.assertEqual(level_to_height(1.0), 1.0)
        heights = [level_to_height(v) for v in (0.002, 0.01, 0.05, 0.2)]
        self.assertEqual(heights, sorted(heights))

    def test_each_step_banks_the_loudest_level_heard(self):
        trace = LevelTrace()
        trace._clock = _Clock()
        trace.start_listening()
        trace.push_level(0.02)
        trace.push_level(0.1)
        trace.push_level(0.05)
        trace._clock.ms = 60
        trace.advance()
        self.assertEqual(trace.bar_heights(), [level_to_height(0.1)])
        # A step with no level from the microphone is silence, not a repeat.
        trace._clock.ms = 120
        trace.advance()
        self.assertEqual(trace.bar_heights(), [level_to_height(0.1), 0.0])

    def test_holding_stops_the_trace_from_taking_levels(self):
        trace = LevelTrace()
        trace._clock = _Clock()
        trace.start_listening()
        trace.push_level(0.1)
        trace._clock.ms = 60
        trace.advance()
        trace.hold(TRANSCRIBING)
        trace.push_level(0.3)
        trace._clock.ms = 600
        trace.advance()
        self.assertEqual(len(trace.bar_heights()), 1)


class TestRuleActivityStrip(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.host = QWidget()
        layout = QVBoxLayout(self.host)
        self.strip = RuleActivityStrip(60)
        layout.addWidget(self.strip)
        self.host.resize(640, 120)
        self.host.show()
        self.app.processEvents()

    def tearDown(self):
        self.host.close()
        self.host.deleteLater()

    def test_listening_opens_at_once_and_finish_closes(self):
        self.assertTrue(self.strip.isHidden())
        self.strip.show_listening("USB microphone")
        self.assertFalse(self.strip.isHidden())
        self.assertEqual(self.strip.state, LISTENING)
        self.assertEqual(self.strip.title_label.text(), "Listening")
        self.assertEqual(self.strip.detail_label.text(), "USB microphone")
        self.assertEqual(self.strip.time_label.text(), "0:00 / 1:00")
        self.strip.finish(animate=False)
        self.assertTrue(self.strip.isHidden())
        self.assertEqual(self.strip.state, IDLE)

    def test_opened_strip_grows_to_its_full_height(self):
        self.strip.show_listening("USB microphone")
        self.assertTrue(
            _pump_until(lambda: self.strip.height() == self.strip.sizeHint().height())
        )

    def test_work_that_ends_at_once_never_opens_the_strip(self):
        self.strip.show_polishing("OpenRouter · model")
        self.assertTrue(self.strip.isHidden())
        self.strip.finish()
        QTest.qWait(250)
        self.assertTrue(self.strip.isHidden())
        self.assertEqual(self.strip.state, IDLE)

    def test_slow_work_opens_after_a_short_delay(self):
        self.strip.show_polishing("OpenRouter · model")
        self.assertTrue(_pump_until(lambda: not self.strip.isHidden(), 1.0))
        self.assertEqual(self.strip.state, POLISHING)
        self.assertEqual(self.strip.title_label.text(), "Polishing with AI")

    def test_transcribing_follows_listening_without_closing(self):
        self.strip.show_listening("USB microphone")
        self.strip.show_transcribing("On this computer · Parakeet")
        self.assertFalse(self.strip.isHidden())
        self.assertEqual(self.strip.state, TRANSCRIBING)
        self.assertTrue(self.strip.time_label.isHidden())

    def test_clock_turns_amber_near_the_automatic_stop(self):
        self.strip.show_listening("USB microphone")
        self.strip._listen_clock = _Clock(51_000)
        self.strip._listen_clock.start()
        self.strip._update_clock()
        self.assertEqual(self.strip.time_label.text(), "0:51 / 1:00")
        self.assertTrue(self.strip.time_label.property("nearCap"))


class TestItemGlow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_flash_washes_the_row_then_fades_away(self):
        view = QListWidget()
        view.addItems(["First rule", "Second rule"])
        view.resize(320, 200)
        view.show()
        glow = ItemGlow(view)
        glow._anim.setDuration(60)
        glow.flash(view.item(1))
        self.assertTrue(glow.isVisible())
        self.assertEqual(glow.strength, 1.0)
        self.assertTrue(_pump_until(lambda: glow.isHidden(), 2.0))
        view.close()
        view.deleteLater()


class TestRuleComposerFlow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        _FakeRecorder.instances.clear()
        _FakeReviewDialog.calls.clear()
        _FakeReviewDialog.answer = QDialog.DialogCode.Rejected
        _FakeReviewDialog.rule = ""
        for target, replacement in (
            ("ui_qt.dialogs.settings_dialog.AudioRecorder", _FakeRecorder),
            ("ui_qt.dialogs.settings_dialog.CleanupRuleDialog", _FakeReviewDialog),
            (
                "services.transcript_cleanup.polish_cleanup_rule",
                lambda raw, **_kw: ("Always write Kubernetes with a capital K.", None),
            ),
        ):
            patcher = patch(target, replacement)
            patcher.start()
            # Undone even when setUp fails, so the fakes never leak.
            self.addCleanup(patcher.stop)
        with patch.object(SettingsDialog, "_load_settings", lambda self: None):
            self.dialog = SettingsDialog()
        self.addCleanup(self.dialog.deleteLater)
        self.addCleanup(self.dialog.close)
        self.dialog.transcript_cleanup_check.setChecked(True)
        self.dialog.on_dictation_transcribe = lambda _path: "capitalize kubernetes"
        self.dialog.get_meeting_active = lambda: False
        self.dialog.show()
        self.dialog.select_destination(CLEANUP_RULES)
        self.app.processEvents()

    def _start_dictation(self) -> _FakeRecorder:
        QTest.mouseClick(self.dialog.cleanup_rule_mic_btn, Qt.MouseButton.LeftButton)
        self.assertEqual(self.dialog._rule_dictation_state, "recording")
        return _FakeRecorder.instances[-1]

    def test_dictating_shows_a_live_strip_and_a_red_stop(self):
        recorder = self._start_dictation()
        button = self.dialog.cleanup_rule_mic_btn
        self.assertEqual(button.text(), "Stop")
        self.assertTrue(button.property("recording"))
        self.assertEqual(self.dialog.cleanup_rule_activity.state, LISTENING)
        # Levels arrive on the audio thread and reach the trace queued.
        worker = threading.Thread(target=recorder.level_callback, args=(0.2,))
        worker.start()
        worker.join()
        trace = self.dialog.cleanup_rule_activity.trace
        self.assertTrue(_pump_until(lambda: trace._peak == 0.2))

    def test_escape_discards_the_take_and_keeps_settings_open(self):
        recorder = self._start_dictation()
        QTest.keyClick(self.dialog, Qt.Key.Key_Escape)
        self.assertEqual(recorder.canceled, 1)
        self.assertEqual(self.dialog._rule_dictation_state, "idle")
        self.assertTrue(self.dialog.isVisible())
        self.assertEqual(self.dialog.cleanup_rule_mic_btn.text(), "Dictate")
        self.assertFalse(self.dialog.cleanup_rule_mic_btn.property("recording"))

    def test_closing_settings_mid_take_leaves_no_stale_stop_button(self):
        # Settings is a reused window, so this state would greet the next open.
        recorder = self._start_dictation()
        self.dialog.close()
        self.assertGreaterEqual(recorder.cleaned, 1)
        self.assertIsNone(self.dialog._rule_recorder)
        self.assertEqual(self.dialog._rule_dictation_state, "idle")
        self.assertEqual(self.dialog.cleanup_rule_mic_btn.text(), "Dictate")
        self.assertTrue(self.dialog.cleanup_rule_activity.isHidden())

    def test_dictated_words_wait_in_the_input_when_the_review_is_canceled(self):
        self._start_dictation()
        QTest.mouseClick(self.dialog.cleanup_rule_mic_btn, Qt.MouseButton.LeftButton)
        self.assertTrue(_pump_until(lambda: _FakeReviewDialog.calls))
        call = _FakeReviewDialog.calls[-1]
        self.assertTrue(call["dictated"])
        self.assertEqual(call["original"], "capitalize kubernetes")
        self.assertEqual(call["rule"], "Always write Kubernetes with a capital K.")
        self.assertIsNone(call["notice"])
        self.assertEqual(self.dialog.cleanup_rule_input.text(), "capitalize kubernetes")
        self.assertEqual(self.dialog.cleanup_rules_list.count(), 0)
        self.assertFalse(self.dialog._rule_polishing)

    def test_dictation_adds_to_words_already_typed(self):
        self.dialog.cleanup_rule_input.setText("Always")
        self._start_dictation()
        QTest.mouseClick(self.dialog.cleanup_rule_mic_btn, Qt.MouseButton.LeftButton)
        self.assertTrue(_pump_until(lambda: _FakeReviewDialog.calls))
        self.assertEqual(
            _FakeReviewDialog.calls[-1]["original"], "Always capitalize kubernetes"
        )

    def test_saved_rule_joins_the_library_and_glows(self):
        _FakeReviewDialog.answer = QDialog.DialogCode.Accepted
        _FakeReviewDialog.rule = "Always write Kubernetes with a capital K."
        self.dialog.cleanup_rule_input.setText("capitalize kubernetes")
        QTest.mouseClick(self.dialog.cleanup_rule_add_btn, Qt.MouseButton.LeftButton)
        self.assertTrue(_pump_until(lambda: self.dialog.cleanup_rules_list.count() == 1))
        self.assertFalse(_FakeReviewDialog.calls[-1]["dictated"])
        self.assertEqual(self.dialog.cleanup_rule_input.text(), "")
        self.assertTrue(self.dialog._rule_glow.isVisible())
        self.assertEqual(self.dialog.rail.value(CLEANUP_RULES), "1 rule")

    def test_typed_rule_notice_says_written_when_polish_fails(self):
        with patch(
            "services.transcript_cleanup.polish_cleanup_rule",
            lambda raw, **_kw: (raw, "cleanup unavailable"),
        ):
            self.dialog.cleanup_rule_input.setText("keep acronyms uppercase")
            QTest.mouseClick(self.dialog.cleanup_rule_add_btn, Qt.MouseButton.LeftButton)
            self.assertTrue(_pump_until(lambda: _FakeReviewDialog.calls))
        self.assertIn("saved as written", _FakeReviewDialog.calls[-1]["notice"])

    def test_duplicate_rule_is_a_notice_and_starts_no_work(self):
        self.dialog.cleanup_rules_list.addItem("Keep acronyms uppercase.")
        self.dialog.cleanup_rule_input.setText("keep acronyms uppercase.")
        QTest.mouseClick(self.dialog.cleanup_rule_add_btn, Qt.MouseButton.LeftButton)
        status = self.dialog.cleanup_rule_status
        self.assertFalse(status.isHidden())
        self.assertEqual(status.text(), "That rule already exists.")
        self.assertFalse(self.dialog._rule_polishing)
        self.assertTrue(self.dialog.cleanup_rule_activity.isHidden())
        # Editing the words clears the notice.
        QTest.keyClick(self.dialog.cleanup_rule_input, Qt.Key.Key_X)
        self.assertTrue(status.isHidden())

    def test_rail_counts_in_the_singular(self):
        self.dialog.cleanup_rules_list.addItem("Keep acronyms uppercase.")
        self.dialog._refresh_rail_values()
        self.assertEqual(self.dialog.rail.value(CLEANUP_RULES), "1 rule")
        self.dialog.cleanup_rules_list.addItem("Spell out numbers under ten.")
        self.dialog._refresh_rail_values()
        self.assertEqual(self.dialog.rail.value(CLEANUP_RULES), "2 rules")
        self.assertRegex(self.dialog.rail.value(CLEANUP_PROFILES), r"^\d+ profiles?$")


if __name__ == "__main__":
    unittest.main()
