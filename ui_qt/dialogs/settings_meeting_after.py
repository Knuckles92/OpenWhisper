"""Settings → Meeting Mode → After End: the passes that run once a meeting ends.

Re-decode, transcript cleanup, the final report and its views, and the
experimental insight review, whose switch asks for consent before it saves.
"""

from __future__ import annotations

from typing import Callable

from PyQt6.QtCore import QObject
from PyQt6.QtWidgets import QHBoxLayout, QMessageBox, QVBoxLayout

from services.settings import (
    INSIGHT_REVIEW_CONSENT,
    SettingsKey,
    resolve_meeting_end_polish,
    resolve_meeting_end_redecode,
    resolve_meeting_end_report,
    resolve_meeting_insight_review,
    resolve_meeting_report_brief,
    resolve_meeting_report_ribbon,
    resolve_meeting_report_signal,
)
from services.typesafe import TYPESAFE_ROUTE as JEV_TYPESAFE_ROUTE
from ui_qt.dialogs.settings_fields import settings_caption, settings_field
from ui_qt.utils.icons import design_icon
from ui_qt.widgets import ElidingComboBox, SettingTile

#: The section's widgets the dialog also exposes by name, for deep links,
#: search reveals and tests (see settings_metadata.CONTROL_DESTINATIONS).
CONTROLS = (
    "meeting_end_redecode_tile",
    "meeting_end_redecode_check",
    "meeting_end_polish_tile",
    "meeting_end_polish_check",
    "meeting_end_report_tile",
    "meeting_end_report_check",
    "meeting_review_tile",
    "meeting_review_check",
    "meeting_review_sensitivity",
    "meeting_report_ribbon_tile",
    "meeting_report_brief_tile",
    "meeting_report_signal_tile",
    "meeting_report_ribbon_check",
    "meeting_report_brief_check",
    "meeting_report_signal_check",
    "meeting_report_views_title",
    "meeting_report_views_info",
    "meeting_report_views_hint",
)


class MeetingAfterSection(QObject):
    """Builds the After End page and keeps at least one report view on.

    The dialog hands in how to save one setting or several at once, the
    binder its plain switches load through, whether it is loading saved
    values, and which Fast judgments route the insight review would use.
    """

    def __init__(
        self,
        parent=None,
        *,
        persist: Callable[[str, object], object],
        persist_many: Callable[[dict], object],
        bindings,
        is_loading: Callable[[], bool],
        review_route: Callable[[], object],
        ensure_built: Callable[[], object] = lambda: None,
    ):
        super().__init__(parent)
        self._persist = persist
        self._persist_many = persist_many
        self._bindings = bindings
        self._is_loading = is_loading
        self._review_route = review_route
        self._ensure_built = ensure_built

    def __getattr__(self, name):
        # Like the dialog's own controls, reaching one of the page's widgets
        # builds the page first.
        if name in CONTROLS:
            ensure = self.__dict__.get("_ensure_built")
            if ensure is not None:
                ensure()
                if name in self.__dict__:
                    return self.__dict__[name]
        raise AttributeError(name)

    def build(self, layout: QVBoxLayout, tile_group: Callable) -> None:
        self.meeting_end_redecode_tile = SettingTile(
            "Re-transcribe the full recording",
            "After End, recut the continuous session audio on longer quiet "
            "gaps and run Whisper again. Live capture is unchanged.",
            design_icon("microphone-blue.svg"),
        )
        self.meeting_end_redecode_check = self.meeting_end_redecode_tile.checkbox
        self._bindings.checkbox(
            self.meeting_end_redecode_check,
            SettingsKey.MEETING_END_REDECODE,
            resolve_meeting_end_redecode,
        )

        self.meeting_end_polish_tile = SettingTile(
            "Clean up the transcript with the LLM",
            "Rewrites the finished transcript for readability. Needs AI "
            "insights on for the meeting.",
            design_icon("stack-purple.svg"),
        )
        self.meeting_end_polish_check = self.meeting_end_polish_tile.checkbox
        self._bindings.checkbox(
            self.meeting_end_polish_check,
            SettingsKey.MEETING_END_POLISH,
            resolve_meeting_end_polish,
        )

        self.meeting_end_report_tile = SettingTile(
            "Write the final report",
            "Topic, summary, and cards, generated once live captions finish. "
            "Needs AI insights on for the meeting.",
            design_icon("check-green.svg"),
        )
        self.meeting_end_report_check = self.meeting_end_report_tile.checkbox
        self.meeting_end_report_check.toggled.connect(
            self._on_end_report_toggled
        )

        self.meeting_review_tile = SettingTile(
            "Review uncertain insights at the end",
            "For new meetings. Gives the Fast judgments judge relevant excerpts, speaker "
            "names and insights to find ambiguities, and asks the three highest-priority "
            "questions first. No audio is sent. Needs AI insights and Fast judgments.",
            design_icon("check-green.svg"),
        )
        self.meeting_review_tile.add_title_badge("Experimental")
        self.meeting_review_check = self.meeting_review_tile.checkbox
        self.meeting_review_check.toggled.connect(self._on_review_toggled)
        review_row = QHBoxLayout()
        self.meeting_review_sensitivity = ElidingComboBox()
        self.meeting_review_sensitivity.addItem("Normal", "normal")
        self.meeting_review_sensitivity.addItem("Thorough", "thorough")
        self._bindings.combo(
            self.meeting_review_sensitivity,
            SettingsKey.MEETING_INSIGHT_REVIEW_SENSITIVITY,
            lambda settings: resolve_meeting_insight_review(settings)["sensitivity"],
        )
        review_row.addWidget(
            settings_field("Sensitivity", self.meeting_review_sensitivity)
        )
        self.meeting_review_tile.add_body_layout(review_row)

        tile_group(
            layout,
            "After End",
            [
                self.meeting_end_redecode_tile,
                self.meeting_end_polish_tile,
                self.meeting_end_report_tile,
                self.meeting_review_tile,
            ],
            intro=(
                "Live captions stay on short chunks so text appears quickly. "
                "These steps run afterward."
            ),
        )

        self.meeting_report_ribbon_tile = SettingTile(
            "Ribbon",
            "Timeline walk. Adds timeline beats and polished minutes, which "
            "is the main token cost.",
            design_icon("stack-slate.svg"),
        )
        self.meeting_report_brief_tile = SettingTile(
            "Brief",
            "One-page editorial summary. Reuses the same cards as Signal.",
            design_icon("notes-blue.svg"),
        )
        self.meeting_report_signal_tile = SettingTile(
            "Signal",
            "One-screen glance. Reuses the same cards as Brief.",
            design_icon("broadcast-green.svg"),
        )
        self.meeting_report_ribbon_check = self.meeting_report_ribbon_tile.checkbox
        self.meeting_report_brief_check = self.meeting_report_brief_tile.checkbox
        self.meeting_report_signal_check = self.meeting_report_signal_tile.checkbox
        for check, key in (
            (self.meeting_report_ribbon_check, SettingsKey.MEETING_REPORT_RIBBON),
            (self.meeting_report_brief_check, SettingsKey.MEETING_REPORT_BRIEF),
            (self.meeting_report_signal_check, SettingsKey.MEETING_REPORT_SIGNAL),
        ):
            check.toggled.connect(
                lambda checked, setting=key: self._on_report_view_toggled(
                    setting, checked
                )
            )

        self.meeting_report_views_title, self.meeting_report_views_info = (
            tile_group(
                layout,
                "Report views",
                [
                    self.meeting_report_ribbon_tile,
                    self.meeting_report_brief_tile,
                    self.meeting_report_signal_tile,
                ],
                intro=(
                    "Each enabled view is generated at End. At least one view "
                    "stays on."
                ),
            )
        )

        self.meeting_report_views_hint = settings_caption(
            "At least one view is required. Ribbon stays on."
        )
        self.meeting_report_views_hint.hide()
        layout.addWidget(self.meeting_report_views_hint)

    def load(self, settings: dict) -> None:
        review_on = (
            settings.get(SettingsKey.MEETING_INSIGHT_REVIEW) is True
            and settings.get(SettingsKey.MEETING_INSIGHT_REVIEW_CONSENT) == INSIGHT_REVIEW_CONSENT
        )
        self.meeting_review_check.setChecked(review_on)
        self.meeting_end_report_check.setChecked(resolve_meeting_end_report(settings))
        self.meeting_report_ribbon_check.setChecked(resolve_meeting_report_ribbon(settings))
        self.meeting_report_brief_check.setChecked(resolve_meeting_report_brief(settings))
        self.meeting_report_signal_check.setChecked(resolve_meeting_report_signal(settings))
        self._guard_report_views()
        self._update_report_views_enabled()

    def _on_review_toggled(self, checked: bool) -> None:
        if self._is_loading():
            return
        if checked:
            route = self._review_route()
            if route.is_local:
                destination = f"{route.name} on this computer"
            elif route.profile is not None:
                destination = f"{route.name} ({route.host})"
            elif route is JEV_TYPESAFE_ROUTE:
                destination = "TypeSafe"
            else:
                destination = f"TypeSafe through {route.name}"
            separate = ("" if route.profile is not None
                        else "This is a separate service from your meeting LLM. ")
            reply = QMessageBox.question(
                self.parent(), "Enable experimental insight review?",
                "For future meetings with AI insights on, send relevant transcript "
                f"excerpts, speaker names, and generated insights to {destination} after the meeting? "
                f"{separate}No audio is sent. "
                "Review is optional and does not block saving your recording.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                self.meeting_review_check.blockSignals(True)
                self.meeting_review_check.setChecked(False)
                self.meeting_review_check.blockSignals(False)
                return
        # One write, so the consent and the switch never disagree on disk.
        self._persist_many({
            SettingsKey.MEETING_INSIGHT_REVIEW_CONSENT:
                INSIGHT_REVIEW_CONSENT if checked else "",
            SettingsKey.MEETING_INSIGHT_REVIEW: bool(checked),
        })

    def _on_end_report_toggled(self, checked: bool) -> None:
        self._update_report_views_enabled()
        self._persist(SettingsKey.MEETING_END_REPORT, bool(checked))

    def _on_report_view_toggled(self, key: str, _checked: bool) -> None:
        self._guard_report_views()
        actual = {
            SettingsKey.MEETING_REPORT_RIBBON: (
                self.meeting_report_ribbon_check.isChecked()
            ),
            SettingsKey.MEETING_REPORT_BRIEF: (
                self.meeting_report_brief_check.isChecked()
            ),
            SettingsKey.MEETING_REPORT_SIGNAL: (
                self.meeting_report_signal_check.isChecked()
            ),
        }
        self._persist(key, actual[key])

    def _report_view_checks(self):
        return (
            self.meeting_report_ribbon_check,
            self.meeting_report_brief_check,
            self.meeting_report_signal_check,
        )

    def _update_report_views_enabled(self) -> None:
        enabled = self.meeting_end_report_check.isChecked()
        self.meeting_report_views_title.setEnabled(enabled)
        self.meeting_report_views_info.setEnabled(enabled)
        for tile in (
            self.meeting_report_ribbon_tile,
            self.meeting_report_brief_tile,
            self.meeting_report_signal_tile,
        ):
            tile.setEnabled(enabled)
        if not enabled:
            self.meeting_report_views_hint.hide()

    def _guard_report_views(self) -> None:
        if any(check.isChecked() for check in self._report_view_checks()):
            self.meeting_report_views_hint.hide()
            return
        blocker = self.meeting_report_ribbon_check.blockSignals(True)
        self.meeting_report_ribbon_check.setChecked(True)
        self.meeting_report_ribbon_check.blockSignals(blocker)
        self.meeting_report_views_hint.show()

