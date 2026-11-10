"""Meeting Mode tab for the main window.

Two panes, as on the Walkthrough tab (``studio.py``). The left column is what
you set: Start Meeting, the AI insights switch with the model it will use, and
an optional brief saying what the host wants out of the meeting (carried into
the engine so the agent works to it from the first checkpoint, and shown only
while AI insights are on and the next meeting is being set up).

The right side is a stage with one slot. With no meeting it says what Meeting
Mode does and offers the newest few to open; during one it is the on-air card
with the elapsed clock, Pause and End, while the column's top slot holds the
dashboard and guest-link actions; after capture ends it is the finalization
card, which reports running/completed/disabled/unavailable/failed AI outcomes
without blocking other tabs. The stage changing never resizes the window.
All user intent leaves through signals; state flows back in via
``set_meeting_state`` payload dicts (partial updates — absent keys leave the
current state untouched).
"""
import itertools
import logging
import math
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from PyQt6.QtCore import QEvent, QObject, QPointF, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QFont, QPainter
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from meeting.platform import meeting_mode_supported, meeting_setup_available, meeting_unsupported_os_name
from meeting.content import fallback_meeting_title, meeting_insights_pill
from meeting.time_utils import (
    UNKNOWN_DATE,
    format_meeting_duration,
    format_meeting_identity_meta,
    format_meeting_started_at,
)
from services.settings import SETTING_DEFAULTS, SettingsKey, settings_manager
from ui_qt.utils.palette import token_color
from ui_qt.utils.restyle import repolish, set_style_property
from ui_qt.widgets.buttons import Button, DangerButton, SuccessButton
from ui_qt.widgets.cards import Card
from ui_qt.widgets.eliding_label import ElidingLabel
from ui_qt.widgets.settings_switch import SettingsSwitch
from ui_qt.widgets.studio import COLUMN_WIDTH, GAP, Stage, StudioSplit, scaled
from ui_qt.widgets.wrapped_label import WrappedLabel

logger = logging.getLogger(__name__)

# Sizes at 100% type; each grows with the font scale.
#: The stage: the width it asks for beside the column, and its height, which
#: holds the empty state with three recent meetings and sets the page's.
STAGE_WIDTH = 560
STAGE_HEIGHT = 540
#: Under the column's top edge: Start, or the two dashboard actions.
TOP_SLOT_HEIGHT = 48
#: The on-air card at its widest, and its Pause and End buttons.
ON_AIR_WIDTH = 520
ON_AIR_BUTTON_WIDTH = 150
#: How many meetings the empty stage offers to open.
RECENT_SHOWN = 3
#: Meetings still being captured are not history yet.
_LIVE_STATUSES = frozenset({"active", "paused", "ending"})


def read_recent_meetings(repository=None) -> List[Dict[str, Any]]:
    """The newest finished meetings kept on this computer, newest first."""
    if repository is None:
        from meeting.persist.repository import SqlMeetingRepository

        repository = SqlMeetingRepository()
    # A few spare rows, in case the newest are still being captured.
    rows = repository.list_past_meeting_summaries(limit=RECENT_SHOWN + 3, query="")
    finished = [
        dict(row) for row in rows
        if str(dict(row).get("status") or "").lower() not in _LIVE_STATUSES
    ]
    return finished[:RECENT_SHOWN]


class _RecentRelay(QObject):
    """Carries a worker's recent meetings back to the UI thread: (token, meetings)."""

    loaded = pyqtSignal(int, object)


_relay_instance: Optional[_RecentRelay] = None
_tokens = itertools.count(1)


def _relay() -> _RecentRelay:
    # Made on first use, on the UI thread: a module-level QObject would live
    # on whichever thread imported this module.
    global _relay_instance
    if _relay_instance is None:
        _relay_instance = _RecentRelay()
    return _relay_instance


class LiveDot(QWidget):
    """The recording light. It breathes only while audio is being captured."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(22, 22)
        self._live = False
        self._phase = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self._tick)

    def set_live(self, live: bool) -> None:
        self._live = bool(live)
        self._sync()

    def _sync(self) -> None:
        if self._live and self.isVisible():
            self._timer.start()
        else:
            self._timer.stop()
            self._phase = 0.0
        self.update()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._sync()

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self._sync()

    def _tick(self) -> None:
        # One breath every two seconds.
        self._phase = (self._phase + 0.025) % 1.0
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        centre = QPointF(self.width() / 2, self.height() / 2)
        if self._live:
            swell = 0.5 - 0.5 * math.cos(self._phase * 2 * math.pi)
            painter.setBrush(token_color("danger", round(40 + 50 * (1 - swell))))
            radius = 7.0 + 4.0 * swell
            painter.drawEllipse(centre, radius, radius)
            painter.setBrush(token_color("danger"))
        else:
            painter.setBrush(token_color("text-muted"))
        painter.drawEllipse(centre, 5.0, 5.0)


class RecentMeetingRow(QFrame):
    """One meeting on the empty stage: click, Enter or Space opens it."""

    clicked = pyqtSignal(str)

    def __init__(self, meeting: Dict[str, Any]):
        super().__init__()
        self.meeting_id = str(meeting.get("id") or "")
        self.setObjectName("meetingRecent")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        row = QHBoxLayout(self)
        row.setContentsMargins(10, 8, 12, 8)
        row.setSpacing(12)
        words = QVBoxLayout()
        words.setSpacing(2)
        title = fallback_meeting_title(meeting)
        self.title_label = ElidingLabel(title)
        self.title_label.setObjectName("meetingTitle")
        words.addWidget(self.title_label)
        meta = " · ".join(part for part in (
            format_meeting_started_at(meeting.get("started_at")),
            format_meeting_duration(meeting),
        ) if part)
        meta_label = QLabel(meta)
        meta_label.setObjectName("meetingHint")
        words.addWidget(meta_label)
        row.addLayout(words, 1)
        pill = meeting_insights_pill(meeting)
        if pill:
            label, tone = pill
            self.pill = QLabel(label)
            self.pill.setObjectName("meetingRecentPill")
            self.pill.setProperty("tone", tone)
            row.addWidget(self.pill, 0, Qt.AlignmentFlag.AlignVCenter)
        self.setAccessibleName(f"Open {title}. {meta}")

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(
                event.position().toPoint()):
            self.clicked.emit(self.meeting_id)
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.clicked.emit(self.meeting_id)
            return
        super().keyPressEvent(event)


def meeting_audio_support_copy(platform: Optional[str] = None) -> tuple[str, str]:
    """Return accurate Meeting Mode capture copy for the current platform."""
    platform = platform or sys.platform
    subtitle = (
        "Capture microphone audio and, when supported, system audio. Follow "
        "the transcript and insights in the browser dashboard."
    )
    if platform.startswith("win"):
        hint = "System audio uses Windows WASAPI loopback when available."
    elif platform == "darwin" and meeting_mode_supported(platform):
        hint = (
            "System audio uses macOS ScreenCaptureKit, which needs Screen "
            "Recording permission. Without it, meetings capture microphone "
            "audio only."
        )
    elif platform.startswith("linux"):
        from meeting.platform import linux_meeting_implementation_ready
        if linux_meeting_implementation_ready():
            hint = (
                "Meeting Mode on Linux is a preview. System audio comes "
                "through PulseAudio or PipeWire, or you can continue with "
                "the microphone only."
            )
        else:
            os_name = meeting_unsupported_os_name(platform)
            hint = (
                f"{os_name} on this architecture is not a supported Meeting Mode "
                "platform. Meetings here capture microphone audio only and are "
                "unsupported."
            )
    else:
        os_name = meeting_unsupported_os_name(platform)
        hint = (
            f"{os_name} is not a supported Meeting Mode platform. "
            "System-audio capture needs Windows or macOS 13+. Meetings here "
            "capture microphone audio only and are unsupported."
        )
    return subtitle, hint


def ai_insights_tooltip() -> str:
    """Explain the AI insights switch in a compact, wrapped hover hint."""
    return (
        "On: send transcript text to the AI model chosen\n"
        "in Settings for live insights and a final report.\n"
        "Off: keep the recording and transcript only.\n"
        "Audio routing is set separately in Voice & speakers."
    )


def ai_insights_destination(
    settings: Optional[Dict[str, Any]] = None,
) -> Tuple[str, str]:
    """Say which model AI insights would use and what leaves this computer.

    The switch is named for what it produces, so this line carries what the
    old "cloud" name implied, and stays true for a model on this PC.

    Args:
        settings: Settings mapping; loaded when omitted.

    Returns:
        ``(where, privacy)``, e.g. ``("OpenRouter · deepseek-v4.1-flash",
        "sends transcript text, never audio")`` or, for an installed agent,
        ``("Claude Code · Haiku", "your agent receives transcript text, never
        audio")``.
    """
    try:
        from services.settings import (
            MeetingAgentCore,
            resolve_meeting_agent_core,
            resolve_meeting_llm_model,
            resolve_meeting_llm_profile,
            resolve_typesafe_enabled,
        )

        if settings is None:
            settings = settings_manager.load_all_settings()
        core = resolve_meeting_agent_core(settings)
        if core in MeetingAgentCore.INSTALLED:
            return installed_agent_destination(core, settings)
        profile = resolve_meeting_llm_profile(settings)
        model = resolve_meeting_llm_model(settings)
        from services.typesafe import route_is_local

        # Fast judgments elsewhere still receive excerpts under the same switch.
        judged_elsewhere = resolve_typesafe_enabled(settings) and not route_is_local(settings)
    except Exception:
        logger.debug("Could not resolve the meeting AI model", exc_info=True)
        profile = None
    if profile is None:
        return (
            "No AI model chosen",
            "pick one in Settings → Meeting Mode → Intelligence",
        )
    if profile.is_local:
        where = f"{profile.name} on this PC"
        # Fast judgments on Jev or a hosted model still receive excerpts, so
        # only claim the text stays here when they are off or local too.
        privacy = (
            "the model runs on this computer"
            if judged_elsewhere
            else "transcript text stays on this computer"
        )
    else:
        where = profile.name
        privacy = "sends transcript text, never audio"
    # OpenRouter ids carry the vendor ("deepseek/deepseek-v4.1-flash"); the
    # provider already names the route, so the short name reads better.
    short_model = (model or "").rsplit("/", 1)[-1]
    if short_model:
        where = f"{where} · {short_model}"
    return where, privacy


def installed_agent_destination(
    agent_id: str, settings: Dict[str, Any],
) -> Tuple[str, str]:
    """The AI insights line for a coding agent the user installed.

    Reads only the cached scan, so the idle tab never waits on the agent's
    command line. Before any scan it names the choice without a verdict.
    """
    from services.installed_agents import AGENT_SPECS, cached_scan, describe_choice
    from services.settings import resolve_meeting_agent_model

    spec = AGENT_SPECS[agent_id]
    elsewhere = "choose another agent in Settings → Meeting Mode → Intelligence"
    scan = cached_scan()
    if scan is not None:
        agent = scan.get(agent_id)
        if agent is None:
            return f"{spec.name} not found", elsewhere
        if agent.problem:
            return f"{spec.name} needs an update", f"update it, or {elsewhere}"
        if agent.signed_in is False:
            return f"{spec.name} isn't signed in", f"sign in, or {elsewhere}"
    where = describe_choice(agent_id, resolve_meeting_agent_model(agent_id, settings))
    return where, "your agent receives transcript text, never audio"


def meeting_audio_platform_tone(platform: Optional[str] = None) -> str:
    """``"note"`` for Linux's preview line, ``"warning"`` for unsupported ones."""
    platform = platform or sys.platform
    if platform.startswith("linux"):
        from meeting.platform import linux_meeting_implementation_ready
        if linux_meeting_implementation_ready():
            return "note"
    return "warning"


def meeting_audio_shows_platform_warning(platform: Optional[str] = None) -> bool:
    """True when the idle platform sentence should stay visible as a warning."""
    platform = platform or sys.platform
    if platform.startswith("win"):
        return False
    if platform == "darwin" and meeting_mode_supported(platform):
        return False
    # Linux remains publicly unsupported until hardware attestation, so the
    # idle warning stays visible even when the implementation is ready.
    return True


def _saved_setting(key: str):
    return settings_manager.get(key, SETTING_DEFAULTS[key])


#: Identity fields a meeting-state payload may update, kept for the header.
_IDENTITY_KEYS = ("title", "display_title", "started_at", "ended_at", "paused_total_s")

_FINALIZATION_TITLES = {
    "running": "Finalizing Meeting",
    "completed": "Final Insights Ready",
    "disabled": "AI Insights Off",
    "unavailable": "Final Insights Unavailable",
    "failed": "Final Insights Incomplete",
}
_FINALIZATION_DEFAULTS = {
    "running": "Preparing final insights…",
    "completed": "Final insights are ready.",
    "disabled": "AI insights are off for this meeting.",
    "unavailable": "Final insights could not run.",
    "failed": "Final insights failed.",
}

#: Steps that may run again after they completed.
_OPTIONAL_RERUN_STEPS = frozenset({"redecode", "speaker_id", "polish", "consolidation"})


@dataclass
class _FinalizationView:
    """A finalization payload, read once for the card."""

    status: str
    message: str
    current_step: int
    total_steps: int
    step_details: str
    steps: List[Dict[str, Any]]
    content: Dict[str, Any]
    meeting_failed: bool
    empty_meeting: bool

    @classmethod
    def read(cls, finalization: Dict[str, Any]) -> "_FinalizationView":
        status = str(finalization.get("status") or "")
        message = str(finalization.get("message") or "").strip()
        current_step = int(finalization.get("current_step") or 0)
        total_steps = int(finalization.get("total_steps") or 0)
        step_details = str(finalization.get("step_details") or "").strip()
        steps = list(finalization.get("steps") or [])
        content = dict(finalization.get("content_summary") or {})
        return cls(
            status=status,
            message=message,
            current_step=current_step,
            total_steps=total_steps,
            step_details=step_details,
            steps=steps,
            content=content,
            meeting_failed=str(content.get("meeting_status") or "") == "failed",
            empty_meeting=bool(content.get("is_empty", False)),
        )


def _finalization_heading(
    status: str, message: str, has_failed_steps: bool, empty_meeting: bool, meeting_failed: bool
) -> Tuple[str, str]:
    """The card's title and message."""
    if has_failed_steps and status == "completed":
        title = "Meeting Finished With Issues"
        default = "Some post-meeting steps failed. The recording was kept."
    else:
        title = _FINALIZATION_TITLES.get(status, "Final insights")
        default = _FINALIZATION_DEFAULTS.get(status, "")
    if meeting_failed and empty_meeting:
        title = "Meeting Failed"
    elif empty_meeting:
        title = "Empty Meeting"
    return title, message or default


def _step_icon(step_status) -> Tuple[str, str]:
    """A step's status glyph and its colour token."""
    if step_status == "completed":
        return "✓", "@success-text"
    if step_status == "running":
        return "●", "@accent"
    if step_status == "failed":
        return "✗", "@danger-text"
    if step_status == "skipped":
        return "–", "@text-muted"
    return "○", "@text-muted"


def _step_detail_text(step_status) -> str:
    if step_status == "running":
        return "In progress"
    if step_status == "completed":
        return "Done"
    if step_status == "failed":
        return "Failed"
    if step_status == "skipped":
        return "Skipped"
    return "Queued"


def _step_action_label(step_id: str, step_status, allow_actions: bool) -> str:
    """The label for the row's action button; empty when it offers none."""
    if not (allow_actions and step_id):
        return ""
    if step_status in {"failed", "skipped"}:
        return "Retry"
    if step_status == "completed" and step_id in _OPTIONAL_RERUN_STEPS:
        return "Run again"
    return ""


class _BriefEdit(QPlainTextEdit):
    """Brief field whose height is a count of text lines at the live font.

    The stylesheet supplies the font size, border, and padding only once the
    field is polished, and again whenever the UI type size changes, so a pixel
    height fixed in the constructor clips at a larger scale. Qt re-reads the
    size hint after each font or style change.
    """

    VISIBLE_LINES = 3

    def sizeHint(self) -> QSize:
        margins = self.contentsMargins()
        height = (
            self.VISIBLE_LINES * self.fontMetrics().lineSpacing()
            + 2 * round(self.document().documentMargin())
            + margins.top()
            + margins.bottom()
        )
        return QSize(super().sizeHint().width(), height)

    def minimumSizeHint(self) -> QSize:
        return QSize(super().minimumSizeHint().width(), self.sizeHint().height())


class MeetingModeTab(QWidget):
    """Full-page tab with Meeting Mode session controls."""

    start_requested = pyqtSignal(bool, str)  # cloud_enabled, meeting brief
    setup_requested = pyqtSignal()
    demo_requested = pyqtSignal(bool)  # cloud_enabled
    pause_requested = pyqtSignal()
    resume_requested = pyqtSignal()
    end_requested = pyqtSignal()
    open_dashboard_requested = pyqtSignal()
    open_report_requested = pyqtSignal()
    copy_guest_link_requested = pyqtSignal()
    cloud_toggled = pyqtSignal(bool)
    #: "Change…" beside AI insights: open Settings → Meeting Mode → Intelligence.
    ai_settings_requested = pyqtSignal()
    retry_insights_requested = pyqtSignal()
    retry_speakers_requested = pyqtSignal()
    retry_step_requested = pyqtSignal(str)
    background_requested = pyqtSignal()
    defer_insights_requested = pyqtSignal()
    #: A recent meeting on the empty stage was opened: its id.
    past_meeting_requested = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("meetingModeTab")
        self.setAccessibleName("Meeting Mode")
        self.setAccessibleDescription(
            "Start, monitor, and finish a meeting, then open its browser dashboard."
        )

        self._active = False
        self._background_available = False
        self._starting = False
        # Quick Record or a walkthrough holds the microphone.
        self._recording_active = False
        self._paused = False
        self._elapsed_base_s = 0.0
        self._running_since: Optional[float] = None
        self._finalization: Optional[Dict[str, Any]] = None
        self._identity: Dict[str, Any] = {}
        self._meeting_id: Optional[str] = None
        self._has_dashboard = False
        self._can_rerun_speakers = False
        # Whether settings allow the OpenAI speaker pass: not offered hides
        # Re-run speakers, a reason disables it with that reason as the tip.
        self._speaker_rerun_offered = True
        self._speaker_rerun_reason = ""
        self._developer_mode = False
        self._built = False
        self._stage_state: Optional[str] = None
        #: What the finalization step rows were last built from.
        self._steps_shown: Optional[tuple] = None
        self._recent_token = 0
        _relay().loaded.connect(self._on_recent_loaded)

        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.setInterval(1000)
        self._elapsed_timer.timeout.connect(self._refresh_elapsed)

        self._setup_ui()
        self.set_developer_mode(
            bool(_saved_setting(SettingsKey.DEVELOPER_MODE))
        )
        self._apply_layout_state()

    @staticmethod
    def _keep_natural_height(widget: QWidget) -> None:
        """Stop a container from being compressed below its content height.

        The finalization boxes hold fixed-height rows, so the default shrinkable
        policy lets a short window squeeze them into overlapping text instead of
        reporting how much room they need.

        Args:
            widget: Container whose height should never drop below its hint.
        """
        widget.setSizePolicy(
            widget.sizePolicy().horizontalPolicy(),
            QSizePolicy.Policy.Minimum,
        )

    @staticmethod
    def _link_button(text: str, object_name: str, tooltip: str = "") -> QPushButton:
        """Build a flat text action.

        Args:
            text: Button label.
            object_name: QSS object name.
            tooltip: Optional hover text.

        Returns:
            A borderless button that reads as a link, not a filled control.
        """
        button = QPushButton(text)
        button.setObjectName(object_name)
        button.setProperty("meetingLink", True)
        button.setFlat(True)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        if tooltip:
            button.setToolTip(tooltip)
        return button

    @staticmethod
    def _decision_button(
        text: str, object_name: str, tooltip: str = ""
    ) -> QPushButton:
        """Build a filled, initially hidden result action."""
        button = QPushButton(text)
        button.setObjectName(object_name)
        button.setProperty("meetingResultAction", True)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        if tooltip:
            button.setToolTip(tooltip)
        button.hide()
        return button

    def _setup_ui(self):
        """Build the control column and the stage beside it."""
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        self.scroll_area = QScrollArea()
        self.scroll_area.setObjectName("meetingModeScrollArea")
        self.scroll_area.setAccessibleName("Meeting Mode controls")
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll_area.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.scroll_area.setStyleSheet(
            "QScrollArea#meetingModeScrollArea { border: none; "
            "background: transparent; }"
        )
        content = QWidget()
        content.setObjectName("meetingModeContent")
        self.content_widget = content
        self.scroll_area.setWidget(content)
        main_layout.addWidget(self.scroll_area)
        page = QVBoxLayout(content)
        page.setContentsMargins(24, 6, 24, 16)
        page.setSpacing(0)

        self.stage = Stage("meetingStage", STAGE_WIDTH, STAGE_HEIGHT)
        self.split = StudioSplit(
            self._build_column(), self.stage, object_name="meetingStudio"
        )
        page.addWidget(self.split, 1)
        self.empty_pane = self.stage.add(self._build_empty_pane())
        self.live_pane = self.stage.add(self._build_live_pane())
        self.result_pane = self.stage.add(self._build_result_pane())
        self._built = True
        self._apply_sizes()

    @staticmethod
    def _plain(name: str = "meetingPlain") -> QWidget:
        widget = QWidget()
        widget.setObjectName(name)
        return widget

    @staticmethod
    def _pane() -> QFrame:
        pane = QFrame()
        pane.setObjectName("meetingStagePane")
        return pane

    @staticmethod
    def _text(text: str, name: str, *, wrap: bool = False) -> QLabel:
        label = WrappedLabel(text) if wrap else QLabel(text)
        label.setObjectName(name)
        if name == "meetingOverline":
            font = label.font()
            font.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 108)
            label.setFont(font)
        return label

    def _section(self, body: QVBoxLayout, title: str) -> None:
        rule = QFrame()
        rule.setObjectName("meetingRule")
        rule.setFixedHeight(1)
        body.addSpacing(12)
        body.addWidget(rule)
        body.addSpacing(10)
        body.addWidget(self._text(title, "meetingOverline"))
        body.addSpacing(4)

    # ---- the control column ----

    def _build_column(self) -> QFrame:
        """The next meeting: Start, then what it will do, read top to bottom.

        The column is the same in every state but a live meeting, so Start
        is always found in one place. The stage beside it holds the meeting
        that is running or just ended.
        """
        column = self.column = QFrame()
        column.setObjectName("meetingColumn")
        body = QVBoxLayout(column)
        body.setContentsMargins(16, 16, 16, 12)
        body.setSpacing(0)

        # The beta mark belongs to the feature, so it sits on the title
        # rather than beside a note about where speech is sent.
        heading = QHBoxLayout()
        heading.setContentsMargins(0, 0, 0, 12)
        heading.setSpacing(8)
        heading.addWidget(self._text("Meeting Mode", "meetingColumnTitle"))
        self.beta_tag = QLabel("Beta")
        self.beta_tag.setObjectName("meetingColumnBetaTag")
        self.beta_tag.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.beta_tag.setAccessibleName("Meeting Mode is in beta")
        beta_notice = "Transcripts and insights may be inaccurate."
        self.beta_tag.setAccessibleDescription(beta_notice)
        self.beta_tag.setToolTip(beta_notice)
        heading.addWidget(self.beta_tag, 0, Qt.AlignmentFlag.AlignVCenter)
        heading.addStretch(1)
        body.addLayout(heading)

        # One slot holds Start, or the dashboard actions while a meeting
        # runs (Pause and End are on the stage), so the rows below stay put.
        self.top_slot = self._plain()
        slot = QVBoxLayout(self.top_slot)
        slot.setContentsMargins(0, 0, 0, 0)
        slot.setSpacing(8)

        self.idle_card = self._plain("meetingIdleCard")
        idle_layout = QVBoxLayout(self.idle_card)
        idle_layout.setContentsMargins(0, 0, 0, 0)
        idle_layout.setSpacing(8)
        self.start_button = SuccessButton("Start Meeting")
        self.start_button.setProperty("buttonId", "meetingStart")
        self.start_button.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self.start_button.setAccessibleDescription(
            "Begin microphone and supported system-audio capture."
        )
        self.start_button.clicked.connect(self._on_start_clicked)
        idle_layout.addWidget(self.start_button)

        self.demo_button = Button("Load demo meeting")
        self.demo_button.setObjectName("meetingDemoButton")
        self.demo_button.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self.demo_button.setToolTip(
            "Open the dashboard with a fake transcript so you can test "
            "end-of-meeting cleanup and the final report without recording."
        )
        self.demo_button.setAccessibleDescription(
            "Load a fake meeting and open its dashboard without recording audio."
        )
        self.demo_button.clicked.connect(self._on_demo_clicked)
        idle_layout.addWidget(self.demo_button)
        self.demo_hint = WrappedLabel(
            "Loads a fake transcript and opens the dashboard. Turn on "
            "AI insights, then End, to test cleanup and the report."
        )
        self.demo_hint.setObjectName("meetingDemoHint")
        idle_layout.addWidget(self.demo_hint)
        slot.addWidget(self.idle_card)

        self.live_actions = self._plain()
        live_layout = QHBoxLayout(self.live_actions)
        live_layout.setContentsMargins(0, 0, 0, 0)
        live_layout.setSpacing(8)
        self.dashboard_button = Button("Open dashboard")
        self.dashboard_button.setObjectName("meetingDashboardButton")
        self.dashboard_button.setAccessibleDescription(
            "Open the live transcript and meeting insights in your web browser."
        )
        self.dashboard_button.clicked.connect(self.open_dashboard_requested)
        self.guest_link_button = Button("Copy guest link")
        self.guest_link_button.setObjectName("meetingGuestLinkButton")
        self.guest_link_button.setAccessibleDescription(
            "Copy the guest dashboard link to the clipboard."
        )
        self.guest_link_button.clicked.connect(self.copy_guest_link_requested)
        for button in (self.dashboard_button, self.guest_link_button):
            button.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
            )
            button.setMinimumWidth(0)
            live_layout.addWidget(button, 1)
        slot.addWidget(self.live_actions)

        # Under Start while the last meeting is still on the stage: what
        # starting the next one does to it.
        self.slot_note = WrappedLabel("")
        self.slot_note.setObjectName("meetingColumnNote")
        slot.addWidget(self.slot_note)
        slot.addStretch(1)
        body.addWidget(self.top_slot)

        self.speech_destination = WrappedLabel("")
        self.speech_destination.setObjectName("meetingColumnNote")
        self.speech_destination.setContentsMargins(0, 10, 0, 0)
        body.addWidget(self.speech_destination)

        self._section(body, "AI INSIGHTS")
        body.addWidget(self._build_ai_panel())
        body.addWidget(self._build_brief_panel())

        body.addStretch(1)
        self.background_notice = WrappedLabel("")
        self.background_notice.setObjectName("meetingColumnNote")
        self.background_notice.setAccessibleName("Background meeting processing")
        self.background_notice.hide()
        body.addWidget(self.background_notice)
        if meeting_setup_available():
            foot = QHBoxLayout()
            foot.setContentsMargins(0, 10, 0, 0)
            self.setup_link = self._link_button(
                "Meeting setup  →",
                "meetingSetupLink",
                "Check the microphone, system audio and speech model",
            )
            self.setup_link.clicked.connect(self.setup_requested.emit)
            foot.addWidget(self.setup_link)
            foot.addStretch(1)
            body.addLayout(foot)
        return column

    def _build_ai_panel(self) -> QWidget:
        """Build the AI insights switch with what it does and where it runs.

        Returns:
            The panel. It stays visible during a meeting, where the switch
            turns insights on or off live, but shows its details only while
            the next meeting is being set up.
        """
        panel = self.ai_panel = self._plain("meetingAiPanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.setSpacing(8)
        self.ai_label = self._text("AI insights", "meetingRowName")
        self.ai_label.setToolTip(ai_insights_tooltip())
        title_row.addWidget(self.ai_label)
        title_row.addStretch(1)
        # The switch carries the row's name for screen readers; it paints no text.
        self.cloud_checkbox = SettingsSwitch()
        self.cloud_checkbox.setText("AI insights")
        self.cloud_checkbox.setAccessibleName("AI insights")
        self.cloud_checkbox.setChecked(
            bool(_saved_setting(SettingsKey.MEETING_CLOUD_LAST_ENABLED))
        )
        self.cloud_checkbox.setAccessibleDescription(
            "When on, transcript text goes to the AI model chosen in "
            "Settings for live insights and a final report. Speech audio routing "
            "is configured separately in Voice & speakers."
        )
        self.cloud_checkbox.setToolTip(ai_insights_tooltip())
        title_row.addWidget(self.cloud_checkbox)
        layout.addLayout(title_row)

        self.ai_compact_destination = WrappedLabel("", break_long_words=True)
        self.ai_compact_destination.setObjectName("meetingAiCompactDestination")
        self.ai_compact_destination.hide()
        layout.addWidget(self.ai_compact_destination)

        self.ai_details = self._plain("meetingAiDetails")
        details_layout = QVBoxLayout(self.ai_details)
        details_layout.setContentsMargins(0, 0, 0, 0)
        details_layout.setSpacing(4)
        self.ai_description = WrappedLabel(
            "Live notes, insights, and a final report."
        )
        self.ai_description.setObjectName("meetingAiDescription")
        details_layout.addWidget(self.ai_description)
        model_row = QHBoxLayout()
        model_row.setContentsMargins(0, 0, 0, 0)
        model_row.setSpacing(10)
        self.ai_destination = WrappedLabel("", break_long_words=True)
        self.ai_destination.setObjectName("meetingAiDestination")
        self.ai_destination.setAccessibleName("AI insights model")
        model_row.addWidget(self.ai_destination, 1)
        self.ai_settings_button = self._link_button(
            "Change…",
            "meetingAiSettingsButton",
            "Choose the AI model in Settings → Meeting Mode → Intelligence",
        )
        self.ai_settings_button.setAccessibleName("Change the AI insights model")
        self.ai_settings_button.clicked.connect(self.ai_settings_requested)
        model_row.addWidget(self.ai_settings_button, 0, Qt.AlignmentFlag.AlignTop)
        details_layout.addLayout(model_row)
        layout.addWidget(self.ai_details)

        panel.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        self.refresh_ai_destination()
        return panel

    def _build_brief_panel(self) -> QWidget:
        """Build the optional brief as a labelled field with its own hint.

        Returns:
            The panel. It shows only while the next meeting is being set up
            with AI insights on; the dashboard owns the brief once it starts.
        """
        panel = self.brief_panel = self._plain("meetingBriefPanel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 12, 0, 0)
        layout.setSpacing(6)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(8)
        self.brief_label = QLabel("Meeting brief")
        self.brief_label.setObjectName("meetingRowName")
        header.addWidget(self.brief_label)
        header.addStretch()
        optional = QLabel("Optional")
        optional.setObjectName("meetingBriefOptional")
        header.addWidget(optional)
        layout.addLayout(header)

        # The brief is stated before Start so the agent works to it from the
        # first checkpoint. It stays editable on the dashboard afterwards.
        self.brief_input = _BriefEdit()
        self.brief_input.setObjectName("meetingBriefInput")
        self.brief_input.setPlaceholderText(
            "What do you want out of this meeting? e.g. “We decide the vendor "
            "today — capture who objected and why.”"
        )
        self.brief_input.setAccessibleName("Meeting brief")
        self.brief_input.setAccessibleDescription(
            "Optional. What you want out of this meeting's notes, including "
            "anything the AI note taker should watch for. It is read on every "
            "pass and can be changed from the dashboard during the meeting."
        )
        # Tab leaves the field like any other form control; a brief has no
        # use for literal tab characters.
        self.brief_input.setTabChangesFocus(True)
        self.brief_input.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self.brief_input.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.brief_label.setBuddy(self.brief_input)
        layout.addWidget(self.brief_input)

        self.brief_hint = WrappedLabel(
            "AI insights read this on every pass. You can change it from the "
            "dashboard."
        )
        self.brief_hint.setObjectName("meetingBriefHint")
        layout.addWidget(self.brief_hint)
        # Show or hide the brief first, so it has already changed by the time
        # turning the switch on opens the consent dialog.
        self.cloud_checkbox.toggled.connect(self._sync_brief_availability)
        self.cloud_checkbox.toggled.connect(self.cloud_toggled)
        panel.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        return panel

    # ---- the stage ----

    def _build_empty_pane(self) -> QFrame:
        """No meeting on the stage: what Meeting Mode does, and the newest few."""
        pane = self._pane()
        outer = QHBoxLayout(pane)
        outer.setContentsMargins(28, 24, 28, 24)
        outer.addStretch(1)
        column = self._plain()
        column.setMaximumWidth(460)
        column.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        body = QVBoxLayout(column)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addStretch(2)

        self.intro = self._plain("meetingIntro")
        intro_layout = QVBoxLayout(self.intro)
        intro_layout.setContentsMargins(0, 0, 0, 0)
        intro_layout.setSpacing(4)
        intro_layout.addWidget(self._text("No meeting running", "meetingHeading"))
        subtitle_copy, platform_copy = meeting_audio_support_copy()
        self.subtitle = WrappedLabel(subtitle_copy)
        self.subtitle.setObjectName("meetingHint")
        self.subtitle.setToolTip(platform_copy)
        intro_layout.addWidget(self.subtitle)
        self.platform_hint = WrappedLabel(platform_copy, self.intro)
        self.platform_hint.setObjectName("meetingModePlatformHint")
        self.platform_hint.setProperty("tone", meeting_audio_platform_tone())
        self.platform_hint.setAccessibleName("Meeting platform notice")
        self.platform_hint.setVisible(meeting_audio_shows_platform_warning())
        intro_layout.addSpacing(6)
        intro_layout.addWidget(self.platform_hint)
        body.addWidget(self.intro)
        body.addSpacing(22)

        for number, (title, text) in enumerate((
            ("Start and talk", "Press Start Meeting when the call begins."),
            ("Follow along", "Open the dashboard for the live transcript and notes."),
            ("Find it later", "The transcript and audio stay in Past Meetings, "
                              "with a final report when AI insights are on."),
        ), 1):
            step = QHBoxLayout()
            step.setSpacing(12)
            badge = self._text(str(number), "meetingStep")
            badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
            step.addWidget(badge, 0, Qt.AlignmentFlag.AlignTop)
            words = QVBoxLayout()
            words.setSpacing(1)
            words.addWidget(self._text(title, "meetingTitle"))
            words.addWidget(self._text(text, "meetingHint", wrap=True))
            step.addLayout(words, 1)
            body.addLayout(step)
            body.addSpacing(14)

        self.recent_box = self._plain()
        recent = QVBoxLayout(self.recent_box)
        recent.setContentsMargins(0, 10, 0, 0)
        recent.setSpacing(4)
        rule = QFrame()
        rule.setObjectName("meetingRule")
        rule.setFixedHeight(1)
        recent.addWidget(rule)
        recent.addSpacing(12)
        recent.addWidget(self._text("RECENT", "meetingOverline"))
        recent.addSpacing(2)
        self._recent_layout = QVBoxLayout()
        self._recent_layout.setSpacing(2)
        recent.addLayout(self._recent_layout)
        self.recent_box.hide()
        body.addWidget(self.recent_box)
        body.addStretch(3)
        outer.addWidget(column, 4)
        outer.addStretch(1)
        self.recent_rows: List[RecentMeetingRow] = []
        return pane

    def _build_live_pane(self) -> QFrame:
        """A meeting on the stage: the on-air card with its clock, Pause and End."""
        pane = self._pane()
        outer = QVBoxLayout(pane)
        outer.setContentsMargins(28, 24, 28, 24)
        outer.setSpacing(0)
        outer.addStretch(2)

        self.session_card = QFrame()
        self.session_card.setObjectName("meetingOnAir")
        self.session_card.setProperty("tone", "starting")
        card = QVBoxLayout(self.session_card)
        card.setContentsMargins(24, 26, 24, 24)
        card.setSpacing(0)

        status_row = QHBoxLayout()
        status_row.setSpacing(10)
        status_row.addStretch(1)
        self.live_dot = LiveDot()
        status_row.addWidget(self.live_dot, 0, Qt.AlignmentFlag.AlignVCenter)
        self.status_pill = QLabel("Meeting")
        self.status_pill.setObjectName("meetingStatusPill")
        self.status_pill.setProperty("tone", "starting")
        font = self.status_pill.font()
        font.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 114)
        self.status_pill.setFont(font)
        self.status_pill.setAccessibleName("Meeting status: Meeting")
        status_row.addWidget(self.status_pill, 0, Qt.AlignmentFlag.AlignVCenter)
        status_row.addStretch(1)
        card.addLayout(status_row)

        self.elapsed_label = QLabel("00:00")
        self.elapsed_label.setObjectName("meetingElapsedLabel")
        self.elapsed_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.elapsed_label.setAccessibleName("Meeting elapsed time: 00:00")
        card.addWidget(self.elapsed_label)
        card.addSpacing(14)

        controls_row = QHBoxLayout()
        controls_row.setSpacing(12)
        controls_row.addStretch(1)
        self.pause_button = Button("Pause")
        self.pause_button.setObjectName("meetingPauseButton")
        self.pause_button.setAccessibleDescription("Pause meeting capture.")
        self.pause_button.clicked.connect(self._on_pause_clicked)
        controls_row.addWidget(self.pause_button)
        self.end_button = DangerButton("End")
        self.end_button.setObjectName("meetingEndButton")
        self.end_button.setAccessibleDescription(
            "Stop capture and begin post-meeting finalization."
        )
        self.end_button.clicked.connect(self.end_requested)
        controls_row.addWidget(self.end_button)
        controls_row.addStretch(1)
        card.addLayout(controls_row)

        centre = QHBoxLayout()
        centre.addStretch(1)
        centre.addWidget(self.session_card, 10)
        centre.addStretch(1)
        outer.addLayout(centre)
        outer.addSpacing(16)
        self.live_hint = self._text(
            "The wrap-up opens here when you end the meeting.", "meetingHint", wrap=True
        )
        self.live_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        outer.addWidget(self.live_hint)
        outer.addStretch(3)
        return pane

    def _build_result_pane(self) -> QFrame:
        """The meeting that just ended: its wrap-up steps and what to do next."""
        pane = self._pane()
        outer = QVBoxLayout(pane)
        outer.setContentsMargins(0, 0, 0, 0)
        # Its own scrolling: a long checklist never grows the window.
        self.result_scroll = QScrollArea()
        self.result_scroll.setObjectName("meetingResultScroll")
        self.result_scroll.setWidgetResizable(True)
        self.result_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.result_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        host = self._plain("meetingResultHost")
        host_layout = QVBoxLayout(host)
        host_layout.setContentsMargins(20, 20, 20, 20)
        self._build_finalization_card()
        host_layout.addWidget(self.finalization_card)
        host_layout.addStretch(1)
        self.result_scroll.setWidget(host)
        outer.addWidget(self.result_scroll)
        return pane

    def _build_finalization_card(self) -> None:
        # Post-meeting finalization / result card
        self.finalization_card = Card()
        self.finalization_card.setObjectName("meetingFinalizationCard")
        self.finalization_card.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.finalization_card.setMinimumHeight(0)
        self._keep_natural_height(self.finalization_card)
        self.finalization_card.setProperty("finalizationTone", "neutral")

        self.identity_box = QWidget()
        self.identity_box.setObjectName("meetingFinalizationIdentity")
        identity_layout = QVBoxLayout(self.identity_box)
        identity_layout.setContentsMargins(0, 0, 0, 0)
        identity_layout.setSpacing(4)

        self.identity_title = WrappedLabel("", break_long_words=True)
        self.identity_title.setObjectName("meetingFinalizationIdentityTitle")
        self.identity_title.setFont(QFont("Segoe UI", 15, QFont.Weight.Bold))
        self.identity_title.setTextFormat(Qt.TextFormat.PlainText)
        identity_layout.addWidget(self.identity_title)

        details_row = QHBoxLayout()
        details_row.setSpacing(12)
        self.identity_meta = WrappedLabel("")
        self.identity_meta.setObjectName("meetingFinalizationIdentityMeta")
        self.identity_meta.setFont(QFont("Segoe UI", 11))
        details_row.addWidget(self.identity_meta, 1, Qt.AlignmentFlag.AlignTop)

        record_actions = QVBoxLayout()
        record_actions.setSpacing(6)
        record_actions.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.finalization_report_button = self._link_button(
            "Open report",
            "meetingFinalizationReportButton",
            "Open the final report in the web dashboard",
        )
        self.finalization_report_button.clicked.connect(self.open_report_requested)
        record_actions.addWidget(
            self.finalization_report_button, 0, Qt.AlignmentFlag.AlignRight
        )
        self.finalization_retry_speakers_button = self._link_button(
            "Re-run speakers",
            "meetingFinalizationRetrySpeakersButton",
        )
        self.finalization_retry_speakers_button.clicked.connect(
            self.retry_speakers_requested.emit
        )
        self.finalization_retry_speakers_button.hide()
        record_actions.addWidget(
            self.finalization_retry_speakers_button, 0, Qt.AlignmentFlag.AlignRight
        )
        details_row.addLayout(record_actions)
        identity_layout.addLayout(details_row)
        self.finalization_card.layout.addWidget(self.identity_box)

        identity_divider = QFrame()
        identity_divider.setObjectName("meetingFinalizationDivider")
        identity_divider.setFrameShape(QFrame.Shape.HLine)
        identity_divider.setFixedHeight(1)
        self.finalization_card.layout.addWidget(identity_divider)

        # Header row with title and step badge
        fin_header_layout = QHBoxLayout()
        fin_header_layout.setContentsMargins(0, 0, 0, 0)
        fin_header_layout.setSpacing(8)

        self.finalization_title = WrappedLabel("Final insights")
        self.finalization_title.setObjectName("meetingFinalizationTitle")
        self.finalization_title.setFont(QFont("Segoe UI", 13, QFont.Weight.DemiBold))
        fin_header_layout.addWidget(self.finalization_title, 1)

        self.finalization_step_badge = QLabel("")
        self.finalization_step_badge.setObjectName("meetingFinalizationStepBadge")
        self.finalization_step_badge.setFont(QFont("Segoe UI", 10, QFont.Weight.DemiBold))
        self.finalization_step_badge.setMinimumHeight(20)
        self.finalization_step_badge.hide()
        fin_header_layout.addWidget(self.finalization_step_badge)

        self.finalization_retry_button = self._decision_button(
            "Retry failed steps",
            "meetingFinalizationRetryButton",
            "Run every failed post-meeting step again.",
        )
        self.finalization_retry_button.clicked.connect(self.retry_insights_requested)
        fin_header_layout.addWidget(self.finalization_retry_button)
        self.finalization_card.layout.addLayout(fin_header_layout)

        # Progress bar
        self.finalization_progress = QProgressBar()
        self.finalization_progress.setObjectName("meetingFinalizationProgress")
        self.finalization_progress.setTextVisible(False)
        self.finalization_progress.setAccessibleName("Meeting finalization progress")
        self.finalization_progress.setMaximumHeight(6)
        self.finalization_progress.hide()
        self.finalization_card.layout.addWidget(self.finalization_progress)

        # Active status / details highlight container
        self.finalization_active_box = QWidget()
        self.finalization_active_box.setObjectName("meetingFinalizationActiveBox")
        active_box_layout = QVBoxLayout(self.finalization_active_box)
        active_box_layout.setContentsMargins(10, 8, 10, 8)
        active_box_layout.setSpacing(4)

        self.finalization_message = WrappedLabel("", break_long_words=True)
        self.finalization_message.setObjectName("meetingFinalizationMessage")
        self.finalization_message.setAccessibleName("Meeting finalization status")
        self.finalization_message.setTextFormat(Qt.TextFormat.PlainText)
        active_box_layout.addWidget(self.finalization_message)

        self.finalization_detail = WrappedLabel("")
        self.finalization_detail.setObjectName("meetingFinalizationDetail")
        active_box_layout.addWidget(self.finalization_detail)
        self.finalization_card.layout.addWidget(self.finalization_active_box)

        # Multi-step pipeline / checklist container
        self.finalization_steps_widget = QWidget()
        self.finalization_steps_widget.setObjectName("meetingFinalizationStepsWidget")
        self.finalization_steps_layout = QVBoxLayout(self.finalization_steps_widget)
        # Matches the active box's inner padding so both inset the same amount
        # from the card. QSS padding does not inset a plain widget's layout.
        self.finalization_steps_layout.setContentsMargins(10, 8, 10, 8)
        self.finalization_steps_layout.setSpacing(6)
        self.finalization_steps_widget.hide()
        self._keep_natural_height(self.finalization_steps_widget)
        self.finalization_card.layout.addWidget(self.finalization_steps_widget)

        # Secondary actions stay with the meeting record.
        self.finalization_footer = QWidget()
        self.finalization_footer.setObjectName("meetingFinalizationFooter")
        footer_layout = QVBoxLayout(self.finalization_footer)
        footer_layout.setContentsMargins(0, 0, 0, 0)
        footer_layout.setSpacing(6)

        footer_divider = QFrame()
        footer_divider.setObjectName("meetingFinalizationDivider")
        footer_divider.setFrameShape(QFrame.Shape.HLine)
        footer_divider.setFixedHeight(1)
        footer_layout.addWidget(footer_divider)

        actions_layout = QHBoxLayout()
        actions_layout.setSpacing(16)

        self.finalization_background_button = self._decision_button(
            "Continue in the background",
            "meetingFinalizationBackgroundButton",
            "Keep preparing this meeting in the background and return to "
            "Start Meeting. Results will be available in Past Meetings.",
        )
        self.finalization_background_button.clicked.connect(
            self.background_requested.emit
        )
        actions_layout.addWidget(self.finalization_background_button)

        self.finalization_keep_later_button = self._decision_button(
            "Keep for later",
            "meetingFinalizationKeepLaterButton",
            "Hide this card and return to idle. The meeting stays in Past "
            "Meetings until you open it again.",
        )
        self.finalization_keep_later_button.clicked.connect(
            self.defer_insights_requested.emit
        )
        actions_layout.addWidget(self.finalization_keep_later_button)

        self.finalization_done_button = self._decision_button(
            "Done",
            "meetingFinalizationDoneButton",
            "This meeting is already in Past Meetings. Hide this card and "
            "return to idle.",
        )
        self.finalization_done_button.clicked.connect(
            self.defer_insights_requested.emit
        )
        actions_layout.addWidget(self.finalization_done_button)
        actions_layout.addStretch()
        footer_layout.addLayout(actions_layout)

        self.finalization_keep_hint = WrappedLabel("")
        self.finalization_keep_hint.setObjectName("meetingFinalizationKeepHint")
        self.finalization_keep_hint.hide()
        footer_layout.addWidget(self.finalization_keep_hint)
        self.finalization_card.layout.addWidget(self.finalization_footer)

    def _sync_brief_availability(self, *_args) -> None:
        """Show the brief only while AI insights are on.

        The brief exists to steer AI insights. Without them it would only
        label the meeting on the dashboard and in the export, so with the
        switch off the panel is hidden and nothing typed there is sent. The
        text stays for when the switch comes back on. It is a pre-meeting
        control: once a meeting starts, the dashboard owns the brief.
        """
        self.brief_panel.setVisible(
            self.cloud_checkbox.isChecked() and self._setting_up()
        )

    def _setting_up(self) -> bool:
        """True while the column is setting up the next meeting.

        That is every state but a starting or live meeting: a result on the
        stage never takes the next meeting's controls away.
        """
        return not (self._active or self._starting)

    def _wrapping_up(self) -> bool:
        """True while the last meeting's post-meeting steps are running."""
        return (self._finalization or {}).get("status") == "running"

    def refresh_ai_destination(self) -> None:
        """Re-read which model AI insights will use for the next meeting.

        A running meeting keeps the endpoint it started with, so the line is
        left alone until it ends rather than showing a later Settings change.
        """
        if self._active:
            return
        from services.settings import resolve_meeting_speech
        from services.remote_asr.settings import load_client_pairing
        engine = resolve_meeting_speech()
        if engine.source == "remote":
            pairing = load_client_pairing()
            host = pairing.host_name if pairing else "a host (not paired yet)"
            speech = f"Remote speech · microphone and system audio sent to {host}. Recordings saved here."
        else:
            # The note says why this isn't the dictation engine.
            speech = f"{engine.note} Speech recognition runs on this computer.".strip()
        self.speech_destination.setText(speech)
        where, privacy = ai_insights_destination()
        line = f"{where} — {privacy}."
        if line == self.ai_destination.text():
            return
        self.ai_destination.setText(line)
        self.ai_compact_destination.setText(where)
        self.ai_compact_destination.setToolTip(line)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.refresh_ai_destination()
        if self.stage_state == "empty":
            self._load_recent()

    def event(self, event) -> bool:
        # Coming back from Settings is how the model usually changes, and
        # Past Meetings can delete what the empty stage lists.
        if event.type() == QEvent.Type.WindowActivate:
            self.refresh_ai_destination()
            if self.stage_state == "empty" and self.isVisible():
                self._load_recent()
        return super().event(event)

    def meeting_brief(self) -> str:
        """The host's pre-meeting brief, trimmed and capped for the engine.

        Empty while AI insights are off: the field is hidden then, so text
        left in it must not reach the meeting.
        """
        if not self.cloud_checkbox.isChecked():
            return ""
        # Imported here rather than at module scope so the validation layer
        # stays the single source of the cap without widening what the main
        # window loads at startup.
        from meeting.state.patches import MAX_INTENT_LEN

        return self.brief_input.toPlainText().strip()[:MAX_INTENT_LEN]

    def _on_start_clicked(self):
        """Emit the start request with the current cloud choice and brief.

        A last meeting that is still wrapping up is sent to the background
        first. A finished or incomplete one needs nothing here: the runtime
        files it in Past Meetings as part of starting.
        """
        if self._wrapping_up() and self._background_available:
            self.background_requested.emit()
        self.start_requested.emit(
            self.cloud_checkbox.isChecked(), self.meeting_brief()
        )

    def _on_demo_clicked(self):
        """Emit the developer-mode demo meeting request."""
        self.demo_requested.emit(self.cloud_checkbox.isChecked())

    def _on_pause_clicked(self):
        """Route the pause/resume button to the matching signal."""
        if self._paused:
            self.resume_requested.emit()
        else:
            self.pause_requested.emit()

    def set_developer_mode(self, enabled: bool) -> None:
        """Show or hide demo-meeting controls.

        Args:
            enabled: True when Settings → Advanced → Developer mode is on.
        """
        self._developer_mode = bool(enabled)
        self._apply_layout_state()

    def set_meeting_state(self, payload: Dict[str, Any]) -> None:
        """Apply a (possibly partial) meeting-state payload.

        Args:
            payload: Dict with any of ``active`` (bool), ``paused`` (bool),
                ``status`` (str), ``cloud_enabled`` (bool), ``elapsed_s``
                (float; resets the elapsed timer base), ``finalization``
                (``{status, message}`` or ``None`` to clear), and
                ``dashboard_available`` (bool).
        """
        if not isinstance(payload, dict):
            return

        self._apply_background(payload)
        self._apply_cloud_choice(payload)
        self._apply_speech_destination(payload.get("speech"))

        if "elapsed_s" in payload:
            self._restart_elapsed(payload["elapsed_s"])

        if "paused" in payload:
            self._set_paused(bool(payload["paused"]))

        if "status" in payload:
            self._apply_status(str(payload["status"]), payload)

        if "dashboard_available" in payload:
            self._has_dashboard = bool(payload["dashboard_available"])

        if "meeting_id" in payload:
            meeting_id = payload.get("meeting_id")
            self._meeting_id = str(meeting_id) if meeting_id else None

        self._apply_identity(payload)

        if "finalization" in payload:
            self._set_finalization(payload.get("finalization"))

        if "active" in payload:
            self._set_active(bool(payload["active"]))
        else:
            self._apply_layout_state()

    def _apply_background(self, payload: Dict[str, Any]) -> None:
        if "background_available" in payload:
            self._background_available = bool(payload["background_available"])
        if "background_message" in payload:
            message = str(payload["background_message"] or "")
            self.background_notice.setText(message)
            self.background_notice.setVisible(bool(message))

    def _apply_cloud_choice(self, payload: Dict[str, Any]) -> None:
        if "cloud_enabled" not in payload:
            return
        self.cloud_checkbox.blockSignals(True)
        self.cloud_checkbox.setChecked(bool(payload["cloud_enabled"]))
        self.cloud_checkbox.blockSignals(False)
        # Blocked signals skip the toggled slots, so the brief (for
        # instance after a declined consent dialog) syncs here instead.
        self._sync_brief_availability()

    def _apply_speech_destination(self, speech) -> None:
        """Say where remote meeting speech goes; local speech keeps the default copy."""
        if not (isinstance(speech, dict) and speech.get("source") == "remote"):
            return
        if speech.get("connected"):
            state = "Audio sent to this host; recordings saved here."
        else:
            state = str(speech.get("message") or "Reconnecting…")
        self.speech_destination.setText(
            f"Remote speech · {speech.get('host', '')} · {speech.get('model', '')}. " + state
        )

    def _restart_elapsed(self, elapsed_s) -> None:
        """Restart the elapsed clock from the runtime's count."""
        try:
            self._elapsed_base_s = float(elapsed_s)
        except (TypeError, ValueError):
            self._elapsed_base_s = 0.0
        # A paused meeting's clock stays where the runtime put it.
        self._running_since = None if self._paused else time.monotonic()
        self._refresh_elapsed()

    def _apply_status(self, status: str, payload: Dict[str, Any]) -> None:
        # Engine status may arrive before the runtime confirms startup.
        # Keep the session card visible and controls disabled until then.
        self._starting = status == "starting" or (
            self._starting
            and status in {"active", "paused"}
            and not payload.get("active", self._active)
        )
        self.set_status_text("Starting" if self._starting else status.capitalize())
        # A new meeting start clears any previous finalization result.
        if status == "starting":
            self._background_available = False
            self._finalization = None
            self._identity = {}
            # The engine snapshots its endpoint now; show that one.
            self.refresh_ai_destination()

    def _apply_identity(self, payload: Dict[str, Any]) -> None:
        for key in _IDENTITY_KEYS:
            if key in payload:
                self._identity[key] = payload.get(key)

    def set_recording_active(self, active: bool) -> None:
        """Keep past meetings available while another mode is recording."""
        self._recording_active = bool(active)
        self._apply_layout_state()

    def set_status_text(self, text: str) -> None:
        """Update the status pill label.

        Args:
            text: Short status string (e.g. "Active", "Paused", "Ending").
        """
        if text:
            self.status_pill.setText(text)
            self.status_pill.setAccessibleName(f"Meeting status: {text}")

    def set_dashboard_available(self, available: bool) -> None:
        """Enable Open Dashboard when a retained URL exists.

        Args:
            available: True when the runtime still holds a dashboard URL.
        """
        self._has_dashboard = bool(available)
        self._apply_layout_state()

    @staticmethod
    def _is_incomplete_finalization(finalization: Optional[Dict[str, Any]]) -> bool:
        """True when the card is a failed / unavailable / partial-failure result.

        Args:
            finalization: Current finalization payload, or None.

        Returns:
            True when the card offers Retry and Keep for later, not Done.
        """
        if not finalization:
            return False
        status = str(finalization.get("status") or "")
        failed_steps = any(
            str(step.get("status") or "") == "failed"
            for step in list(finalization.get("steps") or [])
        )
        return bool(failed_steps) or status in {"failed", "unavailable"}

    def _set_incomplete_actions_visible(self, visible: bool) -> None:
        """Show or hide Keep for later and its hint.

        Args:
            visible: True on incomplete insight cards only.
        """
        self.finalization_keep_hint.setVisible(visible)
        self.finalization_keep_later_button.setVisible(visible)

    def _set_done_visible(self, visible: bool) -> None:
        """Show or hide Done on a completed or disabled leftover card.

        Args:
            visible: True on clean completed / disabled leftovers only.
        """
        self.finalization_done_button.setVisible(visible)

    def _set_finalization(self, value: Any) -> None:
        """Store a finalization payload or clear it.

        Args:
            value: ``None`` clears the card; a mapping keeps ``status`` /
                ``message`` and multi-step progression fields for the persistent
                result view.
        """
        if value is None:
            self._finalization = None
            self._identity = {}
            return
        if not isinstance(value, dict):
            return
        status = str(value.get("status") or "").strip()
        if not status:
            self._finalization = None
            return
        self._finalization = {
            "status": status,
            "message": str(value.get("message") or ""),
            "stage": str(value.get("stage") or ""),
            "current_step": int(value.get("current_step") or 0),
            "total_steps": int(value.get("total_steps") or 0),
            "step_details": str(value.get("step_details") or ""),
            "steps": list(value.get("steps") or []),
            "summary_stats": dict(value.get("summary_stats") or {}),
            "content_summary": dict(value.get("content_summary") or {}),
        }

    def _set_active(self, active: bool) -> None:
        """Switch between the idle and in-meeting layouts."""
        if active == self._active:
            self._apply_layout_state()
            return
        self._active = active
        if active:
            self._starting = False
            # Starting a live session replaces any prior finalization result.
            self._finalization = None
            self._identity = {}
            if self._running_since is None:
                self._elapsed_base_s = 0.0
                self._running_since = time.monotonic()
            self._elapsed_timer.start()
        else:
            self._elapsed_timer.stop()
            self._paused = False
            self._elapsed_base_s = 0.0
            self._running_since = None
            self.pause_button.setText("Pause")
            self.pause_button.setAccessibleDescription("Pause meeting capture.")
            self.elapsed_label.setText("00:00")
            self.elapsed_label.setAccessibleName("Meeting elapsed time: 00:00")
        self._apply_layout_state()

    def _set_paused(self, paused: bool) -> None:
        """Freeze or resume the elapsed timer to mirror the meeting clock."""
        if paused == self._paused:
            return
        self._paused = paused
        now = time.monotonic()
        if paused:
            if self._running_since is not None:
                self._elapsed_base_s += now - self._running_since
                self._running_since = None
            self.pause_button.setText("Resume")
            self.pause_button.setAccessibleDescription("Resume meeting capture.")
        else:
            self._running_since = now
            self.pause_button.setText("Pause")
            self.pause_button.setAccessibleDescription("Pause meeting capture.")

    def _apply_layout_state(self) -> None:
        """Show idle, in-meeting, and/or finalization controls."""
        finalization = self._finalization
        final_status = (finalization or {}).get("status") or ""
        running_finalization = final_status == "running"
        show_finalization = bool(finalization) and not self._active
        showing_session = self._active or self._starting

        self.intro.setVisible(not show_finalization)
        self.session_card.setVisible(showing_session)
        setting_up = self._setting_up()
        self.idle_card.setVisible(setting_up)
        self.live_actions.setVisible(showing_session)
        # Start never leaves the column. It only waits while the last
        # meeting still holds the speech model; after that the wrap-up can
        # finish in the background.
        start_blocked = running_finalization and not self._background_available
        self.start_button.setEnabled(
            not self._recording_active and not start_blocked
        )
        self.demo_button.setEnabled(not self._recording_active)
        if start_blocked:
            note = (
                "Your last meeting is still wrapping up. Start unlocks in "
                "a moment."
            )
        elif running_finalization:
            note = "Your last meeting finishes wrapping up in the background."
        elif self._is_incomplete_finalization(finalization):
            note = (
                "Your last meeting stays in Past Meetings, where you can "
                "pick it up later."
            )
        else:
            note = ""
        self.slot_note.setText(note)
        self.slot_note.setVisible(setting_up and bool(note))
        self.start_button.setToolTip(note if start_blocked else "")
        self._sync_brief_availability()
        self._show_stage(
            "live" if showing_session else "result" if show_finalization else "empty"
        )
        self._sync_live_tone()
        # The switch always stays (it acts on a live meeting), but what it
        # does and "Change…" only matter while the next meeting is being
        # set up.
        self.ai_details.setVisible(setting_up)
        self.ai_settings_button.setVisible(setting_up)
        self.ai_compact_destination.setVisible(not setting_up)
        ai_name = "AI insights for next meeting" if show_finalization else "AI insights"
        self.cloud_checkbox.setText(ai_name)
        self.cloud_checkbox.setAccessibleName(ai_name)
        self.ai_label.setText(ai_name)
        show_demo = (
            self._developer_mode
            and not self._active
            and not running_finalization
        )
        self.demo_button.setVisible(show_demo)
        self.demo_hint.setVisible(show_demo)
        self.finalization_card.setVisible(show_finalization)
        self.finalization_background_button.setVisible(
            show_finalization and running_finalization and self._background_available
        )
        self.pause_button.setEnabled(self._active)
        self.end_button.setEnabled(self._active)
        self.guest_link_button.setEnabled(self._active and self._has_dashboard)

        if show_finalization and finalization is not None:
            self._render_identity()
            self._render_finalization(finalization)

        self.dashboard_button.setEnabled(self._active or self._has_dashboard)
        self.finalization_report_button.setVisible(show_finalization)
        self.finalization_footer.setVisible(
            show_finalization and any(
                not button.isHidden()
                for button in self.finalization_footer.findChildren(QPushButton)
            )
        )

    # ---- the stage ----

    @property
    def stage_state(self) -> str:
        """What the stage shows: "empty", "live" or "result"."""
        return self._stage_state or "empty"

    def _show_stage(self, state: str) -> None:
        changed = state != self._stage_state
        self._stage_state = state
        self.stage.show_only({
            "live": self.live_pane, "result": self.result_pane, "empty": self.empty_pane,
        }[state])
        if changed and state == "empty" and self.isVisible():
            self._load_recent()

    def _sync_live_tone(self) -> None:
        """Colour the on-air card for what capture is doing right now."""
        tone = "starting" if not self._active else "paused" if self._paused else "live"
        for widget in (self.session_card, self.status_pill):
            set_style_property(widget, "tone", tone)
        self.live_dot.set_live(tone == "live")
        # Pause becomes Resume, and a button sizes itself to its new label.
        for button in (self.pause_button, self.end_button):
            button.setFixedWidth(scaled(ON_AIR_BUTTON_WIDTH))

    def _apply_sizes(self) -> None:
        """Size the column and the on-air card for the type size in use."""
        if not getattr(self, "_built", False):
            return
        self.split.set_column_width(scaled(COLUMN_WIDTH))
        self.top_slot.setMinimumHeight(scaled(TOP_SLOT_HEIGHT))
        for button in (self.dashboard_button, self.guest_link_button):
            button.setMinimumHeight(scaled(TOP_SLOT_HEIGHT))
        self.session_card.setMaximumWidth(scaled(ON_AIR_WIDTH))
        self._sync_live_tone()

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.StyleChange:
            # A new type size restyles every widget; the column follows it.
            self._apply_sizes()

    def preferred_width(self) -> int:
        """Room for the column beside the stage, at any type size."""
        return self.split.column_width + GAP + scaled(STAGE_WIDTH)

    # ---- recent meetings (the empty stage) ----

    def _load_recent(self) -> None:
        """Read the newest few meetings for the empty stage, off the UI thread."""
        self._recent_token = token = next(_tokens)
        loaded = _relay().loaded

        def load() -> None:
            try:
                meetings = read_recent_meetings()
            except Exception:
                logger.debug("Could not read recent meetings", exc_info=True)
                meetings = []
            loaded.emit(token, meetings)

        threading.Thread(target=load, name="meeting-recent-load", daemon=True).start()

    def _on_recent_loaded(self, token: int, meetings) -> None:
        if token != self._recent_token:
            return
        self._recent_token = 0
        self.set_recent_meetings(meetings)

    def set_recent_meetings(self, meetings) -> None:
        """Show ``meetings`` (newest first) as the rows the empty stage opens."""
        for row in self.recent_rows:
            row.hide()
            row.setParent(None)
            row.deleteLater()
        self.recent_rows = []
        for meeting in list(meetings)[:RECENT_SHOWN]:
            row = RecentMeetingRow(meeting)
            row.clicked.connect(self.past_meeting_requested)
            self._recent_layout.addWidget(row)
            self.recent_rows.append(row)
        self.recent_box.setVisible(bool(self.recent_rows))

    def _render_identity(self) -> None:
        """Show the meeting name, date, and duration above its result."""
        identity = self._identity
        title = str(
            identity.get("display_title") or identity.get("title") or ""
        ).strip()
        meta = format_meeting_identity_meta(identity) if identity else ""
        if meta == UNKNOWN_DATE and not identity.get("started_at"):
            meta = ""
        self.identity_title.setText(title or "Meeting result")
        self.identity_meta.setText(meta)
        self.identity_meta.setVisible(bool(meta))

    def _render_finalization(self, finalization: Dict[str, Any]) -> None:
        """Update finalization card copy, tone, and step visibility.

        Args:
            finalization: Mapping with status, message, and steps.
        """
        view = _FinalizationView.read(finalization)
        content = view.content
        self._can_rerun_speakers = bool(
            content.get("can_rerun_speakers", False)
        )
        self._speaker_rerun_offered = bool(
            content.get("speaker_rerun_offered", True)
        )
        self._speaker_rerun_reason = str(
            content.get("speaker_rerun_reason") or ""
        )
        self._update_report_button(view)
        if view.empty_meeting:
            view.message = (
                "No audio or transcript was captured. The meeting failed "
                "before it could start, so no dashboard was created."
                if view.meeting_failed
                else "No audio or transcript was captured for this meeting."
            )

        failed_steps = [
            step for step in view.steps
            if str(step.get("status") or "") == "failed"
        ]
        needs_retry = bool(failed_steps) or view.status in {"failed", "unavailable"}
        title, shown_message = _finalization_heading(
            view.status, view.message, bool(failed_steps), view.empty_meeting, view.meeting_failed
        )
        self.finalization_title.setText(title)
        self.finalization_message.setText(shown_message)
        self.finalization_card.setAccessibleName(self.finalization_title.text())
        self.finalization_card.setAccessibleDescription(
            self.finalization_message.text()
        )
        self.finalization_keep_hint.setText(
            "This failed start has no audio or transcript."
            if view.empty_meeting
            else "This meeting, transcript, and audio stay in Past Meetings."
        )
        self.finalization_retry_speakers_button.setToolTip(
            self._speaker_rerun_tip()
        )

        if view.status == "running":
            tone = "neutral"
            self._show_running_finalization(view)
        else:
            self.finalization_step_badge.hide()
            self.finalization_progress.hide()
            self.finalization_progress.setRange(0, 1)
            self.finalization_progress.setValue(0)
            has_speaker_step = any(
                str(step.get("id") or "") == "speaker_id" for step in view.steps
            )

            if view.status == "completed" and not failed_steps:
                tone = "success"
                self._show_completed_finalization(view, has_speaker_step)
            elif needs_retry:
                tone = "warning"
                self._show_retry_finalization(view, has_speaker_step)
            else:
                tone = "info"
                self._show_off_finalization()

        self._apply_finalization_tone(tone)

    def _update_report_button(self, view: _FinalizationView) -> None:
        no_report = view.empty_meeting or view.meeting_failed or view.status == "running"
        self.finalization_report_button.setEnabled(not no_report)
        if view.empty_meeting or view.meeting_failed:
            report_tip = "No report for this meeting"
        elif view.status == "running":
            report_tip = "The final report is still being prepared"
        else:
            report_tip = "Open the final report in the web dashboard"
        self.finalization_report_button.setToolTip(report_tip)

    def _show_running_finalization(self, view: _FinalizationView) -> None:
        self.finalization_progress.show()
        self.finalization_retry_button.hide()
        self.finalization_retry_speakers_button.hide()
        self._set_incomplete_actions_visible(False)
        self._set_done_visible(False)
        self.finalization_active_box.show()

        if view.total_steps > 0:
            self.finalization_step_badge.setText(
                f"Step {view.current_step} of {view.total_steps}"
            )
            # Progress calculation
            pct = int(max(5, min(95, ((view.current_step - 0.5) / view.total_steps) * 100)))
            self.finalization_progress.setRange(0, 100)
            self.finalization_progress.setValue(pct)
        else:
            self.finalization_step_badge.setText("In progress")
            # Indeterminate only when steps not configured
            self.finalization_progress.setRange(0, 0)

        self.finalization_step_badge.show()
        self._show_step_details(view)
        self._show_steps(view.steps, view.current_step, allow_actions=False)

    def _show_completed_finalization(self, view: _FinalizationView, has_speaker_step: bool) -> None:
        self.finalization_retry_button.hide()
        self._offer_speaker_rerun(not has_speaker_step and self._speaker_rerun_offered)
        self._set_incomplete_actions_visible(False)
        self._set_done_visible(True)
        # Header + checklist already cover a successful finish; the
        # stats recap lives on the dashboard instead.
        self.finalization_active_box.setVisible(view.empty_meeting)
        self.finalization_detail.hide()
        self._show_steps(view.steps, len(view.steps))

    def _show_retry_finalization(
        self, view: _FinalizationView, has_speaker_step: bool
    ) -> None:
        self.finalization_retry_button.show()
        self.finalization_retry_button.setEnabled(True)
        self._offer_speaker_rerun(not has_speaker_step and self._speaker_rerun_offered)
        self._set_incomplete_actions_visible(True)
        self._set_done_visible(False)
        self.finalization_active_box.show()

        self._show_step_details(view)
        self._show_steps(view.steps, view.current_step)

    def _show_off_finalization(self) -> None:
        self.finalization_active_box.show()
        self.finalization_detail.hide()
        self.finalization_steps_widget.hide()
        self.finalization_retry_button.hide()
        self._offer_speaker_rerun(self._speaker_rerun_offered)
        self._set_incomplete_actions_visible(False)
        self._set_done_visible(True)

    def _offer_speaker_rerun(self, visible: bool) -> None:
        self.finalization_retry_speakers_button.setVisible(visible)
        self.finalization_retry_speakers_button.setEnabled(
            self._speaker_rerun_allowed()
        )

    def _show_step_details(self, view: _FinalizationView) -> None:
        """The current step's detail line, unless it repeats the message."""
        if view.step_details and view.step_details != view.message:
            self.finalization_detail.setText(view.step_details)
            self.finalization_detail.show()
        else:
            self.finalization_detail.hide()

    def _show_steps(
        self, steps: List[Dict[str, Any]], current_step: int, allow_actions: bool = True
    ) -> None:
        if steps:
            self._populate_steps(steps, current_step, allow_actions=allow_actions)
            self.finalization_steps_widget.show()
        else:
            self.finalization_steps_widget.hide()

    def _apply_finalization_tone(self, tone: str) -> None:
        self.finalization_card.setProperty("finalizationTone", tone)
        # The title's colour follows its card's tone.
        for widget in (self.finalization_card, self.finalization_title):
            repolish(widget)

    def _speaker_rerun_allowed(self) -> bool:
        """Whether a speaker re-run could label anything right now."""
        return self._can_rerun_speakers and not self._speaker_rerun_reason

    def _speaker_rerun_tip(self) -> str:
        """Tooltip for the speaker re-run controls, naming what blocks them."""
        if not self._can_rerun_speakers:
            return (
                "No system-audio recording is available for speaker "
                "identification"
            )
        return self._speaker_rerun_reason or "Re-run speaker identification"

    def _populate_steps(
        self,
        steps: List[Dict[str, Any]],
        current_step: int,
        allow_actions: bool = True,
    ) -> None:
        """Render the step pipeline rows.

        Args:
            steps: List of step dicts with id, name, status, detail.
            current_step: 1-based index of current step.
            allow_actions: When False, hide per-row Retry / Run again.
        """
        # Every state update lands here. Rebuilding rows that would come out
        # the same takes keyboard focus off a Retry button for nothing.
        shown = (
            tuple(
                (str(s.get("id") or ""), str(s.get("status", "pending")),
                 str(s.get("name") or ""), str(s.get("detail") or ""))
                for s in steps
            ),
            allow_actions,
            self._speaker_rerun_allowed(),
            self._speaker_rerun_tip(),
        )
        if shown == self._steps_shown:
            return
        self._steps_shown = shown
        while self.finalization_steps_layout.count():
            item = self.finalization_steps_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        for idx, s in enumerate(steps, 1):
            self._add_step_row(idx, s, allow_actions)

    def _add_step_row(self, idx: int, s: Dict[str, Any], allow_actions: bool) -> None:
        row = QWidget()
        row.setObjectName("meetingFinalizationStepRow")
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 3, 0, 3)
        row_layout.setSpacing(8)

        step_id = str(s.get("id") or "")
        step_status = s.get("status", "pending")
        step_name = str(s.get("name") or f"Step {idx}")
        detail_text = str(s.get("detail") or "")
        row.setAccessibleName(f"{step_name}: {step_status}")
        row.setAccessibleDescription(detail_text)
        icon_text, icon_color = _step_icon(step_status)

        icon_label = QLabel(icon_text)
        icon_label.setObjectName("meetingFinalizationStepIcon")
        icon_label.setFont(QFont("Segoe UI", 11, QFont.Weight.Bold))
        icon_label.setStyleSheet(f"color: {icon_color};")
        icon_label.setFixedWidth(16)
        row_layout.addWidget(icon_label)

        name_label = WrappedLabel(step_name, break_long_words=True)
        name_label.setObjectName("meetingFinalizationStepName")
        name_label.setTextFormat(Qt.TextFormat.PlainText)
        name_font = QFont("Segoe UI", 11)
        if step_status == "running":
            name_font.setWeight(QFont.Weight.DemiBold)
        name_label.setFont(name_font)
        row_layout.addWidget(name_label, 1)

        detail_label = QLabel(_step_detail_text(step_status))
        detail_label.setObjectName("meetingFinalizationStepDetail")
        detail_label.setFont(QFont("Segoe UI", 10))
        row_layout.addWidget(detail_label)

        action_label = _step_action_label(step_id, step_status, allow_actions)
        if action_label:
            self._add_step_action(row_layout, action_label, step_id, step_name)

        self.finalization_steps_layout.addWidget(row)

    def _add_step_action(
        self, row_layout: QHBoxLayout, action_label: str, step_id: str, step_name: str
    ) -> None:
        action = QPushButton(action_label)
        action.setObjectName("meetingFinalizationStepAction")
        action.setCursor(Qt.CursorShape.PointingHandCursor)
        action.setFlat(True)
        action.setAccessibleName(f"{action_label} {step_name}")
        action.clicked.connect(
            lambda _checked=False, sid=step_id: (
                self.retry_step_requested.emit(sid)
            )
        )
        if step_id == "speaker_id" and not self._speaker_rerun_allowed():
            action.setEnabled(False)
            action.setToolTip(self._speaker_rerun_tip())
        row_layout.addWidget(action)

    def _refresh_elapsed(self) -> None:
        """Update the elapsed label from the local pause-aware timer."""
        elapsed = self._elapsed_base_s
        if self._running_since is not None:
            elapsed += time.monotonic() - self._running_since
        total = int(max(0, elapsed))
        hours, remainder = divmod(total, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours:
            elapsed_text = f"{hours}:{minutes:02d}:{seconds:02d}"
        else:
            elapsed_text = f"{minutes:02d}:{seconds:02d}"
        self.elapsed_label.setText(elapsed_text)
        self.elapsed_label.setAccessibleName(
            f"Meeting elapsed time: {elapsed_text}"
        )

    @property
    def is_meeting_active(self) -> bool:
        """True while the tab shows the in-meeting layout."""
        return self._active

    @property
    def finalization_status(self) -> Optional[str]:
        """Current finalization status, or None when no result is shown."""
        if not self._finalization:
            return None
        return self._finalization.get("status")
