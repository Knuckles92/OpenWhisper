"""Capture shareable OpenWhisper screenshots, cards, and a short demo video."""
from __future__ import annotations

import math
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PyQt6.QtCore import Qt, QRect
from PyQt6.QtGui import QColor, QFont, QPainter, QPixmap
from PyQt6.QtWidgets import QApplication, QWidget

from config import bundle_root, config
from ui_qt.app import QtApplication
from ui_qt.dialogs.meeting_delete_dialog import MeetingDeleteDialog
from ui_qt.dialogs.model_manager_dialog import ModelManagerDialog
from ui_qt.dialogs.settings_dialog import (
    GENERAL,
    HOTKEYS,
    MEETING_AFTER,
    RECORDING,
    SettingsDialog,
)
from ui_qt.main_window import MainWindow
from ui_qt.overlays.waveform_overlay import WaveformOverlay
from ui_qt.utils.app_icon import render_app_pixmap
from ui_qt.utils.palette import current_palette
from ui_qt.widgets.tabbed_content import TabbedContentWidget

OUT = ROOT / "demo"
PICTURES = OUT / "pictures"
CARDS = PICTURES / "cards"
VIDEO_DIR = OUT / "video"
CARD_SIZE = (1920, 1080)

_READY_STATE = {
    "cloud_enabled": True,
    "finalization": {"status": "completed"},
}

DEMO_MEETINGS = [
    {
        "id": "m_demo_planning",
        "title": "Demo Planning Sync",
        "status": "ended",
        "started_at": "2026-09-10T14:02:00",
        "ended_at": "2026-09-10T14:44:00",
        "state_json": _READY_STATE,
        "content_summary": {
            "has_audio": True,
            "has_transcript": True,
            "is_empty": False,
            "preview_text": (
                "June works if we freeze the API by May fifteenth. "
                "Otherwise the mobile team slips."
            ),
        },
    },
    {
        "id": "m_demo_standup",
        "title": "Monday standup",
        "status": "ended",
        "started_at": "2026-09-08T09:15:00",
        "ended_at": "2026-09-08T09:28:00",
        "state_json": _READY_STATE,
        "content_summary": {
            "has_audio": True,
            "has_transcript": True,
            "is_empty": False,
            "preview_text": (
                "I'll take the mobile client spike after the RFC lands."
            ),
        },
    },
    {
        "id": "m_demo_design",
        "title": "Dashboard design review",
        "status": "ended",
        "started_at": "2026-09-04T16:00:00",
        "ended_at": "2026-09-04T16:38:00",
        "state_json": _READY_STATE,
        "content_summary": {
            "has_audio": True,
            "has_transcript": True,
            "is_empty": False,
            "preview_text": (
                "Keep audio for fourteen days unless the host exports."
            ),
        },
    },
]

FINALIZING = {
    "status": "running",
    "message": "Preparing the final report…",
    "current_step": 3,
    "total_steps": 4,
    "step_details": "Writing the summary, decisions, and action items…",
    "steps": [
        {"id": "redecode", "name": "Audio Re-transcription", "status": "completed"},
        {"id": "polish", "name": "Transcript Cleanup", "status": "completed"},
        {"id": "consolidation", "name": "Summary & Action Items", "status": "running"},
        {"id": "finalize", "name": "State Finalization", "status": "pending"},
    ],
}

COMPLETED = {
    "status": "completed",
    "message": (
        "Final insights ready — 24 segments, 4 key points, "
        "3 action items, 2 decisions."
    ),
    "stage": "complete",
    "current_step": 4,
    "total_steps": 4,
    "step_details": "All finalization passes completed successfully.",
    "steps": [
        {"id": "redecode", "name": "Audio Re-transcription", "status": "completed"},
        {"id": "polish", "name": "Transcript Cleanup", "status": "completed"},
        {
            "id": "consolidation",
            "name": "Summary & Action Items",
            "status": "completed",
        },
        {"id": "finalize", "name": "State Finalization", "status": "completed"},
    ],
    "summary_stats": {
        "duration_s": 252.0,
        "segments": 24,
        "words": 512,
        "key_points": 4,
        "action_items": 3,
        "decisions": 2,
    },
}

TRANSCRIPT = (
    "Welcome everyone. Thanks for making time for this planning sync.\n\n"
    "I think we should ship the beta in June. That's the date we floated last week."
)

VIDEO_SLIDES = [
    ("00-title.png", 3.6),
    ("01-dictate.png", 4.2),
    ("02-overlay.png", 4.0),
    ("03-meeting-start.png", 4.0),
    ("04-meeting-live.png", 4.0),
    ("05-meeting-ready.png", 4.2),
    ("06-past-meetings.png", 4.0),
    ("07-models.png", 4.0),
    ("08-settings.png", 4.0),
    ("99-try-it.png", 5.2),
]


def wait_ms(app: QApplication, ms: int) -> None:
    deadline = time.monotonic() + ms / 1000.0
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.016)


def save_widget(widget: QWidget, path: Path) -> Path:
    widget.ensurePolished()
    QApplication.processEvents()
    pixmap = widget.grab()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not pixmap.save(str(path), "PNG"):
        raise RuntimeError(f"Could not save {path}")
    return path


def paint_overlay(overlay: WaveformOverlay, path: Path, background: QColor) -> Path:
    overlay.ensurePolished()
    QApplication.processEvents()
    dpr = max(1.0, overlay.devicePixelRatioF())
    size = overlay.size()
    pixmap = QPixmap(max(1, int(size.width() * dpr)), max(1, int(size.height() * dpr)))
    pixmap.setDevicePixelRatio(dpr)
    pixmap.fill(background)
    overlay.render(pixmap)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not pixmap.save(str(path), "PNG"):
        raise RuntimeError(f"Could not save {path}")
    return path


def _fit_rect(src_w: int, src_h: int, box: QRect) -> QRect:
    if src_w <= 0 or src_h <= 0:
        return box
    scale = min(box.width() / src_w, box.height() / src_h)
    width = max(1, int(src_w * scale))
    height = max(1, int(src_h * scale))
    x = box.x() + (box.width() - width) // 2
    y = box.y() + (box.height() - height) // 2
    return QRect(x, y, width, height)


def make_card(
    *,
    path: Path,
    title: str,
    subtitle: str = "",
    screenshot: QPixmap | None = None,
    screenshots: list[QPixmap] | None = None,
    footer: str = "openwhisper.fiorilabs.tech",
    logo: QPixmap,
    palette,
) -> Path:
    width, height = CARD_SIZE
    canvas = QPixmap(width, height)
    canvas.fill(QColor(palette.css("bg")))
    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)

    painter.drawPixmap(56, 44, logo.scaled(56, 56, Qt.AspectRatioMode.KeepAspectRatio,
                                           Qt.TransformationMode.SmoothTransformation))
    painter.setPen(QColor(palette.css("text-heading")))
    painter.setFont(QFont("Segoe UI", 22, QFont.Weight.DemiBold))
    painter.drawText(128, 82, "OpenWhisper")

    painter.setPen(QColor(palette.css("text-heading")))
    painter.setFont(QFont("Segoe UI", 36, QFont.Weight.DemiBold))
    painter.drawText(QRect(56, 120, width - 112, 70), Qt.AlignmentFlag.AlignLeft, title)

    if subtitle:
        painter.setPen(QColor(palette.css("text-secondary")))
        painter.setFont(QFont("Segoe UI", 18))
        painter.drawText(QRect(56, 188, width - 112, 48), Qt.AlignmentFlag.AlignLeft, subtitle)

    shots = screenshots if screenshots is not None else ([screenshot] if screenshot else [])
    if shots:
        top = 260 if subtitle else 210
        box = QRect(56, top, width - 112, height - top - 88)
        if len(shots) == 1:
            dest = _fit_rect(shots[0].width(), shots[0].height(), box)
            painter.drawPixmap(dest, shots[0])
        else:
            gap = 28
            cell_w = (box.width() - gap * (len(shots) - 1)) // len(shots)
            for index, shot in enumerate(shots):
                cell = QRect(box.x() + index * (cell_w + gap), box.y(), cell_w, box.height())
                dest = _fit_rect(shot.width(), shot.height(), cell)
                painter.drawPixmap(dest, shot)

    painter.setPen(QColor(palette.css("text-muted")))
    painter.setFont(QFont("Segoe UI", 14))
    painter.drawText(QRect(56, height - 64, width - 112, 28), Qt.AlignmentFlag.AlignLeft, footer)
    painter.end()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not canvas.save(str(path), "PNG"):
        raise RuntimeError(f"Could not save {path}")
    return path


def make_title_card(path: Path, logo: QPixmap, palette) -> Path:
    width, height = CARD_SIZE
    canvas = QPixmap(width, height)
    canvas.fill(QColor(palette.css("bg")))
    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    mark = logo.scaled(128, 128, Qt.AspectRatioMode.KeepAspectRatio,
                       Qt.TransformationMode.SmoothTransformation)
    painter.drawPixmap((width - mark.width()) // 2, 250, mark)
    painter.setPen(QColor(palette.css("text-heading")))
    painter.setFont(QFont("Segoe UI", 56, QFont.Weight.DemiBold))
    painter.drawText(QRect(80, 420, width - 160, 80), Qt.AlignmentFlag.AlignHCenter, "OpenWhisper")
    painter.setPen(QColor(palette.css("text-secondary")))
    painter.setFont(QFont("Segoe UI", 22))
    painter.drawText(
        QRect(120, 520, width - 240, 90),
        Qt.AlignmentFlag.AlignHCenter | Qt.TextFlag.TextWordWrap,
        "Dictate, upload a file, or record a whole meeting — on your computer.",
    )
    painter.setPen(QColor(palette.css("accent")))
    painter.setFont(QFont("Segoe UI", 16, QFont.Weight.DemiBold))
    painter.drawText(
        QRect(80, 900, width - 160, 40),
        Qt.AlignmentFlag.AlignHCenter,
        "openwhisper.fiorilabs.tech",
    )
    painter.end()
    path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(str(path), "PNG")
    return path


def make_end_card(path: Path, logo: QPixmap, palette) -> Path:
    width, height = CARD_SIZE
    canvas = QPixmap(width, height)
    canvas.fill(QColor(palette.css("bg")))
    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    mark = logo.scaled(72, 72, Qt.AspectRatioMode.KeepAspectRatio,
                       Qt.TransformationMode.SmoothTransformation)
    painter.drawPixmap(80, 80, mark)
    painter.setPen(QColor(palette.css("text-heading")))
    painter.setFont(QFont("Segoe UI", 22, QFont.Weight.DemiBold))
    painter.drawText(172, 128, "OpenWhisper")
    painter.setFont(QFont("Segoe UI", 42, QFont.Weight.DemiBold))
    painter.drawText(QRect(80, 220, width - 160, 70), Qt.AlignmentFlag.AlignLeft, "Try it in two minutes")

    steps = [
        ("1", "Download OpenWhisper-Setup from openwhisper.fiorilabs.tech"),
        ("2", "Run the installer. If Windows warns you, click More info → Run anyway."),
        ("3", "Press * on the numpad to dictate, or open Meeting Mode for a call."),
    ]
    y = 340
    for number, text in steps:
        painter.setBrush(QColor(palette.css("accent")))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(80, y, 48, 48)
        painter.setPen(QColor(palette.css("on-accent")))
        painter.setFont(QFont("Segoe UI", 18, QFont.Weight.DemiBold))
        painter.drawText(QRect(80, y, 48, 48), Qt.AlignmentFlag.AlignCenter, number)
        painter.setPen(QColor(palette.css("text")))
        painter.setFont(QFont("Segoe UI", 22))
        painter.drawText(QRect(152, y, width - 240, 48), Qt.AlignmentFlag.AlignVCenter, text)
        y += 88

    painter.setPen(QColor(palette.css("text-secondary")))
    painter.setFont(QFont("Segoe UI", 18))
    painter.drawText(
        QRect(80, 680, width - 160, 80),
        Qt.AlignmentFlag.AlignLeft | Qt.TextFlag.TextWordWrap,
        "Audio stays on this PC. Cloud intelligence is optional, and only "
        "sends transcript text if you turn it on.",
    )
    painter.setPen(QColor(palette.css("accent")))
    painter.setFont(QFont("Segoe UI", 18, QFont.Weight.DemiBold))
    painter.drawText(80, 900, "openwhisper.fiorilabs.tech")
    painter.end()
    path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(str(path), "PNG")
    return path


def reset_meeting(tab) -> None:
    tab.set_developer_mode(False)
    tab.set_meeting_state(
        {
            "active": False,
            "paused": False,
            "status": "idle",
            "elapsed_s": 0,
            "finalization": None,
            "dashboard_available": False,
            "cloud_enabled": True,
            "background_available": False,
            "background_message": "",
            "meeting_id": None,
        }
    )


def show_window(window: MainWindow, width: int, height: int) -> None:
    if window._compact_mode:
        window.set_compact_mode(False, persist=False)
    window.setMinimumSize(config.MAIN_WINDOW_MIN_WIDTH, config.MAIN_WINDOW_MIN_HEIGHT)
    window.setMaximumWidth(config.MAIN_WINDOW_MAX_WIDTH)
    window.resize(width, height)
    window.show()
    window.raise_()
    QApplication.processEvents()


def collapse_sidebar(window: MainWindow) -> None:
    sidebar = window.history_sidebar
    sidebar.animation.stop()
    sidebar._is_expanded = False
    sidebar._set_sidebar_width(sidebar.COLLAPSED_WIDTH)
    sidebar.setMinimumWidth(sidebar.COLLAPSED_WIDTH)
    sidebar.setMaximumWidth(sidebar.COLLAPSED_WIDTH)
    window.history_edge_tab.set_expanded(False)


def expand_past_meetings(window: MainWindow) -> None:
    sidebar = window.history_sidebar
    sidebar.meetings_content_widget._meeting_provider = lambda: list(DEMO_MEETINGS)
    sidebar.animation.stop()
    sidebar._is_expanded = True
    sidebar._set_sidebar_width(sidebar.EXPANDED_WIDTH)
    sidebar.setMinimumWidth(sidebar.EXPANDED_WIDTH)
    sidebar.setMaximumWidth(sidebar.EXPANDED_WIDTH)
    window.history_edge_tab.set_expanded(True)
    sidebar.refresh()
    window.resize(
        min(config.MAIN_WINDOW_MAX_WIDTH, 720 + sidebar.EXPANDED_WIDTH),
        max(window.height(), 820),
    )


def find_ffmpeg() -> str | None:
    found = shutil.which("ffmpeg")
    if found:
        return found
    for candidate in (
        Path(r"C:\ffmpeg\bin\ffmpeg.exe"),
        Path(r"C:\Program Files\ffmpeg\bin\ffmpeg.exe"),
    ):
        if candidate.is_file():
            return str(candidate)
    return None


def write_concat_list(cards: Path, dest: Path) -> Path:
    lines = ["ffconcat version 1.0"]
    last = None
    for name, duration in VIDEO_SLIDES:
        path = (cards / name).resolve().as_posix()
        lines.append(f"file '{path}'")
        lines.append(f"duration {duration}")
        last = path
    if last:
        lines.append(f"file '{last}'")
    dest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return dest


def encode_video(cards: Path) -> Path:
    VIDEO_DIR.mkdir(parents=True, exist_ok=True)
    dest = VIDEO_DIR / "openwhisper-demo.mp4"
    concat = VIDEO_DIR / "slides.concat"
    write_concat_list(cards, concat)
    ffmpeg = find_ffmpeg()
    if ffmpeg:
        subprocess.run(
            [
                ffmpeg,
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat),
                "-vf",
                "fps=30,format=yuv420p",
                "-c:v",
                "libx264",
                "-crf",
                "18",
                "-movflags",
                "+faststart",
                str(dest),
            ],
            check=True,
        )
        return dest
    return encode_video_python(cards, dest)


def encode_video_python(cards: Path, dest: Path) -> Path:
    try:
        import imageio.v2 as imageio
    except ImportError as exc:
        raise RuntimeError(
            "ffmpeg is not on PATH and imageio is not installed"
        ) from exc
    frames = []
    for name, duration in VIDEO_SLIDES:
        image = imageio.imread(cards / name)
        frames.append((image, duration))
    writer = imageio.get_writer(
        dest,
        fps=30,
        codec="libx264",
        quality=8,
        pixelformat="yuv420p",
        macro_block_size=None,
    )
    try:
        for image, duration in frames:
            count = max(1, int(round(duration * 30)))
            for _ in range(count):
                writer.append_data(image)
    finally:
        writer.close()
    return dest


def capture() -> dict[str, Path]:
    PICTURES.mkdir(parents=True, exist_ok=True)
    CARDS.mkdir(parents=True, exist_ok=True)
    qt = QtApplication()
    app = qt.app
    palette = current_palette()
    logo = render_app_pixmap(128)
    saved: dict[str, Path] = {}

    with (
        patch("services.settings.settings_manager.save_setting"),
        patch("ui_qt.main_window.resolve_meeting_mode_intro_seen", return_value=True),
        patch(
            "ui_qt.dialogs.meeting_intro_dialog.maybe_show_meeting_mode_intro",
            return_value=False,
        ),
    ):
        window = MainWindow()
        window._force_quit = True
        window.meeting_mode_tab.set_developer_mode(False)
        if window._compact_mode:
            window.set_compact_mode(False, persist=False)
        collapse_sidebar(window)

        show_window(window, 680, 600)
        window.tabbed_content.set_current_index(TabbedContentWidget.TAB_QUICK_RECORD)
        wait_ms(app, 120)
        saved["quick-record"] = save_widget(window, PICTURES / "01-quick-record.png")

        window.quick_record_tab.set_transcription_collapsed(False)
        window.quick_record_tab.set_transcript(TRANSCRIPT)
        window.quick_record_tab.set_transcription_stats(2.4, 18.0, 320_000, 0.8)
        show_window(window, 720, 920)
        wait_ms(app, 220)
        saved["quick-record-transcript"] = save_widget(
            window, PICTURES / "02-quick-record-transcript.png"
        )

        window.quick_record_tab.clear_transcription()
        window.quick_record_tab.clear_transcription_stats()
        window.quick_record_tab.set_transcription_collapsed(True)
        window.tabbed_content.set_current_index(TabbedContentWidget.TAB_UPLOAD_FILE)
        show_window(window, 680, 620)
        wait_ms(app, 120)
        saved["upload-file"] = save_widget(window, PICTURES / "03-upload-file.png")

        window.tabbed_content.set_current_index(TabbedContentWidget.TAB_MEETING_MODE)
        reset_meeting(window.meeting_mode_tab)
        show_window(window, 720, 760)
        wait_ms(app, 180)
        saved["meeting-start"] = save_widget(window, PICTURES / "04-meeting-start.png")

        window.meeting_mode_tab.set_meeting_state(
            {
                "active": True,
                "paused": False,
                "status": "active",
                "elapsed_s": 142,
                "cloud_enabled": True,
                "dashboard_available": True,
                "meeting_id": "m_demo_planning",
            }
        )
        show_window(window, 720, 700)
        wait_ms(app, 180)
        saved["meeting-live"] = save_widget(window, PICTURES / "05-meeting-live.png")

        window.meeting_mode_tab.set_meeting_state(
            {"paused": True, "status": "paused", "active": True}
        )
        wait_ms(app, 120)
        saved["meeting-paused"] = save_widget(window, PICTURES / "06-meeting-paused.png")

        window.meeting_mode_tab.set_meeting_state(
            {
                "active": False,
                "paused": False,
                "status": "ended",
                "title": "Demo Planning Sync",
                "display_title": "Demo Planning Sync",
                "started_at": "2026-09-10T14:02:00",
                "ended_at": "2026-09-10T14:44:00",
                "insights_pill": "Working",
                "insights_tone": "running",
                "cloud_enabled": True,
                "dashboard_available": True,
                "background_available": True,
                "finalization": FINALIZING,
            }
        )
        show_window(window, 720, 900)
        wait_ms(app, 220)
        saved["meeting-finalizing"] = save_widget(
            window, PICTURES / "07-meeting-finalizing.png"
        )

        window.meeting_mode_tab.set_meeting_state(
            {
                "active": False,
                "status": "ended",
                "title": "Demo Planning Sync",
                "display_title": "Demo Planning Sync",
                "started_at": "2026-09-10T14:02:00",
                "ended_at": "2026-09-10T14:44:00",
                "insights_pill": "Ready",
                "insights_tone": "ready",
                "cloud_enabled": True,
                "dashboard_available": True,
                "background_available": False,
                "finalization": COMPLETED,
            }
        )
        show_window(window, 720, 920)
        wait_ms(app, 220)
        saved["meeting-ready"] = save_widget(window, PICTURES / "08-meeting-ready.png")

        reset_meeting(window.meeting_mode_tab)
        expand_past_meetings(window)
        wait_ms(app, 240)
        saved["past-meetings"] = save_widget(window, PICTURES / "09-past-meetings.png")

        collapse_sidebar(window)
        reset_meeting(window.meeting_mode_tab)
        window.tabbed_content.set_current_index(TabbedContentWidget.TAB_QUICK_RECORD)
        window.set_compact_mode(True, persist=False)
        wait_ms(app, 160)
        saved["compact"] = save_widget(window, PICTURES / "10-compact.png")
        window.set_compact_mode(False, persist=False)
        collapse_sidebar(window)

        overlay = WaveformOverlay()
        overlay.move(80, 80)
        overlay.show()
        levels = [0.25 + 0.7 * abs(math.sin(i * 0.45 + 0.4)) for i in range(20)]
        overlay.set_state(WaveformOverlay.STATE_RECORDING)
        overlay.update_audio_levels(levels)
        wait_ms(app, 700)
        bg = QColor(palette.css("bg"))
        saved["overlay-recording"] = paint_overlay(
            overlay, PICTURES / "11-overlay-recording.png", bg
        )

        overlay.set_state(WaveformOverlay.STATE_STREAMING)
        overlay.update_audio_levels(levels)
        overlay.update_streaming_text(
            "June works if we freeze the API by May fifteenth."
        )
        wait_ms(app, 500)
        saved["overlay-live"] = paint_overlay(
            overlay, PICTURES / "12-overlay-live-preview.png", bg
        )

        overlay.set_state(WaveformOverlay.STATE_COPIED)
        wait_ms(app, 350)
        saved["overlay-copied"] = paint_overlay(
            overlay, PICTURES / "13-overlay-copied.png", bg
        )
        overlay.hide()
        overlay.deleteLater()

        settings = SettingsDialog(window)
        settings.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        settings.resize(settings.DEFAULT_SIZE)
        settings.show()
        settings.select_destination(GENERAL)
        wait_ms(app, 200)
        saved["settings-general"] = save_widget(
            settings, PICTURES / "14-settings-general.png"
        )
        settings.select_destination(RECORDING)
        wait_ms(app, 160)
        saved["settings-recording"] = save_widget(
            settings, PICTURES / "15-settings-recording.png"
        )
        settings.select_destination(HOTKEYS)
        wait_ms(app, 160)
        saved["settings-hotkeys"] = save_widget(
            settings, PICTURES / "16-settings-hotkeys.png"
        )
        settings.select_destination(MEETING_AFTER)
        wait_ms(app, 160)
        saved["settings-after"] = save_widget(
            settings, PICTURES / "16b-settings-after-meeting.png"
        )
        settings.hide()
        settings.deleteLater()

        manager = ModelManagerDialog(background_cache_scan=False, parent=window)
        manager.show()
        wait_ms(app, 240)
        saved["model-manager"] = save_widget(
            manager, PICTURES / "17-model-manager.png"
        )
        manager.hide()
        manager.deleteLater()

        delete = MeetingDeleteDialog(window, has_audio=True)
        delete.resize(420, 240)
        delete.show()
        wait_ms(app, 120)
        saved["delete-meeting"] = save_widget(
            delete, PICTURES / "18-delete-meeting.png"
        )
        delete.hide()
        delete.deleteLater()

        window.hide()
        window.close()
        wait_ms(app, 80)

    saved["card-title"] = make_title_card(CARDS / "00-title.png", logo, palette)
    saved["card-dictate"] = make_card(
        path=CARDS / "01-dictate.png",
        title="Dictate from any app",
        subtitle="A hotkey starts recording. When you stop, the text pastes for you.",
        screenshot=QPixmap(str(saved["quick-record"])),
        logo=logo,
        palette=palette,
    )
    saved["card-overlay"] = make_card(
        path=CARDS / "02-overlay.png",
        title="A tiny overlay follows you",
        subtitle="See recording, live words, then Copied — without leaving your document.",
        screenshots=[
            QPixmap(str(saved["overlay-recording"])),
            QPixmap(str(saved["overlay-live"])),
            QPixmap(str(saved["overlay-copied"])),
        ],
        logo=logo,
        palette=palette,
    )
    saved["card-meeting-start"] = make_card(
        path=CARDS / "03-meeting-start.png",
        title="Record the whole call",
        subtitle="Meeting Mode captures your mic and the other side of the call.",
        screenshot=QPixmap(str(saved["meeting-start"])),
        logo=logo,
        palette=palette,
    )
    saved["card-meeting-live"] = make_card(
        path=CARDS / "04-meeting-live.png",
        title="Share a live dashboard",
        subtitle="Pause, open the browser dashboard, or copy a guest link.",
        screenshot=QPixmap(str(saved["meeting-live"])),
        logo=logo,
        palette=palette,
    )
    saved["card-meeting-ready"] = make_card(
        path=CARDS / "05-meeting-ready.png",
        title="It writes the recap when you hang up",
        subtitle="Cleanup, summary, decisions, and action items — then Done.",
        screenshot=QPixmap(str(saved["meeting-ready"])),
        logo=logo,
        palette=palette,
    )
    saved["card-past"] = make_card(
        path=CARDS / "06-past-meetings.png",
        title="Find any meeting later",
        subtitle="Search Past Meetings, copy the transcript, or delete a session.",
        screenshot=QPixmap(str(saved["past-meetings"])),
        logo=logo,
        palette=palette,
    )
    saved["card-models"] = make_card(
        path=CARDS / "07-models.png",
        title="Local voice, optional cloud text",
        subtitle="Pick the speech engine and the cleanup model in one place.",
        screenshot=QPixmap(str(saved["model-manager"])),
        logo=logo,
        palette=palette,
    )
    saved["card-settings"] = make_card(
        path=CARDS / "08-settings.png",
        title="Make it yours",
        subtitle="Theme, hotkeys, microphone, and what happens after a meeting.",
        screenshot=QPixmap(str(saved["settings-general"])),
        logo=logo,
        palette=palette,
    )
    saved["card-end"] = make_end_card(CARDS / "99-try-it.png", logo, palette)
    saved["video"] = encode_video(CARDS)
    return saved


if __name__ == "__main__":
    results = capture()
    for key, path in results.items():
        print(f"{key}: {path}")
    logging = __import__("logging")
    logging.shutdown()
    os._exit(0)
