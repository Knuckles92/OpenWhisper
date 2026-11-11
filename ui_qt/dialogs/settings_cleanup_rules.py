"""Settings → Learned rules: teach, dictate, edit and delete cleanup rules.

A typed or dictated instruction is polished into a rule by the AI cleanup
model and reviewed before it is saved. Polishing, dictation transcription
and microphone levels run off the Qt thread and come back through a
session-lived relay, so no worker or audio thread ever holds or emits on the
window.
"""

from __future__ import annotations

import itertools
import os
import tempfile
import threading
from typing import Callable, Optional

from PyQt6.QtCore import QObject, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import QDialog, QHBoxLayout, QLabel, QLineEdit, QListWidget, QVBoxLayout

from config import config
from services.recorder import AudioRecorder
from services.settings import (
    resolve_transcript_cleanup_model,
    resolve_transcript_cleanup_provider,
    resolve_transcript_cleanup_reasoning,
    resolve_transcript_cleanup_rules,
)
from services.text_llm import profile_display_name
from ui_qt.dialogs import settings_microphones
from ui_qt.dialogs.cleanup_rule_dialog import CleanupRuleDialog
from ui_qt.utils.icons import design_icon
from ui_qt.utils.restyle import set_style_property
from ui_qt.widgets import Button, InfoTile, PrimaryButton
from ui_qt.widgets.buttons import neutral_button
from ui_qt.widgets.empty_state import EmptyState
from ui_qt.widgets.rule_activity import ItemGlow, RuleActivityStrip

#: A dictated rule stops recording on its own after this long.
RULE_DICTATION_CAP_MS = 60_000

#: The section's widgets the dialog also exposes by name, for deep links,
#: search reveals and tests (see settings_metadata.CONTROL_DESTINATIONS).
CONTROLS = (
    "cleanup_rules_composer_tile",
    "cleanup_rule_input",
    "cleanup_rule_mic_btn",
    "cleanup_rule_add_btn",
    "cleanup_rule_activity",
    "cleanup_rule_status",
    "cleanup_rules_library_tile",
    "cleanup_rules_count",
    "cleanup_rules_list",
    "cleanup_rules_empty",
    "cleanup_rule_edit_btn",
    "cleanup_rule_delete_btn",
)


class _Relay(QObject):
    """Brings worker results and microphone levels to the Qt thread.

    It lives for the session, so a worker or the rule recorder's audio thread
    never holds or emits on a window. Holding one let the thread drop the
    last reference and delete the window off the Qt thread, and emitting on
    one raced it closing; both crash the process. Qt drops a deleted
    section's connections.
    """

    cleanup_rule_polished = pyqtSignal(int, str, str, str)
    rule_dictation_finished = pyqtSignal(int, str, str)
    rule_dictation_level = pyqtSignal(int, float)


_relay: Optional[_Relay] = None
#: Request tokens, unique across windows because they share the relay.
_tokens = itertools.count(1)


def _thread_relay() -> _Relay:
    global _relay
    if _relay is None:
        _relay = _Relay()
    return _relay


def rule_count_label(rule_count: int) -> str:
    if rule_count == 0:
        return "No rules"
    return f"{rule_count} rule{'' if rule_count == 1 else 's'}"


def _rule_capture_problem(recorder, audio_path: str) -> Optional[str]:
    """Why a stopped rule dictation can't be transcribed, or None once it's saved."""
    if not recorder.wait_for_stop_completion():
        return "Recording did not finish stopping; audio kept for recovery."
    if getattr(recorder, "last_capture_error", None):
        return str(recorder.last_capture_error)
    if not recorder.has_recording_data():
        return "No audio was captured."
    if not recorder.save_recording(audio_path):
        return "Couldn't save the dictation audio."
    return None


def _remove_dictation_audio(audio_path: str) -> None:
    try:
        if os.path.exists(audio_path):
            os.remove(audio_path)
    except OSError:
        pass


def _transcribe_rule_dictation(recorder, transcribe, audio_path: str):
    """Stop, save and transcribe a rule dictation; returns ``(text, error)``.

    Runs on a worker thread. The recording and its audio file are discarded
    either way.
    """
    text = ""
    error = ""
    try:
        recorder.stop_recording()
        problem = _rule_capture_problem(recorder, audio_path)
        if problem is not None:
            error = problem
        else:
            text = transcribe(audio_path) or ""
            if not text.strip():
                error = "Nothing was transcribed."
    except Exception as exc:
        error = str(exc) or "Transcription failed."
    finally:
        recorder.clear_recording_data()
        _remove_dictation_audio(audio_path)
    return text, error


class CleanupRulesSection(QObject):
    """Builds the Learned rules page and runs its composer.

    The dialog hands in what the page needs from the rest of the window:
    whether AI cleanup is on, how to save the rules, the dictation engine and
    where it runs, whether a meeting holds the microphone, and whether the
    window is still showing.
    """

    def __init__(
        self,
        parent=None,
        *,
        load_settings: Callable[[], dict],
        settings_snapshot: Callable[[], dict],
        cleanup_enabled: Callable[[], bool],
        persist_rules: Callable[[list], object],
        transcriber: Callable[[], Optional[Callable[[str], str]]],
        meeting_active: Callable[[], bool],
        transcribe_detail: Callable[[], str],
        is_visible: Callable[[], bool],
        ensure_built: Callable[[], object] = lambda: None,
    ):
        super().__init__(parent)
        self._ensure_built = ensure_built
        self._load_settings = load_settings
        self._settings_snapshot = settings_snapshot
        self._cleanup_enabled = cleanup_enabled
        self._persist_rules = persist_rules
        self._transcriber = transcriber
        self._meeting_active = meeting_active
        self._transcribe_detail = transcribe_detail
        self._is_visible = is_visible
        self._polishing = False
        # Whether the words being polished came (at least partly) from
        # dictation, so the review dialog can say "said" instead of "typed".
        self._polish_dictated = False
        self._dictation_state = "idle"
        self._recorder: Optional[AudioRecorder] = None
        self._recorder_device: tuple = ()
        # The request each relayed result must match; 0 matches none.
        self._level_token = 0
        self._polish_token = 0
        self._dictation_token = 0
        self._dictation_path = os.path.join(
            tempfile.gettempdir(), "openwhisper_rule_dictation.wav"
        )
        self._dictation_timer = QTimer(self)
        self._dictation_timer.setSingleShot(True)
        self._dictation_timer.setInterval(RULE_DICTATION_CAP_MS)
        self._dictation_timer.timeout.connect(self.stop_dictation)
        relay = _thread_relay()
        relay.cleanup_rule_polished.connect(self._on_polished)
        relay.rule_dictation_finished.connect(self._on_dictation_finished)
        relay.rule_dictation_level.connect(self._on_dictation_level)

    def __getattr__(self, name):
        # Like the dialog's own controls, reaching one of the page's widgets
        # builds the page first.
        if name in CONTROLS or name == "glow":
            ensure = self.__dict__.get("_ensure_built")
            if ensure is not None:
                ensure()
                if name in self.__dict__:
                    return self.__dict__[name]
        raise AttributeError(name)

    @property
    def dictation_state(self) -> str:
        """"idle", "recording" or "transcribing"."""
        return self._dictation_state

    @property
    def polishing(self) -> bool:
        return self._polishing

    @property
    def recorder(self) -> Optional[AudioRecorder]:
        return self._recorder

    # ---- construction ----

    def build(self, layout: QVBoxLayout, tile_group: Callable) -> None:
        """Add the composer and library tiles to ``layout``."""
        self.cleanup_rules_composer_tile = InfoTile(
            "Teach a new rule",
            "Type or dictate an instruction in your own words. Your AI "
            "cleanup model rewrites it as a clear rule, and you review it "
            "before it's saved.",
            design_icon("plus-blue.svg"),
        )
        rule_input_row = QHBoxLayout()
        rule_input_row.setContentsMargins(0, 0, 0, 0)
        rule_input_row.setSpacing(8)
        self.cleanup_rule_input = QLineEdit()
        self.cleanup_rule_input.setObjectName("cleanupRuleInput")
        self.cleanup_rule_input.setMinimumHeight(40)
        self.cleanup_rule_input.setPlaceholderText(
            'Try: Always spell my name "Alex Rivera"'
        )
        self.cleanup_rule_input.returnPressed.connect(self.add_rule)
        self.cleanup_rule_input.textEdited.connect(
            lambda _text: self._set_notice("")
        )
        rule_input_row.addWidget(self.cleanup_rule_input, stretch=1)
        self.cleanup_rule_mic_btn = Button("Dictate")
        self.cleanup_rule_mic_btn.setObjectName("cleanupRuleDictateButton")
        # theme.qss swaps the icon (microphone / stop) on this property.
        self.cleanup_rule_mic_btn.setProperty("recording", False)
        self.cleanup_rule_mic_btn.setIconSize(QSize(16, 16))
        self.cleanup_rule_mic_btn.set_base_minimum_size(108, 40)
        self.cleanup_rule_mic_btn.setToolTip(
            "Speak the instruction instead of typing it"
        )
        self.cleanup_rule_mic_btn.clicked.connect(self.toggle_dictation)
        rule_input_row.addWidget(self.cleanup_rule_mic_btn)
        self.cleanup_rule_add_btn = PrimaryButton("Add rule")
        self.cleanup_rule_add_btn.setObjectName("cleanupRuleAddButton")
        self.cleanup_rule_add_btn.set_base_minimum_size(104, 40)
        self.cleanup_rule_add_btn.clicked.connect(self.add_rule)
        rule_input_row.addWidget(self.cleanup_rule_add_btn)
        # No spacing between the input row and the activity strip: the strip
        # carries its own gap inside the height it animates, so it slides
        # open from nothing.
        composer_column = QVBoxLayout()
        composer_column.setContentsMargins(0, 0, 0, 0)
        composer_column.setSpacing(0)
        composer_column.addLayout(rule_input_row)
        self.cleanup_rule_activity = RuleActivityStrip(
            RULE_DICTATION_CAP_MS // 1000
        )
        composer_column.addWidget(self.cleanup_rule_activity)
        self.cleanup_rules_composer_tile.add_body_layout(composer_column)
        # Problems only; progress is narrated by the activity strip.
        self.cleanup_rule_status = QLabel("")
        self.cleanup_rule_status.setObjectName("cleanupRuleStatus")
        self.cleanup_rule_status.setWordWrap(True)
        self.cleanup_rule_status.hide()
        self.cleanup_rules_composer_tile.add_body(self.cleanup_rule_status)
        tile_group(layout, "", [self.cleanup_rules_composer_tile], columns=1)

        self.cleanup_rules_library_tile = InfoTile(
            "Your rules",
            "Select a rule or double-click it to edit. Every rule applies to "
            "AI cleanup, and to profiles set to also apply learned rules.",
            design_icon("stack-slate.svg"),
        )
        self.cleanup_rules_count = QLabel()
        self.cleanup_rules_count.setObjectName("cleanupRulesCount")
        self.cleanup_rules_count.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cleanup_rules_library_tile.add_trailing(self.cleanup_rules_count)

        self.cleanup_rules_list = QListWidget()
        self.cleanup_rules_list.setObjectName("cleanupRulesList")
        self.cleanup_rules_list.setWordWrap(True)
        self.cleanup_rules_list.setSpacing(6)
        self.cleanup_rules_list.setMinimumHeight(88)
        self.cleanup_rules_list.itemSelectionChanged.connect(self.update_controls)
        self.cleanup_rules_list.itemDoubleClicked.connect(
            lambda _item: self.edit_rule()
        )
        self.cleanup_rules_library_tile.add_body(self.cleanup_rules_list)
        self.glow = ItemGlow(self.cleanup_rules_list)

        self.cleanup_rules_empty = EmptyState(
            "No rules yet",
            "Add your first instruction above to start building a personal cleanup profile.",
        )
        self.cleanup_rules_library_tile.add_body(self.cleanup_rules_empty)

        rule_btn_row = QHBoxLayout()
        rule_btn_row.setContentsMargins(0, 0, 0, 0)
        rule_btn_row.setSpacing(8)
        rule_btn_row.addStretch()
        self.cleanup_rule_edit_btn = neutral_button(Button("Edit rule"))
        self.cleanup_rule_edit_btn.setObjectName("cleanupRuleEditButton")
        self.cleanup_rule_edit_btn.set_base_minimum_size(96, 34)
        self.cleanup_rule_edit_btn.clicked.connect(self.edit_rule)
        rule_btn_row.addWidget(self.cleanup_rule_edit_btn)
        self.cleanup_rule_delete_btn = neutral_button(Button("Delete"))
        self.cleanup_rule_delete_btn.setObjectName("cleanupRuleDeleteButton")
        self.cleanup_rule_delete_btn.set_base_minimum_size(88, 34)
        self.cleanup_rule_delete_btn.clicked.connect(self.delete_rule)
        rule_btn_row.addWidget(self.cleanup_rule_delete_btn)
        self.cleanup_rules_library_tile.add_body_layout(rule_btn_row)
        tile_group(layout, "", [self.cleanup_rules_library_tile], columns=1)

    def load(self, settings: dict) -> None:
        self.cleanup_rules_list.clear()
        self.cleanup_rules_list.addItems(resolve_transcript_cleanup_rules(settings))

    # ---- the library ----

    def staged_rules(self) -> list:
        return [
            self.cleanup_rules_list.item(i).text()
            for i in range(self.cleanup_rules_list.count())
        ]

    def _save_rules(self) -> None:
        self._persist_rules(self.staged_rules())

    def update_controls(self) -> None:
        enabled = self._cleanup_enabled()
        busy = self._polishing or self._dictation_state != "idle"
        rule_count = self.cleanup_rules_list.count()
        self.cleanup_rules_count.setText(
            f"{rule_count} / {config.MAX_TRANSCRIPT_CLEANUP_RULES}"
        )
        self.cleanup_rules_list.setVisible(rule_count > 0)
        self.cleanup_rules_empty.setVisible(rule_count == 0)
        self.cleanup_rules_empty.setEnabled(enabled)
        self.cleanup_rule_input.setEnabled(enabled and not busy)
        self.cleanup_rule_add_btn.setEnabled(enabled and not busy)
        self.cleanup_rule_mic_btn.setEnabled(
            enabled
            and not self._polishing
            and self._dictation_state != "transcribing"
        )
        has_selection = bool(self.cleanup_rules_list.selectedItems())
        self.cleanup_rule_edit_btn.setEnabled(enabled and has_selection)
        self.cleanup_rule_delete_btn.setEnabled(enabled and has_selection)

    def _set_notice(self, text: str) -> None:
        """Show a problem under the composer, or clear it with ``""``."""
        self.cleanup_rule_status.setText(text)
        self.cleanup_rule_status.setVisible(bool(text))

    def _set_mic_recording(self, recording: bool) -> None:
        button = self.cleanup_rule_mic_btn
        set_style_property(button, "recording", recording)
        button.setText("Stop" if recording else "Dictate")
        button.setToolTip(
            "Stop and transcribe · Esc throws the recording away"
            if recording else
            "Speak the instruction instead of typing it"
        )

    @staticmethod
    def _polish_detail(provider: str, model: str, settings) -> str:
        """The endpoint and model that will rewrite the words."""
        name = profile_display_name(provider, settings)
        return " · ".join(part for part in (name, model) if part)

    def add_rule(self) -> None:
        self.polish_rule(self.cleanup_rule_input.text())

    def polish_rule(self, raw: str, dictated: bool = False) -> None:
        raw = raw.strip()
        if self._polishing or self._dictation_state != "idle":
            return
        if not raw:
            self.cleanup_rule_activity.finish()
            return
        staged = {r.casefold() for r in self.staged_rules()}
        if raw.casefold() in staged:
            self.cleanup_rule_activity.finish()
            self._set_notice("That rule already exists.")
            return
        if self.cleanup_rules_list.count() >= config.MAX_TRANSCRIPT_CLEANUP_RULES:
            self.cleanup_rule_activity.finish()
            self._set_notice(
                f"Rule limit reached ({config.MAX_TRANSCRIPT_CLEANUP_RULES})."
            )
            return

        self._polishing = True
        self._polish_dictated = dictated
        self._set_notice("")

        settings = self._load_settings()
        provider = resolve_transcript_cleanup_provider(settings)
        model = resolve_transcript_cleanup_model(settings)
        reasoning = resolve_transcript_cleanup_reasoning(settings)
        self.cleanup_rule_activity.show_polishing(
            self._polish_detail(provider, model, settings)
        )
        self.update_controls()
        self._polish_token = token = next(_tokens)
        done = _thread_relay().cleanup_rule_polished

        def worker():
            try:
                from services.transcript_cleanup import polish_cleanup_rule

                polished, error = polish_cleanup_rule(
                    raw, provider=provider, model=model, reasoning=reasoning
                )
            except Exception as exc:
                polished, error = raw, str(exc)
            done.emit(token, raw, polished or raw, error or "")

        threading.Thread(
            target=worker, name="cleanup-rule-polish", daemon=True
        ).start()

    def _on_polished(self, token: int, raw: str, polished: str, error: str) -> None:
        if token != self._polish_token:
            return
        self._polishing = False
        dictated, self._polish_dictated = self._polish_dictated, False
        self.cleanup_rule_activity.finish()
        self.update_controls()
        if not self._is_visible():
            # Settings closed while the model worked. The words are still in
            # the input for next time; a review dialog now would pop up alone.
            return

        notice = None
        if error:
            notice = (
                "AI polish unavailable — your words will be saved as transcribed."
                if dictated else
                "AI polish unavailable — your wording will be saved as written."
            )
        dialog = CleanupRuleDialog(
            polished, original=raw, notice=notice, dictated=dictated, parent=self.parent()
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        rule = dialog.rule_text()
        if not rule:
            return
        staged = {r.casefold() for r in self.staged_rules()}
        if rule.casefold() in staged:
            self._set_notice("That rule already exists.")
            return
        self.cleanup_rules_list.addItem(rule)
        item = self.cleanup_rules_list.item(self.cleanup_rules_list.count() - 1)
        self.cleanup_rule_input.clear()
        self.update_controls()
        self._save_rules()
        self.glow.flash(item)

    def edit_rule(self) -> None:
        items = self.cleanup_rules_list.selectedItems()
        if not items:
            return
        item = items[0]
        dialog = CleanupRuleDialog(item.text(), parent=self.parent())
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        rule = dialog.rule_text()
        if rule:
            item.setText(rule)
            self._save_rules()

    def delete_rule(self) -> None:
        for item in self.cleanup_rules_list.selectedItems():
            self.cleanup_rules_list.takeItem(self.cleanup_rules_list.row(item))
        self.update_controls()
        self._save_rules()

    # ---- dictation ----

    def toggle_dictation(self) -> None:
        if self._dictation_state == "recording":
            self.stop_dictation()
            return
        if self._dictation_state != "idle" or self._polishing:
            return
        if self._transcriber() is None:
            self._set_notice("Dictation is unavailable.")
            return
        if self._meeting_active():
            self._set_notice(
                "Meeting Mode is active — end the meeting to dictate a rule."
            )
            return

        settings = self._settings_snapshot()
        priority = settings_microphones.saved_priority(settings)
        order = tuple((entry["hostapi"], entry["name"]) for entry in priority)
        if self._recorder is None or self._recorder_device != order:
            if self._recorder is not None:
                self._recorder.cleanup()
            self._recorder = AudioRecorder(
                device_priority=priority, output_file=self._dictation_path
            )
            # Levels come from the audio thread, which must not reach back
            # into the window; the relay hands them to the composer's trace.
            self._level_token = token = next(_tokens)
            level = _thread_relay().rule_dictation_level
            self._recorder.set_audio_level_callback(
                lambda value: level.emit(token, value)
            )
            self._recorder_device = order

        if not self._recorder.start_recording():
            reason = self._recorder.last_start_error
            self._set_notice(
                f"Couldn't start recording: {reason}." if reason
                else "Couldn't start recording."
            )
            return
        self._dictation_state = "recording"
        self._set_notice("")
        self._set_mic_recording(True)
        self.cleanup_rule_activity.show_listening(
            settings_microphones.preferred_name(settings)
        )
        self._dictation_timer.start()
        self.update_controls()

    def _on_dictation_level(self, token: int, level: float) -> None:
        if token == self._level_token:
            self.cleanup_rule_activity.push_level(level)

    def cancel_dictation(self) -> None:
        """Throw the take away without transcribing it."""
        if self._dictation_state != "recording":
            return
        self._dictation_timer.stop()
        if self._recorder is not None:
            self._recorder.cancel_recording()
        self._dictation_state = "idle"
        self._set_mic_recording(False)
        self.cleanup_rule_activity.finish()
        self.update_controls()

    def stop_dictation(self) -> None:
        if self._dictation_state != "recording":
            return
        self._dictation_timer.stop()
        self._dictation_state = "transcribing"
        self._set_mic_recording(False)
        self.cleanup_rule_activity.show_transcribing(self._transcribe_detail())
        self.update_controls()

        recorder = self._recorder
        transcribe = self._transcriber()
        audio_path = self._dictation_path
        self._dictation_token = token = next(_tokens)
        done = _thread_relay().rule_dictation_finished

        def worker():
            text, error = _transcribe_rule_dictation(recorder, transcribe, audio_path)
            done.emit(token, text.strip(), error)

        threading.Thread(
            target=worker, name="rule-dictation", daemon=True
        ).start()

    def _on_dictation_finished(self, token: int, text: str, error: str) -> None:
        if token != self._dictation_token:
            return
        self._dictation_state = "idle"
        self.update_controls()
        if error:
            self.cleanup_rule_activity.finish()
            self._set_notice(error)
            return
        current = self.cleanup_rule_input.text().strip()
        raw = f"{current} {text}".strip() if current else text
        # The words land in the input first, so a canceled review (or a
        # Settings window closed mid-polish) keeps them for editing.
        self.cleanup_rule_input.setText(raw)
        if not self._is_visible():
            self.cleanup_rule_activity.finish(animate=False)
            return
        self.polish_rule(raw, dictated=True)

    def release_recorder(self, *_args) -> None:
        self._dictation_timer.stop()
        if self._dictation_state == "recording":
            # Settings is reused when it reopens, so a take still recording
            # at close is discarded here rather than left showing "Stop".
            self._dictation_state = "idle"
            self._set_mic_recording(False)
            self.cleanup_rule_activity.finish(animate=False)
            self.update_controls()
        if self._recorder is not None:
            self._recorder.cleanup()
            self._recorder = None
