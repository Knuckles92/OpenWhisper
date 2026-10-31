import logging
import sys
from typing import Optional, Callable
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QFrame, QPushButton
)
from PyQt6.QtCore import Qt, QObject, QTimer, pyqtSignal, QEvent, QPropertyAnimation, QPointF, QRect, QRectF
from PyQt6.QtGui import QAction, QKeySequence, QPainter, QPen

from config import config
from services.hotkey_manager import format_hotkey_display
from services.settings import (
    SETTING_DEFAULTS,
    SettingsKey,
    resolve_meeting_mode_intro_seen,
    setting_value,
    settings_manager,
)
from ui_qt.utils.collapse_animation import (
    SECTION_COLLAPSE_DURATION_MS,
    SECTION_COLLAPSE_EASING,
    UNLIMITED_HEIGHT,
)
from ui_qt.utils.desktop import compositor_managed, without_window_buttons
from ui_qt.utils.palette import token_color

logger = logging.getLogger(__name__)


class WindowGlyphButton(QPushButton):
    """A title-bar button that paints its glyph instead of drawing a character.

    Text glyphs (─ □ ❐ ✕) come from whatever font the platform falls back to,
    so they render at different sizes and weights, and □ can turn into "=".
    Painted lines look the same on every platform.
    """

    MINIMIZE = "minimize"
    MAXIMIZE = "maximize"
    RESTORE = "restore"
    CLOSE = "close"
    _SIZE = 10  # the glyph's box, in pixels

    def __init__(self, glyph: str, hover_token: str = "text-heading", parent=None):
        super().__init__(parent)
        self._glyph = glyph
        self._hover_token = hover_token
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)

    @property
    def glyph(self) -> str:
        return self._glyph

    def set_glyph(self, glyph: str) -> None:
        self._glyph = glyph
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)  # the stylesheet's hover background
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = token_color(self._hover_token if self.underMouse() else "text-secondary")
        pen = QPen(color, 1.0)
        pen.setCapStyle(Qt.PenCapStyle.FlatCap)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        size = self._SIZE
        # A whole-pixel origin plus half a pixel keeps 1px lines crisp.
        left = (self.width() - size) // 2 + 0.5
        top = (self.height() - size) // 2 + 0.5
        right, bottom = left + size - 1, top + size - 1
        if self._glyph == self.MINIMIZE:
            y = top + (size - 1) // 2
            painter.drawLine(QPointF(left, y), QPointF(right, y))
        elif self._glyph == self.MAXIMIZE:
            painter.drawRect(QRectF(left, top, size - 1, size - 1))
        elif self._glyph == self.RESTORE:
            # The front window, and the edges of the one behind that show
            # above and to its right.
            painter.drawRect(QRectF(left, top + 2, size - 3, size - 3))
            painter.drawLine(QPointF(left + 2, top), QPointF(right, top))
            painter.drawLine(QPointF(right, top), QPointF(right, bottom - 2))
        elif self._glyph == self.CLOSE:
            painter.drawLine(QPointF(left, top), QPointF(right, bottom))
            painter.drawLine(QPointF(right, top), QPointF(left, bottom))
        painter.end()


class CustomTitleBar(QFrame):
    # Height at the default font scale. The bar grows when a larger UI font
    # makes the menu items taller: QMenuBar hides every item that does not
    # fit inside its rect behind an overflow button.
    BASE_HEIGHT = 32

    _MENU_BAR_STYLE = """
        QMenuBar {
            background-color: transparent;
            color: @text-secondary;
            font-size: 12px;
            border: none;
            spacing: 0px;
        }
        QMenuBar::item {
            background-color: transparent;
            padding: 4px 10px;
        }
        QMenuBar::item:selected {
            background-color: @surface-hover;
            color: @text-heading;
        }
        QMenuBar::item:pressed {
            background-color: @surface-active;
        }
        QMenu::separator {
            height: 1px;
            background-color: @surface-hover;
            margin: 4px 8px;
        }
    """

    _TITLE_LABEL_STYLE = """
        QLabel {
            background-color: transparent;
            color: @text;
            font-size: 13px;
            font-weight: 600;
            font-family: 'Segoe UI', sans-serif;
        }
    """

    _WINDOW_BUTTON_STYLE = """
        QPushButton {
            background-color: transparent;
            border: none;
            color: @text-secondary;
            font-size: 14px;
            font-family: 'Segoe UI', sans-serif;
        }
        QPushButton:hover {
            background-color: @surface-hover;
            color: @text-heading;
        }
    """

    _CLOSE_BUTTON_STYLE = """
        QPushButton {
            background-color: transparent;
            border: none;
            color: @text-secondary;
            font-size: 14px;
            font-family: 'Segoe UI', sans-serif;
        }
        QPushButton:hover {
            background-color: @danger;
            color: @on-accent;
        }
    """

    _TITLE_BAR_STYLE = """
        #customTitleBar {
            background-color: @surface;
            border-bottom: 1px solid @border;
        }
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.parent_window = parent
        self._drag_position = None
        self._is_maximized = False
        self._normal_geometry = None
        self.setFixedHeight(self.BASE_HEIGHT)
        self.setObjectName("customTitleBar")
        self.setAutoFillBackground(True)
        # Before any child exists. Qt restyles a widget's children before it
        # sends the widget its own StyleChange, which is when WidgetStyleFilter
        # substitutes the @tokens, so children styled first parsed the raw
        # "@surface" sheet and logged "Could not parse stylesheet of object
        # CustomTitleBar" on every launch.
        self.setStyleSheet(self._TITLE_BAR_STYLE)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 0, 0, 0)
        layout.setSpacing(0)

        self._build_menu_bar(layout)
        layout.addStretch()
        self._build_title_label(layout)
        layout.addStretch()
        self._build_window_buttons(layout)

        self._sync_height()

    def _build_menu_bar(self, layout: QHBoxLayout) -> None:
        from PyQt6.QtWidgets import QMenuBar
        self.menu_bar = QMenuBar()
        self.menu_bar.setStyleSheet(self._MENU_BAR_STYLE)
        layout.addWidget(self.menu_bar, 0, Qt.AlignmentFlag.AlignVCenter)

    def _build_title_label(self, layout: QHBoxLayout) -> None:
        self.title_label = QLabel("OpenWhisper")
        self.title_label.setStyleSheet(self._TITLE_LABEL_STYLE)
        layout.addWidget(self.title_label)

    def _build_window_buttons(self, layout: QHBoxLayout) -> None:
        self.minimize_btn = WindowGlyphButton(WindowGlyphButton.MINIMIZE)
        self.minimize_btn.setFixedSize(46, self.BASE_HEIGHT)
        self.minimize_btn.setStyleSheet(self._WINDOW_BUTTON_STYLE)
        self.minimize_btn.setToolTip("Minimize")
        self.minimize_btn.setAccessibleName("Minimize window")
        self.minimize_btn.clicked.connect(self._minimize)

        self.maximize_btn = WindowGlyphButton(WindowGlyphButton.MAXIMIZE)
        self.maximize_btn.setFixedSize(46, self.BASE_HEIGHT)
        self.maximize_btn.setStyleSheet(self._WINDOW_BUTTON_STYLE)
        self.maximize_btn.setToolTip("Maximize")
        self.maximize_btn.setAccessibleName("Maximize window")
        self.maximize_btn.clicked.connect(self._toggle_maximize)

        self.close_btn = WindowGlyphButton(WindowGlyphButton.CLOSE, hover_token="on-accent")
        self.close_btn.setFixedSize(46, self.BASE_HEIGHT)
        self.close_btn.setStyleSheet(self._CLOSE_BUTTON_STYLE)
        self.close_btn.setToolTip("Close")
        self.close_btn.setAccessibleName("Close window")
        self.close_btn.clicked.connect(self._close)

        layout.addWidget(self.minimize_btn)
        layout.addWidget(self.maximize_btn)
        layout.addWidget(self.close_btn)

    def add_notice(self, widget: QWidget) -> None:
        """Place ``widget`` just before the window buttons."""
        layout = self.layout()
        layout.insertWidget(
            layout.indexOf(self.minimize_btn), widget, 0, Qt.AlignmentFlag.AlignVCenter
        )

    def _sync_height(self) -> None:
        border = self.height() - self.contentsRect().height()
        height = max(self.BASE_HEIGHT, self.menu_bar.sizeHint().height() + border)
        if height == self.height():
            return
        self.setFixedHeight(height)
        for button in (self.minimize_btn, self.maximize_btn, self.close_btn):
            button.setFixedSize(button.width(), height)

    def event(self, event):
        # A font-scale change re-styles the menu bar, which invalidates this
        # layout; that is the moment the menu's size hint changes.
        if event.type() == QEvent.Type.LayoutRequest:
            self._sync_height()
        return super().event(event)

    def _minimize(self):
        if self.parent_window:
            self.parent_window.showMinimized()

    def _toggle_maximize(self):
        if self.parent_window:
            if getattr(self.parent_window, "_compact_mode", False):
                return
            if self._is_maximized:
                if self._normal_geometry:
                    self.parent_window.setGeometry(self._normal_geometry)
                self.maximize_btn.set_glyph(WindowGlyphButton.MAXIMIZE)
                self.maximize_btn.setToolTip("Maximize")
                self.maximize_btn.setAccessibleName("Maximize window")
            else:
                self._normal_geometry = self.parent_window.geometry()
                self.parent_window.showMaximized()
                self.maximize_btn.set_glyph(WindowGlyphButton.RESTORE)
                self.maximize_btn.setToolTip("Restore")
                self.maximize_btn.setAccessibleName("Restore window")
            self._is_maximized = not self._is_maximized

    def _close(self):
        if self.parent_window:
            self.parent_window.close()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.parent_window:
            global_pos = event.globalPosition().toPoint()
            local_pos = self.parent_window.mapFromGlobal(global_pos)
            edge = self.parent_window._get_resize_edge(local_pos)
            if edge != (0, 0):
                self.parent_window._begin_resize(edge, global_pos)
                event.accept()
                return
            self._drag_position = global_pos - self.parent_window.frameGeometry().topLeft()
            event.accept()
            return

        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.parent_window and self.parent_window._resizing:
            self.parent_window._apply_resize_delta(event.globalPosition().toPoint())
            event.accept()
            return
        if event.buttons() == Qt.MouseButton.LeftButton and self._drag_position and self.parent_window:
            self.parent_window.move(event.globalPosition().toPoint() - self._drag_position)
            event.accept()
            return

        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.parent_window:
            self._drag_position = None
            if self.parent_window._resizing:
                self.parent_window._finish_resize()
                event.accept()
                return

        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._toggle_maximize()


from ui_qt.widgets import (
    Button,
    HistorySidebar, HistoryEdgeTab, HotkeyHintFilter,
    TabbedContentWidget, QuickRecordTab,
    CompactRecordController,
)
from ui_qt.widgets.update_notice import UpdateNoticeButton
from services.history_manager import history_manager
from ui_qt.dialogs.history_entry_dialog import HistoryEntryDialog


class _PageLayoutWatcher(QObject):
    """Call back when a tab page's content may have changed size.

    Installed on each page's scroll content. Any row shown, hidden or
    rewrapped below it re-lays that widget out, which posts it a
    LayoutRequest, so no page has to announce its own changes.
    """

    def __init__(self, changed: Callable[[], None], parent: QObject):
        super().__init__(parent)
        self._changed = changed

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.LayoutRequest:
            self._changed()
        return False


class MainWindow(QMainWindow):
    # Window-local keyboard shortcuts. Distinct from the global hotkeys in
    # config.DEFAULT_HOTKEYS, which work even when the app is unfocused.
    HISTORY_SHORTCUT = "Ctrl+H"
    HISTORY_CALENDAR_SHORTCUT = "Ctrl+Shift+D"
    COMPACT_SHORTCUT = "Ctrl+Shift+C"
    HOST_SHORTCUT = "Ctrl+Shift+H"
    QUIT_SHORTCUT = "Ctrl+Q"

    record_toggled = pyqtSignal(bool)
    record_canceled = pyqtSignal()
    walkthrough_requested = pyqtSignal()
    #: Open the walkthrough library at a capture folder, or None for the newest.
    walkthrough_library_requested = pyqtSignal(object)
    model_changed = pyqtSignal(str)
    whisper_engine_changed = pyqtSignal()  # Local engine (model/device/quant) changed
    # The Remote backend's Model field chose one of the paired computer's
    # models: (family, model).
    remote_model_selected = pyqtSignal(str, str)
    remote_runtime_selected = pyqtSignal(str, str, dict)
    # The Remote engine's link was clicked while the host was out of reach.
    remote_retry_requested = pyqtSignal()
    live_preview_changed = pyqtSignal()  # Live preview toggled from a tab footer
    settings_requested = pyqtSignal()
    guided_setup_requested = pyqtSignal()
    #: A Settings destination key (or legacy alias) to open.
    settings_destination_requested = pyqtSignal(str)
    engine_help_requested = pyqtSignal(str)
    hotkeys_requested = pyqtSignal()
    about_requested = pyqtSignal()
    check_for_updates_requested = pyqtSignal()
    #: The title bar's update notice was clicked.
    update_notice_requested = pyqtSignal()
    retranscribe_requested = pyqtSignal(str)  # audio_path
    upload_file_requested = pyqtSignal(str, float)
    upload_files_requested = pyqtSignal(object)  # BatchUploadRequest
    upload_cancel_requested = pyqtSignal()
    upload_copy_requested = pyqtSignal(str)
    quick_record_copy_requested = pyqtSignal(str)
    meeting_dashboard_requested = pyqtSignal()
    scratchpad_requested = pyqtSignal()
    stats_requested = pyqtSignal()
    # Host Mode's engine picker chose one of this computer's ready models:
    # (family, model).
    host_model_selected = pyqtSignal(str, str)
    past_meeting_requested = pyqtSignal(str)
    past_meeting_copy_requested = pyqtSignal(str)
    past_meeting_delete_requested = pyqtSignal(str, bool)
    past_meetings_clear_requested = pyqtSignal(bool)
    screen_created = pyqtSignal(str, object)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("OpenWhisper")
        self._compositor_managed = compositor_managed()

        # Keep the explicit Window type flag: setWindowFlags() replaces *all*
        # flags, and a bare FramelessWindowHint drops the top-level Window type.
        # On macOS that produces an NSWindow that fails to order back to the
        # front after hide() (i.e. can't be restored from the tray); on Windows
        # it happens to work either way. Including Window is safe on both.
        self.setWindowFlags(
            without_window_buttons(Qt.WindowType.Window) if self._compositor_managed else
            Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint
        )
        self.setMinimumSize(
            config.MAIN_WINDOW_MIN_WIDTH,
            config.MAIN_WINDOW_MIN_HEIGHT,
        )
        self.setMaximumWidth(config.MAIN_WINDOW_MAX_WIDTH)
        self.resize(
            config.MAIN_WINDOW_DEFAULT_WIDTH,
            config.MAIN_WINDOW_DEFAULT_HEIGHT,
        )
        if self._compositor_managed:
            self.setMinimumSize(360, 240)
            self.setMaximumSize(UNLIMITED_HEIGHT, UNLIMITED_HEIGHT)

        self.is_recording = False
        # The recording in progress is a walkthrough, which its own tab finishes.
        self.walkthrough_active = False
        self.current_model = config.MODEL_CHOICES[0]
        self._force_quit = False
        # Updated by UIController after QSystemTrayIcon probes the desktop.
        # Defaulting to True preserves construction behavior in lightweight
        # tests that instantiate MainWindow without a tray manager.
        self._tray_available = True
        self._initial_show_complete = False
        self._screen_widgets = {}
        self._screen_updates = {"upload": {}, "walkthrough": {}, "meeting": {}, "host": {}}
        self._shared_tab_updates = {}
        self._host_service = None
        self._compact_mode = False
        self._full_geometry = None
        self._host_mode = False
        # The recording view's geometry while Host Mode is shown.
        self._pre_host_geometry = None

        self._sidebar_width = config.MAIN_WINDOW_HISTORY_SIDEBAR_WIDTH
        self._geometry_format = "collapsed_content_v1"

        self._current_tab_index = TabbedContentWidget.TAB_QUICK_RECORD
        # Every tab sizes the window to its page (see _sync_page_size),
        # except an open transcript, which keeps the height the user gives it.
        self._transcript_height = config.MAIN_WINDOW_TRANSCRIPTION_EXPAND_HEIGHT
        # The page height the window last fitted, so it refits only on change.
        self._page_fit = None
        # The content width the page last asked for, so it rewidens only on change.
        self._page_width_need = None
        # The window's width, without the side panel, from before a page
        # widened it; a page that fits in it gets it back.
        self._own_width = None
        # How short the window may go on this page: its fit, capped so a long
        # page can still be shrunk to scroll.
        self._page_floor = config.MAIN_WINDOW_MIN_HEIGHT
        self._page_fit_timer = QTimer(self)
        self._page_fit_timer.setSingleShot(True)
        self._page_fit_timer.timeout.connect(self._sync_page_size)
        self._page_watcher = _PageLayoutWatcher(self._schedule_page_fit, self)
        # The page whose scrollbar a window animation is holding off.
        self._unscrolled_page = None
        self._meeting_intro_scheduled = False

        self._resize_margin = 8
        self._resizing = False
        self._resize_edge = None
        self._resize_start_pos = None
        self._resize_start_geometry = None

        self._geometry_save_timer = None
        self._tab_history_refresh_timer = QTimer(self)
        self._tab_history_refresh_timer.setSingleShot(True)
        self._tab_history_refresh_timer.timeout.connect(
            self._refresh_history_sidebar_if_expanded
        )
        #: A result's stats card, to scroll into view once the window has
        #: finished growing for it.
        self._stats_to_reveal = None
        self._stats_reveal_timer = QTimer(self)
        self._stats_reveal_timer.setSingleShot(True)
        self._stats_reveal_timer.setInterval(SECTION_COLLAPSE_DURATION_MS + 50)
        self._stats_reveal_timer.timeout.connect(self._reveal_shown_stats)

        self.on_show_copied_animation: Optional[Callable] = None
        #: Asked before a quit the user requested; False keeps the app open.
        self.confirm_quit: Optional[Callable[[], bool]] = None
        #: The History calendar window, made on first open and kept after.
        self._history_calendar = None

        self._setup_ui()
        self._setup_menu()
        self._load_saved_settings()
        self._restore_window_geometry()
        self._restore_compact_mode()
        self._restore_host_mode()

        self.setMouseTracking(True)
        from PyQt6.QtWidgets import QApplication
        if not self._compositor_managed:
            QApplication.instance().installEventFilter(self)

    def _setup_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)

        central_widget.setStyleSheet("""
            QWidget#centralWidget {
                border: 1px solid @border;
            }
        """)
        central_widget.setObjectName("centralWidget")
        central_widget.setMouseTracking(True)

        outer_layout = QVBoxLayout(central_widget)
        outer_layout.setContentsMargins(1, 1, 1, 1)  # 1px margin for border visibility
        outer_layout.setSpacing(0)

        if self._compositor_managed:
            from ui_qt.widgets.desktop_header import DesktopHeader

            self.title_bar = DesktopHeader(self)
        else:
            self.title_bar = CustomTitleBar(self)
        outer_layout.addWidget(self.title_bar)
        self.update_notice = UpdateNoticeButton()
        self.update_notice.clicked.connect(self.update_notice_requested.emit)
        self.title_bar.add_notice(self.update_notice)

        content_wrapper = QWidget()
        root_layout = QHBoxLayout(content_wrapper)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)
        if self._compositor_managed:
            from PyQt6.QtWidgets import QScrollArea

            # The viewport follows the configure size even when a tile is
            # smaller than the contents' minimum size. Nothing paints outside
            # the allocated surface, and every control remains reachable.
            self.desktop_scroll = QScrollArea()
            self.desktop_scroll.setFrameShape(QFrame.Shape.NoFrame)
            self.desktop_scroll.setWidgetResizable(True)
            self.desktop_scroll.setWidget(content_wrapper)
            outer_layout.addWidget(self.desktop_scroll, stretch=1)
        else:
            outer_layout.addWidget(content_wrapper, stretch=1)

        main_area = QWidget()
        main_area_layout = QVBoxLayout(main_area)
        main_area_layout.setContentsMargins(0, 0, 0, 0)
        main_area_layout.setSpacing(0)

        self.tabbed_content = TabbedContentWidget()
        self.quick_record_tab = QuickRecordTab()

        self.tabbed_content.add_tab(self.quick_record_tab, "Quick Record")

        self._screen_placeholders = {}
        for key, label in (
            ("upload", "Upload File"),
            ("walkthrough", "Walkthrough"),
            ("meeting", "Meeting Mode"),
        ):
            placeholder = QWidget()
            self._screen_placeholders[key] = placeholder
            self.tabbed_content.add_tab(placeholder, label)

        # Sync the stack with the tab bar after all tabs have been added
        # (fixes timing issue where tab bar index is restored before stack has widgets)
        self.tabbed_content.sync_stack_with_tab_bar()

        self.compact_controller = CompactRecordController()
        self.compact_controller.hide()
        self.compact_controller.record_requested.connect(
            self.quick_record_tab.record_button.click
        )
        self.compact_controller.stop_requested.connect(
            self.quick_record_tab.stop_button.click
        )
        self.compact_controller.cancel_requested.connect(
            self.quick_record_tab.cancel_button.click
        )

        self.tabbed_content.tab_changed.connect(self._on_tab_changed)

        self._connect_transcription_tab(self.quick_record_tab)
        self.quick_record_tab.scroll_area.widget().installEventFilter(self._page_watcher)

        self.quick_record_tab.record_toggled.connect(self._on_quick_record_toggled)
        self.quick_record_tab.record_canceled.connect(self._on_quick_record_canceled)
        self.quick_record_tab.copy_requested.connect(self.quick_record_copy_requested)

        main_area_layout.addWidget(self.tabbed_content)
        main_area_layout.addWidget(self.compact_controller)

        # View → Host Mode shows this instead of the tabs (see set_host_mode).
        self._main_area_layout = main_area_layout

        root_layout.addWidget(main_area, stretch=1)

        self.history_edge_tab = HistoryEdgeTab()
        self.history_edge_tab.set_shortcut_hint(self.HISTORY_SHORTCUT)
        self.history_edge_tab.clicked.connect(self.toggle_history)
        root_layout.addWidget(self.history_edge_tab)

        self.history_sidebar = HistorySidebar()
        self.history_sidebar.entry_selected.connect(self._on_history_entry_selected)
        self.history_sidebar.entry_copied.connect(self._on_history_entry_copied)
        self.history_sidebar.entry_deleted.connect(self._on_history_entry_deleted)
        self.history_sidebar.retranscribe_requested.connect(self._on_retranscribe_requested)
        self.history_sidebar.past_meeting_selected.connect(
            self.past_meeting_requested.emit
        )
        self.history_sidebar.past_meeting_copy_requested.connect(
            self.past_meeting_copy_requested.emit
        )
        self.history_sidebar.past_meeting_delete_requested.connect(
            self.past_meeting_delete_requested.emit
        )
        self.history_sidebar.past_meetings_clear_requested.connect(
            self.past_meetings_clear_requested.emit
        )
        self.history_sidebar.calendar_requested.connect(self.open_history_calendar)
        self.history_sidebar.walkthrough_selected.connect(self._on_walkthrough_picked)
        self.history_sidebar.width_animated.connect(self._on_sidebar_width_animated)
        self.history_sidebar.animation.finished.connect(
            self._on_sidebar_animation_finished
        )
        root_layout.addWidget(self.history_sidebar)

        # Sync the sidebar with the restored tab (must be after history_sidebar is created)
        self._on_tab_changed(self.tabbed_content.current_index())

        self._build_footer(outer_layout)

    def _connect_transcription_tab(self, tab):
        tab.model_changed.connect(self._on_model_changed)
        tab.engine_settings_changed.connect(self._on_engine_settings_changed)
        tab.remote_model_selected.connect(self.remote_model_selected)
        tab.remote_runtime_selected.connect(self.remote_runtime_selected)
        tab.remote_retry_requested.connect(self.remote_retry_requested)
        tab.live_preview_changed.connect(self._on_live_preview_changed)
        tab.help_requested.connect(self.engine_help_requested)
        tab.engine_downloads_requested.connect(
            lambda: self.settings_destination_requested.emit("engine_downloads")
        )
        tab.transcription_collapsed.connect(self._on_transcription_collapsed)
        tab.stats_widget.visibility_changed.connect(self._on_stats_visibility_changed)

    def existing_transcription_tabs(self):
        if not isinstance(getattr(self, "_screen_widgets", None), dict):
            return getattr(self, "transcription_tabs", ())
        upload = self._screen_widgets.get("upload")
        return (self.quick_record_tab, upload) if upload is not None else (self.quick_record_tab,)

    @property
    def transcription_tabs(self):
        """Explicit callers retain access to both recording surfaces."""
        return self.quick_record_tab, self.upload_file_tab

    @property
    def upload_file_tab(self):
        return self._ensure_screen("upload")

    @property
    def walkthrough_tab(self):
        return self._ensure_screen("walkthrough")

    @property
    def meeting_mode_tab(self):
        return self._ensure_screen("meeting")

    @property
    def host_dashboard(self):
        return self._ensure_screen("host")

    def _ensure_screen(self, key):
        if key in self._screen_widgets:
            return self._screen_widgets[key]
        if key == "upload":
            from ui_qt.widgets.upload_file_tab import UploadFileTab

            widget = UploadFileTab()
        elif key == "walkthrough":
            from ui_qt.widgets.walkthrough_tab import WalkthroughTab

            widget = WalkthroughTab()
        elif key == "meeting":
            from ui_qt.widgets.meeting_mode_tab import MeetingModeTab

            widget = MeetingModeTab()
        else:
            from ui_qt.widgets.host_dashboard import HostDashboard

            widget = HostDashboard()
        self._screen_widgets[key] = widget
        if key == "host":
            widget.hide()
            widget.model_requested.connect(self.host_model_selected)
            widget.settings_requested.connect(self.settings_destination_requested)
            self._main_area_layout.addWidget(widget)
            if self._host_service is not None:
                widget.bind(self._host_service)
        else:
            stack = self.tabbed_content.stack
            placeholder = self._screen_placeholders.pop(key)
            index = stack.indexOf(placeholder)
            current = stack.currentIndex()
            stack.removeWidget(placeholder)
            stack.insertWidget(index, widget)
            stack.setCurrentIndex(current)
            placeholder.deleteLater()
            widget.scroll_area.widget().installEventFilter(self._page_watcher)
            if key == "upload":
                self._connect_transcription_tab(widget)
                widget.upload_requested.connect(self._on_upload_file_transcribe)
                widget.upload_files_requested.connect(self.upload_files_requested.emit)
                widget.cancel_requested.connect(self.upload_cancel_requested.emit)
                widget.copy_requested.connect(self.upload_copy_requested.emit)
                widget.set_model_selection(settings_manager.load_model_selection())
                self._apply_local_engine_visibility(self.current_model)
                for method, (args, kwargs) in self._shared_tab_updates.items():
                    getattr(widget, method)(*args, **kwargs)
            elif key == "walkthrough":
                widget.walkthrough_requested.connect(self.walkthrough_requested)
                widget.cancel_requested.connect(self._on_quick_record_canceled)
                widget.settings_requested.connect(self.settings_destination_requested)
                widget.walkthroughs_changed.connect(self.refresh_history)
        for method, (args, kwargs) in self._screen_updates[key].items():
            if isinstance(method, tuple):
                method = method[0]
            getattr(widget, method)(*args, **kwargs)
        self.screen_created.emit(key, widget)
        return widget

    def update_screen(self, key, method, *args, **kwargs):
        """Retain the latest runtime values until an inactive screen opens."""
        if key == "meeting" and method == "set_meeting_state":
            previous = self._screen_updates[key].get(method, (({},), {}))[0][0]
            args = ({**previous, **args[0]},)
        cache_key = (method, args[0]) if method == "set_model_downloading" else method
        self._screen_updates[key].pop(cache_key, None)
        self._screen_updates[key][cache_key] = (args, kwargs)
        widget = self._screen_widgets.get(key)
        if widget is not None:
            getattr(widget, method)(*args, **kwargs)

    def update_transcription_tabs(self, method, *args, **kwargs):
        self._shared_tab_updates.pop(method, None)
        self._shared_tab_updates[method] = (args, kwargs)
        for tab in MainWindow.existing_transcription_tabs(self):
            getattr(tab, method)(*args, **kwargs)

    def bind_host_service(self, service):
        self._host_service = service
        host = self._screen_widgets.get("host")
        if host is not None:
            host.bind(service)

    def meeting_is_active(self):
        meeting = self._screen_widgets.get("meeting")
        if meeting is not None:
            return meeting.is_meeting_active
        state = self._screen_updates["meeting"].get("set_meeting_state", (({},), {}))[0][0]
        return bool(state.get("active"))

    _FOOTER_BAR_STYLE = """
        QWidget#footerBar {
            background-color: @bg;
            border-top: 1px solid @border-subtle;
        }
    """

    # Minimize, Stats and Settings share one neutral look; only Quit stands out.
    _FOOTER_BUTTON_STYLE = """
        QPushButton#trayButton,
        QPushButton#statsButton,
        QPushButton#settingsButton {
            background-color: @surface;
            color: @text-body;
            border: 1px solid @border;
            border-radius: 8px;
            padding: 6px 18px;
            font-weight: 600;
            font-size: 13px;
        }
        QPushButton#trayButton:hover,
        QPushButton#statsButton:hover,
        QPushButton#settingsButton:hover {
            background-color: @accent;
            color: @on-accent;
            border: 1px solid @accent;
        }
        QPushButton#trayButton:pressed,
        QPushButton#statsButton:pressed,
        QPushButton#settingsButton:pressed {
            background-color: @accent-pressed;
            color: @on-accent;
        }
    """

    _QUIT_BUTTON_STYLE = """
        QPushButton#quitButton {
            background-color: @surface;
            color: @danger-text;
            border: 1px solid @border;
            border-radius: 8px;
            padding: 6px 18px;
            font-weight: 600;
            font-size: 13px;
        }
        QPushButton#quitButton:hover {
            background-color: @danger;
            color: @on-accent;
            border: 1px solid @danger;
        }
        QPushButton#quitButton:pressed {
            background-color: @danger-pressed;
            color: @on-accent;
        }
    """

    def _build_footer(self, outer_layout: QVBoxLayout) -> None:
        self.footer = QWidget()
        self.footer.setObjectName("footerBar")
        self.footer.setFixedHeight(48)
        self.footer.setStyleSheet(self._FOOTER_BAR_STYLE)

        footer_layout = QHBoxLayout(self.footer)
        footer_layout.setContentsMargins(16, 7, 16, 7)
        footer_layout.setSpacing(0)
        footer_layout.addStretch()

        self.tray_button = Button("Hide" if self._compositor_managed else "Minimize to Tray")
        self.tray_button.setObjectName("trayButton")
        self.tray_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.tray_button.setFixedHeight(34)
        self.tray_button.setMinimumWidth(60 if self._compositor_managed else 140)
        self.tray_button.setToolTip("Hide OpenWhisper in the system tray")
        self.tray_button.setStyleSheet(self._FOOTER_BUTTON_STYLE)
        self.tray_button.set_hotkey(
            format_hotkey_display(config.DEFAULT_HOTKEYS["minimize_tray"])
        )
        self.tray_button.clicked.connect(self.minimize_to_tray)
        footer_layout.addWidget(self.tray_button)

        footer_layout.addSpacing(10)

        self.stats_button = QPushButton("Stats")
        self.stats_button.setObjectName("statsButton")
        self.stats_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.stats_button.setFixedHeight(34)
        self.stats_button.setMinimumWidth(80)
        self.stats_button.setStyleSheet(self._FOOTER_BUTTON_STYLE)
        self.stats_button.setToolTip("Your words, pace, streak and favorite apps")
        self.stats_button.clicked.connect(self.stats_requested.emit)
        footer_layout.addWidget(self.stats_button)

        footer_layout.addSpacing(10)

        self.settings_button = QPushButton("Settings")
        self.settings_button.setObjectName("settingsButton")
        self.settings_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.settings_button.setFixedHeight(34)
        self.settings_button.setMinimumWidth(100)
        self.settings_button.setStyleSheet(self._FOOTER_BUTTON_STYLE)
        self.settings_button.setToolTip(
            "Settings, models, and downloads (starts on the Overview)"
        )
        self.settings_button.clicked.connect(self.open_settings)
        footer_layout.addWidget(self.settings_button)

        footer_layout.addSpacing(10)

        self.quit_button = QPushButton("Quit")
        self.quit_button.setObjectName("quitButton")
        self.quit_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.quit_button.setFixedHeight(34)
        self.quit_button.setMinimumWidth(100)
        self.quit_button.setStyleSheet(self._QUIT_BUTTON_STYLE)
        HotkeyHintFilter(self.quit_button, self.QUIT_SHORTCUT)
        self.quit_button.clicked.connect(self.request_quit)
        footer_layout.addWidget(self.quit_button)

        footer_layout.addStretch()

        outer_layout.addWidget(self.footer)

    def _setup_menu(self):
        self.menuBar().hide()

        menubar = self.title_bar.menu_bar

        file_menu = menubar.addMenu("File")
        # Qt auto-assigns PreferencesRole to actions named "Settings", which
        # rewrites the label to "Preferences" on Windows. Keep our wording.
        settings_action = file_menu.addAction("Settings", self.open_settings)
        settings_action.setMenuRole(QAction.MenuRole.NoRole)
        setup_action = file_menu.addAction("Guided setup…", self.guided_setup_requested.emit)
        setup_action.setMenuRole(QAction.MenuRole.NoRole)
        downloads_action = file_menu.addAction("Downloads...", self.open_downloads)
        downloads_action.setMenuRole(QAction.MenuRole.NoRole)
        file_menu.addAction("Hotkeys", self.open_hotkey_settings)
        file_menu.addSeparator()
        self.minimize_to_tray_action = file_menu.addAction(
            "Minimize to Tray", self.minimize_to_tray
        )
        quit_action = file_menu.addAction(
            "Quit" if sys.platform == "darwin" else "Exit", self.request_quit
        )
        quit_action.setMenuRole(QAction.MenuRole.NoRole)
        quit_action.setShortcut(QKeySequence(self.QUIT_SHORTCUT))

        view_menu = menubar.addMenu("View")
        sidebar_name = (
            "Past Meetings"
            if self.tabbed_content.current_index() == TabbedContentWidget.TAB_MEETING_MODE
            else "History"
        )
        self.sidebar_action = view_menu.addAction(sidebar_name, self.toggle_history)
        self.sidebar_action.setShortcut(QKeySequence(self.HISTORY_SHORTCUT))
        calendar_action = view_menu.addAction("History Calendar", self.open_history_calendar)
        calendar_action.setShortcut(QKeySequence(self.HISTORY_CALENDAR_SHORTCUT))
        self.compact_action = view_menu.addAction(
            "Compact Mode", self.toggle_compact_mode
        )
        self.compact_action.setShortcut(QKeySequence(self.COMPACT_SHORTCUT))
        self.host_mode_action = view_menu.addAction("Host Mode", self.toggle_host_mode)
        self.host_mode_action.setCheckable(True)
        self.host_mode_action.setShortcut(QKeySequence(self.HOST_SHORTCUT))
        self.host_mode_action.setStatusTip(
            "Show what this computer serves to paired computers instead of "
            "the recording tabs"
        )
        view_menu.addSeparator()
        self.scratchpad_action = view_menu.addAction(
            "Scratchpad", self.scratchpad_requested.emit
        )
        self.stats_action = view_menu.addAction("Stats", self.stats_requested.emit)
        view_menu.addSeparator()
        view_menu.addAction(
            "Open Meeting Dashboard", self.meeting_dashboard_requested.emit
        )

        help_menu = menubar.addMenu("Help")
        updates_action = QAction("Check for Updates...", self)
        # Set the role before insertion so macOS merges it into the app menu.
        updates_action.setMenuRole(
            QAction.MenuRole.ApplicationSpecificRole
            if sys.platform == "darwin"
            else QAction.MenuRole.NoRole
        )
        updates_action.triggered.connect(self.check_for_updates)
        help_menu.addAction(updates_action)
        about_action = help_menu.addAction("About", self.show_about)
        about_action.setMenuRole(QAction.MenuRole.NoRole)

    def _load_saved_settings(self):
        try:
            saved_model = settings_manager.load_model_selection()
            for tab in MainWindow.existing_transcription_tabs(self):
                tab.set_model_selection(saved_model)
            self.current_model = self.quick_record_tab.current_model
            self._apply_local_engine_visibility(self.current_model)
            logger.info(f"Loaded saved model selection: {saved_model}")
        except Exception as e:
            logger.error(f"Failed to load saved settings: {e}")

    def _on_tab_changed(self, index: int):
        logger.debug(f"Tab changed to index {index}")
        if index == TabbedContentWidget.TAB_UPLOAD_FILE:
            self._ensure_screen("upload")
        elif index == TabbedContentWidget.TAB_WALKTHROUGH:
            self._ensure_screen("walkthrough")
        elif index == TabbedContentWidget.TAB_MEETING_MODE:
            self._ensure_screen("meeting")

        if self._compact_mode and index != TabbedContentWidget.TAB_QUICK_RECORD:
            self.set_compact_mode(False)

        self._current_tab_index = index

        meeting_mode = index == TabbedContentWidget.TAB_MEETING_MODE
        sidebar = self.history_sidebar
        # Meeting Mode and the Walkthrough tab list their own past items there.
        page, panel_name = {
            TabbedContentWidget.TAB_MEETING_MODE: (sidebar.MEETINGS, "Past Meetings"),
            TabbedContentWidget.TAB_WALKTHROUGH: (sidebar.WALKTHROUGHS, "Walkthroughs"),
        }.get(index, (sidebar.HISTORY, "History"))
        sidebar.set_page(page)
        self.history_edge_tab.set_panel_name(panel_name)
        if hasattr(self, "sidebar_action"):
            self.sidebar_action.setText(panel_name)

        self._schedule_history_sidebar_refresh()
        self._refit_page()

        if meeting_mode:
            self._schedule_meeting_mode_intro()

    def _schedule_meeting_mode_intro(self) -> None:
        """Queue the first-visit Meeting Mode overview after the tab is up."""
        if self._meeting_intro_scheduled:
            return
        if self.tabbed_content.current_index() != (
            TabbedContentWidget.TAB_MEETING_MODE
        ):
            return
        if self.tabbed_content.meeting_tab_is_locked():
            return
        if resolve_meeting_mode_intro_seen():
            return
        self._meeting_intro_scheduled = True
        QTimer.singleShot(0, self._maybe_show_meeting_mode_intro)

    def _maybe_show_meeting_mode_intro(self) -> None:
        self._meeting_intro_scheduled = False
        if not self.isVisible():
            return
        if self.tabbed_content.current_index() != (
            TabbedContentWidget.TAB_MEETING_MODE
        ):
            return
        if self.tabbed_content.meeting_tab_is_locked():
            return
        if resolve_meeting_mode_intro_seen():
            return
        # Tray / hotkey start switches to this tab after capture is live.
        if self.meeting_mode_tab.is_meeting_active:
            return
        from ui_qt.dialogs.meeting_intro_dialog import (
            maybe_show_meeting_mode_intro,
        )

        maybe_show_meeting_mode_intro(self)

    def _schedule_history_sidebar_refresh(self) -> None:
        """Defer visible sidebar refreshes so tab clicks stay responsive."""
        if not self.history_sidebar.is_expanded:
            return

        self._tab_history_refresh_timer.start(75)

    def _refresh_history_sidebar_if_expanded(self) -> None:
        if self.history_sidebar.is_expanded:
            self.history_sidebar.refresh()

    def _on_quick_record_toggled(self, is_recording: bool):
        """Forward a record/stop request; chrome follows recording_state_changed."""
        self.record_toggled.emit(is_recording)

    def _on_quick_record_canceled(self):
        self.is_recording = False
        self.compact_controller.set_recording_state(False)
        self.compact_controller.set_status("Ready to record")
        if not self.meeting_is_active():
            self.tabbed_content.set_recording_state(False, -1)

        self.record_canceled.emit()

    def _on_model_changed(self, model_name: str):
        self.current_model = model_name

        # Sync the other tabs without re-emitting the signal
        for tab in MainWindow.existing_transcription_tabs(self):
            tab.set_backend(model_name)

        self._apply_local_engine_visibility(model_name)

        self.model_changed.emit(model_name)

    def _apply_local_engine_visibility(self, model_name: str):
        from services.local_asr.catalog import BACKENDS
        backend = config.MODEL_VALUE_MAP.get(model_name)
        is_local = backend == "local_whisper" or backend in BACKENDS
        tabs = (MainWindow.existing_transcription_tabs(self) if hasattr(self, "existing_transcription_tabs")
                else self.transcription_tabs)
        for tab in tabs:
            tab.set_local_engine_visible(is_local)

    def _on_engine_settings_changed(self):
        """Keep both tabs' engine panels in sync, then notify listeners.

        The emitting widget has already persisted the three keys to settings, so
        both panels reload from that canonical source (signals blocked inside
        ``load_from_settings``). This avoids depending on ``sender()`` identity
        and guarantees the two tabs always agree. ``whisper_engine_changed`` then
        triggers the controller's background reload.
        """
        for tab in MainWindow.existing_transcription_tabs(self):
            tab.local_engine.load_from_settings()
        self.whisper_engine_changed.emit()

    def _on_live_preview_changed(self):
        """Re-sync every tab's Live preview checkbox, then notify listeners.

        Same shape as ``_on_engine_settings_changed``: the emitting tab already
        wrote the setting, so the others reload from it with signals blocked
        and the controller reconfigures the streaming runtime once.
        """
        self.refresh_live_preview_controls()
        self.live_preview_changed.emit()

    def refresh_live_preview_controls(self):
        for tab in MainWindow.existing_transcription_tabs(self):
            tab.load_live_preview_setting()

    def _on_upload_file_transcribe(
        self, audio_path: str, duration_seconds: float = 0.0
    ):
        self.upload_file_requested.emit(audio_path, duration_seconds)

    def _update_recording_state(self):
        self.quick_record_tab.is_recording = self.is_recording
        self.quick_record_tab._update_recording_state()
        self.compact_controller.set_recording_state(self.is_recording)
        self.compact_controller.set_status(
            "Recording in progress..." if self.is_recording else "Ready to record"
        )

        self.update_screen(
            "walkthrough", "set_recording_state", self.is_recording, self.walkthrough_active
        )
        if self.is_recording:
            self.tabbed_content.set_recording_state(True, self._recording_tab())
        elif not self.meeting_is_active():
            self.tabbed_content.set_recording_state(False, -1)

    def _recording_tab(self) -> int:
        """The tab that finishes the recording in progress.

        Compact mode stays on Quick Record: opening another tab leaves it.
        """
        if self.walkthrough_active and not self._compact_mode:
            return TabbedContentWidget.TAB_WALKTHROUGH
        return TabbedContentWidget.TAB_QUICK_RECORD

    def set_walkthrough_active(self, active: bool) -> None:
        """Whether the recording in progress is a walkthrough; its tab then finishes it."""
        self.walkthrough_active = bool(active)
        self.update_screen(
            "walkthrough", "set_recording_state", self.is_recording, self.walkthrough_active
        )
        if self.is_recording:
            self.tabbed_content.set_recording_state(True, self._recording_tab())

    def set_status(self, status_text: str):
        self.quick_record_tab.set_status(status_text)
        self.compact_controller.set_status(status_text)

    def set_device_info(self, device_info: str, ready: Optional[bool] = None):
        self.update_transcription_tabs("set_device_info", device_info, ready)
        self.update_screen("host", "set_device_info", device_info, ready)

    def set_remote_models(self, choices):
        self.update_transcription_tabs("set_remote_models", choices)

    def set_remote_link(self, link):
        self.update_transcription_tabs("set_remote_link", link)

    def set_remote_clients(self, clients):
        self.update_transcription_tabs("set_remote_clients", clients)

    def set_transcript(self, text: str, raw=None):
        self.quick_record_tab.set_transcript(text, raw=raw)

    def append_transcription(self, text: str):
        self.quick_record_tab.append_transcription(text)

    def clear_transcription(self):
        self.quick_record_tab.clear_transcription()

    def set_partial_transcription(self, text: str, is_final: bool):
        self.quick_record_tab.set_partial_transcription(text, is_final)

    def clear_partial_transcription(self):
        self.quick_record_tab.clear_partial_transcription()

    def set_transcription_stats(
        self,
        transcription_time: float,
        audio_duration: float,
        file_size: int,
        cleanup_time: Optional[float] = None,
        remote=None,
    ):
        self.quick_record_tab.set_transcription_stats(
            transcription_time, audio_duration, file_size, cleanup_time, remote=remote
        )

    def clear_transcription_stats(self):
        self.quick_record_tab.clear_transcription_stats()

    def _on_transcription_collapsed(self, collapsed: bool, delta: int):
        """Reclaim/restore window height when the transcription card toggles.

        Keeps both tabs in the same collapsed state, then animates the window
        height by the freed (or restored) body height so the change feels smooth.

        Args:
            collapsed: True if the card was just collapsed, False if expanded.
            delta: The body height that was hidden/shown, in pixels.
        """
        source = self.sender()
        self._shared_tab_updates["set_transcription_collapsed"] = ((collapsed,), {})
        for tab in MainWindow.existing_transcription_tabs(self):
            if tab is not source:
                tab.set_transcription_collapsed(collapsed)

        if not self.isVisible() or self._compact_mode or self._host_mode:
            return
        # The window moves with the card's own animation, then refits to the
        # page once both settle (_on_resize_animation_finished).
        current_height = self.height()
        if collapsed:
            # Remember the open transcript's height for the next expand.
            self._transcript_height = current_height
            target_height = current_height - delta
            self.setMinimumHeight(min(self.minimumHeight(), target_height))
        else:
            target_height = max(current_height + delta, self._transcript_height)
            self._transcript_height = target_height
        self._animate_resize(self.width(), target_height)

    def _schedule_page_fit(self) -> None:
        """Queue a page size check for the next event-loop pass.

        Deferring keeps the check off the state-update path and coalesces a
        burst of layout changes into one fit.
        """
        if not self._compositor_managed:
            self._page_fit_timer.start(0)

    def _refit_page(self, animate: bool = True) -> None:
        """Fit the window to the page now, even if its size has not changed.

        Not deferred: a page just switched to would otherwise show a frame at
        the old height, with a scrollbar it is about to lose.
        """
        self._page_fit = None
        self._page_width_need = None
        self._sync_page_size(animate)

    def _current_page(self) -> QWidget:
        return self.tabbed_content.stack.currentWidget()

    def _page_is_elastic(self, page: QWidget) -> bool:
        """An open transcript or a walkthrough storyboard fills whatever height the user gives it."""
        return hasattr(page, "fills_height") and page.fills_height()

    def _page_content_height(self, page: QWidget, width: int) -> int:
        """The height ``page`` needs to show all of its content ``width`` wide without scrolling."""
        layout = page.scroll_area.widget().layout()
        if layout.hasHeightForWidth() and width > 0:
            return max(layout.minimumHeightForWidth(width), layout.heightForWidth(width))
        return max(layout.minimumSize().height(), layout.sizeHint().height())

    def _page_content_width(self, page: QWidget) -> int:
        """The width ``page``'s content needs: all of it unclipped, or more where it reads better."""
        need = page.scroll_area.widget().minimumSizeHint().width()
        if hasattr(page, "preferred_width"):
            need = max(need, page.preferred_width())
        return need

    def _page_width(self, page: QWidget) -> int:
        """The window width for ``page``: the user's own, widened while the page needs more.

        Widening remembers the width it started from, and the next page that
        fits in that width gets it back, so the Walkthrough tab's storyboard
        doesn't leave every other tab wide. The width is chosen again only
        when the page's need changes (or on _refit_page), so a width the user
        drags stays until then.
        """
        need = self._page_content_width(page)
        if need == self._page_width_need:
            return self.width()
        self._page_width_need = need
        panel = self.history_sidebar.width()
        own = self.width() if self._own_width is None else self._own_width + panel
        # Everything beside the page's content (side panel included) stays as wide.
        wanted = self.width() - page.scroll_area.contentsRect().width() + need
        if wanted <= own:
            self._own_width = None
            return own
        if self._own_width is None:
            self._own_width = self.width() - panel
        return wanted

    def _sync_page_size(self, animate: bool = True) -> None:
        """Smoothly fit the window to the selected tab's page.

        Every tab sizes the window's height to its page, so none shows blank
        space below its content or a scrollbar it does not need, and the
        window follows as rows come and go. An open transcript is the
        exception: it fills the height the user last gave it. A page that
        needs more width than the window has widens it (_page_width). The
        window refits only when the page's needs change (or on _refit_page),
        so a size the user drags stays until then.
        """
        page = self._current_page()
        if (
            self._compositor_managed
            or self._compact_mode
            or self._host_mode
            or not self.isVisible()
            or self.isMaximized()
            or self.title_bar._is_maximized
            or self._resizing
            or not hasattr(page, "scroll_area")
        ):
            return
        if (
            self._resize_animation_running()
            or self.history_sidebar.animation.state() == QPropertyAnimation.State.Running
        ):
            # Refit once the window settles (_on_resize_animation_finished,
            # _on_sidebar_animation_finished).
            return

        from PyQt6.QtWidgets import QApplication

        # Settle pending layouts so the chrome and the page measure as shown.
        QApplication.sendPostedEvents(None, QEvent.Type.LayoutRequest)
        width = self._clamp_geometry(0, 0, self._page_width(page), self.height()).width()
        chrome = self.height() - self.tabbed_content.stack.height()
        # The width the content gets once the window is there, with no scrollbar.
        content_width = page.scroll_area.contentsRect().width() + width - self.width()
        fit = min(
            chrome + self._page_content_height(page, content_width), self._max_usable_height()
        )
        if fit == self._page_fit and width == self.width():
            return
        self._page_fit = fit
        self._page_floor = min(fit, config.MAIN_WINDOW_MIN_HEIGHT)
        target = max(fit, self._transcript_height) if self._page_is_elastic(page) else fit
        # Lower the floor first so a shrink is not stopped short; a rise
        # waits until the window is there, or it would jump.
        self.setMinimumHeight(min(self._page_floor, self.height()))
        if abs(target - self.height()) > 1 or width != self.width():
            if animate:
                self._animate_resize(width, target)
                return
            geo = self.geometry()
            self.setGeometry(self._clamp_geometry(geo.x(), geo.y(), width, target))
        self.setMinimumHeight(self._page_floor)

    def _resize_animation_running(self) -> bool:
        return (
            hasattr(self, "_resize_animation")
            and self._resize_animation.state() == QPropertyAnimation.State.Running
        )

    def _on_resize_animation_finished(self) -> None:
        if self._compact_mode or self._host_mode:
            return
        # A transcript toggle moves the window ahead of the floor's refit.
        self.setMinimumHeight(min(self._page_floor, self.height()))
        self._schedule_page_fit()

    def _max_usable_height(self) -> int:
        from PyQt6.QtWidgets import QApplication

        screen = (
            QApplication.screenAt(self.geometry().center())
            or QApplication.primaryScreen()
        )
        if screen is None:
            return UNLIMITED_HEIGHT
        return screen.availableGeometry().height()

    def _on_stats_visibility_changed(self, visible: bool):
        if self._host_mode:
            # A dictation finished behind the dashboard, which is not its page.
            return
        stats = self.sender()
        if visible and isinstance(stats, QWidget):
            self._stats_to_reveal = stats
            self._stats_reveal_timer.start()

    def _reveal_shown_stats(self) -> None:
        """Scroll a result's stats into view below the transcript.

        The window grows to fit them like any other row, but not past the
        screen (1024x640), past an open transcript's height, or where the
        compositor sizes it, which left the stats under the tab's fold,
        behind the footer.
        """
        from PyQt6.QtWidgets import QApplication, QScrollArea

        stats, self._stats_to_reveal = self._stats_to_reveal, None
        try:
            if stats is None or not stats.isVisible():
                return
            QApplication.sendPostedEvents(None, QEvent.Type.LayoutRequest)
            area = stats.parentWidget()
            while area is not None:
                if isinstance(area, QScrollArea):
                    area.ensureWidgetVisible(stats, 0, 12)
                area = area.parentWidget()
        except RuntimeError:
            pass  # The tab was deleted before the window settled.

    def open_settings(self):
        logger.info("Opening settings dialog")
        self.settings_requested.emit()

    def open_downloads(self):
        logger.info("Opening Settings on Downloads")
        self.settings_destination_requested.emit("downloads")

    def open_hotkey_settings(self):
        logger.info("Opening hotkey settings")
        self.hotkeys_requested.emit()

    def check_for_updates(self):
        logger.info("Check for updates requested")
        self.check_for_updates_requested.emit()

    def show_about(self):
        logger.info("Showing about dialog")
        self.about_requested.emit()

    def set_update_notice(self, version: str) -> None:
        """Offer the newer release ``version`` in the title bar; "" hides it."""
        self.update_notice.show_version(version)

    def set_tray_available(self, available: bool) -> None:
        """Enable tray-only UI only when the desktop actually has a tray."""
        self._tray_available = bool(available)
        self.tray_button.setVisible(self._tray_available)
        self.minimize_to_tray_action.setVisible(self._tray_available)
        if not self._tray_available:
            self.tray_button.setEnabled(False)
            self.tray_button.setToolTip(
                "This desktop session does not provide a system tray."
            )

    def minimize_to_tray(self):
        if not self._tray_available:
            logger.info("No system tray is available; minimizing the window")
            self.showMinimized()
            return
        logger.info("Minimizing to tray")
        self.hide()

    def toggle_compact_mode(self) -> None:
        if self._host_mode:
            return
        self.set_compact_mode(not self._compact_mode)

    def set_compact_mode(self, compact: bool, persist: bool = True) -> None:
        """Apply compact or full main-window mode.

        Args:
            compact: Whether to show the compact recording controller.
            persist: Whether to save the selected mode to settings.
        """
        if compact == self._compact_mode:
            return
        if compact and self._host_mode:
            return

        if (
            hasattr(self, "_resize_animation")
            and self._resize_animation.state() == QPropertyAnimation.State.Running
        ):
            self._resize_animation.stop()

        if compact:
            self._enter_compact_mode()
        else:
            self._leave_compact_mode()

        if persist:
            try:
                settings_manager.save_setting(SettingsKey.COMPACT_MODE, compact)
            except Exception as e:
                logger.warning(f"Failed to save compact mode: {e}")

    def _enter_compact_mode(self) -> None:
        if not self._compositor_managed and self.title_bar._is_maximized:
            self.title_bar._toggle_maximize()
        elif not self._compositor_managed and self.isMaximized():
            self.showNormal()

        self._full_geometry = QRect(self.geometry())
        self._save_geometry()
        self._compact_mode = True

        self.tabbed_content.hide()
        self.compact_controller.show()
        # Four footer buttons don't fit the compact width; Stats stays in the tray menu.
        self.stats_button.hide()
        self.history_edge_tab.hide()
        self.history_sidebar.hide()
        self.title_bar.title_label.hide()
        self.update_notice.set_compact(True)
        if not self._compositor_managed:
            self.title_bar.maximize_btn.hide()
            self.setMinimumSize(0, 0)
            self.setMaximumSize(UNLIMITED_HEIGHT, UNLIMITED_HEIGHT)
            self.setFixedSize(
                config.MAIN_WINDOW_COMPACT_WIDTH,
                config.MAIN_WINDOW_COMPACT_HEIGHT,
            )
        self._restore_compact_geometry()

    def _leave_compact_mode(self) -> None:
        self._save_compact_geometry()
        self._compact_mode = False

        if not self._compositor_managed:
            self.setMinimumSize(config.MAIN_WINDOW_MIN_WIDTH, self._page_floor)
            self.setMaximumSize(config.MAIN_WINDOW_MAX_WIDTH, UNLIMITED_HEIGHT)
        self.compact_controller.hide()
        self.tabbed_content.show()
        self.stats_button.show()
        self.history_edge_tab.show()
        self.history_sidebar.show()
        self.title_bar.title_label.show()
        self.update_notice.set_compact(False)
        if not self._compositor_managed:
            self.title_bar.maximize_btn.show()

        if self._full_geometry is not None and not self._compositor_managed:
            self.setGeometry(self._full_geometry)
        else:
            self._restore_window_geometry()

        self._refit_page()

    def _restore_compact_mode(self) -> None:
        try:
            default = SETTING_DEFAULTS[SettingsKey.COMPACT_MODE]
            if settings_manager.get(SettingsKey.COMPACT_MODE, default) is True:
                self.set_compact_mode(True, persist=False)
        except Exception as e:
            logger.warning(f"Failed to restore compact mode: {e}")

    def _save_compact_geometry(self) -> None:
        if self._compositor_managed:
            return
        geo = self.geometry()
        try:
            settings_manager.save_setting(
                SettingsKey.COMPACT_WINDOW_GEOMETRY,
                {"x": geo.x(), "y": geo.y()},
            )
        except Exception as e:
            logger.warning(f"Failed to save compact window geometry: {e}")

    def _restore_compact_geometry(self) -> None:
        if self._compositor_managed:
            return
        x = self.x()
        y = self.y()
        try:
            geo = settings_manager.get(SettingsKey.COMPACT_WINDOW_GEOMETRY)
            if isinstance(geo, dict) and {"x", "y"}.issubset(geo):
                x = int(geo["x"])
                y = int(geo["y"])
        except (TypeError, ValueError) as e:
            logger.warning(f"Invalid compact window geometry: {e}")

        from PyQt6.QtWidgets import QApplication

        screen = QApplication.screenAt(self.geometry().center()) or QApplication.primaryScreen()
        if screen:
            available = screen.availableGeometry()
            x = min(max(x, available.left()), available.right() - self.width() + 1)
            y = min(max(y, available.top()), available.bottom() - self.height() + 1)
        self.move(x, y)

    @property
    def host_mode(self) -> bool:
        return self._host_mode

    def toggle_host_mode(self, _checked: Optional[bool] = None) -> None:
        self.set_host_mode(not self._host_mode)

    def set_host_mode(self, enabled: bool, persist: bool = True) -> None:
        """Show the host dashboard instead of the recording tabs, or go back.

        For a computer that only shares its engine with paired computers, where
        recording controls are no use. Like compact mode, the dashboard keeps
        its own window size and position, so each view comes back as it was
        left.

        Args:
            enabled: Whether to show the host dashboard.
            persist: Whether to save the choice to settings.
        """
        enabled = bool(enabled)
        if enabled == self._host_mode:
            self.host_mode_action.setChecked(enabled)
            return

        if (
            hasattr(self, "_resize_animation")
            and self._resize_animation.state() == QPropertyAnimation.State.Running
        ):
            self._resize_animation.stop()
        if not self._compositor_managed and self.title_bar._is_maximized:
            self.title_bar._toggle_maximize()
        elif not self._compositor_managed and self.isMaximized():
            self.showNormal()

        if enabled:
            if self._compact_mode:
                self.set_compact_mode(False, persist=persist)
            self._save_geometry()
            self._pre_host_geometry = QRect(self.geometry())
            self._host_mode = True
            self.tabbed_content.hide()
            self.history_edge_tab.hide()
            self.history_sidebar.hide()
            self.host_dashboard.show()
            self.title_bar.title_label.setText("OpenWhisper Host")
            self._restore_host_geometry()
        else:
            self._save_host_geometry()
            self._host_mode = False
            self.host_dashboard.hide()
            self.tabbed_content.show()
            self.history_edge_tab.show()
            self.history_sidebar.show()
            self.title_bar.title_label.setText("OpenWhisper")
            if self._pre_host_geometry is not None and not self._compositor_managed:
                self.setGeometry(self._pre_host_geometry)
            else:
                self._restore_window_geometry()
            self._pre_host_geometry = None
            self._refit_page()

        # History and compact mode belong to the recording view.
        self.sidebar_action.setEnabled(not enabled)
        self.compact_action.setEnabled(not enabled)
        self.host_mode_action.setChecked(enabled)

        if persist:
            try:
                settings_manager.save_setting(SettingsKey.HOST_MODE, enabled)
            except Exception as e:
                logger.warning(f"Failed to save host mode: {e}")

    def _restore_host_mode(self) -> None:
        try:
            if settings_manager.get(
                SettingsKey.HOST_MODE, SETTING_DEFAULTS[SettingsKey.HOST_MODE]
            ) is True:
                self.set_host_mode(True, persist=False)
        except Exception as e:
            logger.warning(f"Failed to restore host mode: {e}")

    def _save_host_geometry(self) -> None:
        if self._compositor_managed:
            return
        if self.isMaximized() or self.isMinimized():
            return
        geo = self.geometry()
        try:
            settings_manager.save_setting(
                SettingsKey.HOST_WINDOW_GEOMETRY,
                {"x": geo.x(), "y": geo.y(), "width": geo.width(), "height": geo.height()},
            )
        except Exception as e:
            logger.warning(f"Failed to save host window geometry: {e}")

    def _restore_host_geometry(self) -> None:
        if self._compositor_managed:
            return
        from PyQt6.QtWidgets import QApplication

        target = None
        try:
            saved = settings_manager.get(SettingsKey.HOST_WINDOW_GEOMETRY)
            if isinstance(saved, dict) and {"x", "y", "width", "height"}.issubset(saved):
                target = QRect(
                    int(saved["x"]), int(saved["y"]),
                    int(saved["width"]), int(saved["height"]),
                )
        except (TypeError, ValueError) as e:
            logger.warning(f"Invalid host window geometry: {e}")
        if target is None or not any(
            screen.availableGeometry().intersects(target)
            for screen in QApplication.screens()
        ):
            # The first time: grow from where the window is.
            current = self.geometry()
            target = QRect(
                current.x(),
                current.y(),
                max(current.width(), config.MAIN_WINDOW_HOST_DEFAULT_WIDTH),
                max(current.height(), config.MAIN_WINDOW_HOST_DEFAULT_HEIGHT),
            )
        # Move first, so the clamp uses the screen the rect is on.
        self.setGeometry(target)
        self.setGeometry(
            self._clamp_geometry(target.x(), target.y(), target.width(), target.height())
        )

    def toggle_tray_visibility(self):
        if self.isVisible() and not self.isMinimized():
            self.minimize_to_tray()
            return

        self.restore_from_tray()

    def restore_from_tray(self):
        """Reliably bring the window back from the tray / hidden state.

        macOS needs the full clear-minimized + show + raise + activate
        sequence: once an app has no visible windows it is deactivated, so a
        bare showNormal() can leave the window hidden behind other apps (or not
        appear at all). The sequence is harmless on Windows, which restores fine
        from showNormal() alone.
        """
        logger.info("Restoring window from tray")
        # Drop any minimized bit and mark the window active before showing.
        self.setWindowState(
            (self.windowState() & ~Qt.WindowState.WindowMinimized)
            | Qt.WindowState.WindowActive
        )
        if self._compositor_managed:
            self.show()
        else:
            self.showNormal()
        self.raise_()
        self.activateWindow()

    def request_quit(self) -> None:
        """Quit for the user, after they confirm stopping work in progress."""
        if self.confirm_quit is not None and not self.confirm_quit():
            logger.info("Quit canceled: work in progress")
            return
        self.quit_application()

    def quit_application(self):
        logger.info("Quitting application")
        self._save_geometry()
        self._force_quit = True
        from PyQt6.QtWidgets import QApplication
        QApplication.instance().quit()

    def toggle_history(self):
        logger.info("Toggling contextual sidebar")

        if self._host_mode:
            return
        if self._compact_mode:
            self.set_compact_mode(False)
            if self.history_sidebar.is_expanded:
                return

        will_be_expanded = not self.history_sidebar.is_expanded
        self.history_edge_tab.set_expanded(will_be_expanded)

        # A running height animation would fight the per-frame lockstep resize.
        if (
            hasattr(self, '_resize_animation')
            and self._resize_animation.state() == QPropertyAnimation.State.Running
        ):
            self._resize_animation.stop()

        # Capture the width of everything except the sidebar so each animation
        # frame can hold the main content area at a constant width. Works
        # mid-animation too: the sidebar's current width is subtracted out.
        self._sidebar_base_width = self.width() - self.history_sidebar.width()

        # The sidebar's single animation drives the window width via
        # width_animated -> _on_sidebar_width_animated.
        self.history_sidebar.toggle()

    def _on_sidebar_width_animated(self, sidebar_width: int):
        """Resize the window in lockstep with the sidebar width animation.

        Args:
            sidebar_width: Current animated width of the history sidebar.
        """
        if self._compositor_managed:
            return
        base = getattr(self, '_sidebar_base_width', None)
        if base is None:
            return

        target_width = min(self.maximumWidth(), base + sidebar_width)
        geo = self.geometry()
        self.setGeometry(
            self._clamp_geometry(geo.x(), geo.y(), target_width, geo.height())
        )

    def _available_screen_rect(self) -> QRect:
        """Return the available geometry of the screen that contains this window."""
        from PyQt6.QtWidgets import QApplication

        screen = (
            QApplication.screenAt(self.frameGeometry().center())
            or QApplication.primaryScreen()
        )
        if screen is None:
            return QRect(0, 0, 1280, 800)
        return screen.availableGeometry()

    def _clamp_geometry(self, x: int, y: int, width: int, height: int) -> QRect:
        """Clamp a candidate window rect so it stays fully on the current screen.

        Args:
            x: Proposed left edge.
            y: Proposed top edge.
            width: Proposed width.
            height: Proposed height.

        Returns:
            A ``QRect`` that fits inside the available screen area.
        """
        avail = self._available_screen_rect()
        max_width = min(self.maximumWidth(), avail.width())
        max_height = avail.height()
        width = max(self.minimumWidth(), min(width, max_width))
        height = max(self.minimumHeight(), min(height, max_height))
        max_x = avail.x() + avail.width() - width
        max_y = avail.y() + avail.height() - height
        x = min(max(x, avail.x()), max(avail.x(), max_x))
        y = min(max(y, avail.y()), max(avail.y(), max_y))
        return QRect(x, y, width, height)

    def _center_on_screen(self) -> None:
        """Place the window in the center of the available screen area."""
        avail = self._available_screen_rect()
        self.setGeometry(
            self._clamp_geometry(
                avail.x() + (avail.width() - self.width()) // 2,
                avail.y() + (avail.height() - self.height()) // 2,
                self.width(),
                self.height(),
            )
        )

    def _on_sidebar_animation_finished(self) -> None:
        page = self._current_page()
        if hasattr(page, "scroll_area"):
            page.updateGeometry()
            page.scroll_area.widget().updateGeometry()
            page.scroll_area.viewport().update()
        self._schedule_page_fit()

    def _animate_resize(self, target_width: int, target_height: int):
        """Animate window resize.

        Args:
            target_width: Target window width.
            target_height: Target window height.
        """
        if self._compositor_managed:
            return
        if not hasattr(self, '_resize_animation'):
            self._resize_animation = QPropertyAnimation(self, b"geometry")
            self._resize_animation.setDuration(SECTION_COLLAPSE_DURATION_MS)
            self._resize_animation.setEasingCurve(SECTION_COLLAPSE_EASING)
            self._resize_animation.finished.connect(self._on_resize_animation_finished)
            self._resize_animation.stateChanged.connect(self._on_resize_animation_state)

        current_geo = self.geometry()
        target_geo = self._clamp_geometry(
            current_geo.x(), current_geo.y(), target_width, target_height
        )

        # Continue smoothly from the current frame when interrupting a resize.
        if self._resize_animation.state() == QPropertyAnimation.State.Running:
            current_geo = self._resize_animation.currentValue()

        self._resize_animation.stop()
        self._resize_animation.setDuration(SECTION_COLLAPSE_DURATION_MS)
        self._resize_animation.setEasingCurve(SECTION_COLLAPSE_EASING)
        self._resize_animation.setStartValue(current_geo)
        self._resize_animation.setEndValue(target_geo)
        self._resize_animation.start()

    def _on_resize_animation_state(self, new_state, _old_state) -> None:
        """Keep the page's scrollbar away while the window grows to fit it.

        Mid-animation the page is briefly taller than its viewport, which
        would flash a scrollbar the settled window does not need.
        """
        running = new_state == QPropertyAnimation.State.Running
        page = self._current_page() if running else self._unscrolled_page
        self._unscrolled_page = page if running else None
        if hasattr(page, "scroll_area"):
            page.scroll_area.setVerticalScrollBarPolicy(
                Qt.ScrollBarPolicy.ScrollBarAlwaysOff if running
                else Qt.ScrollBarPolicy.ScrollBarAsNeeded
            )

    def refresh_history(self):
        self.history_sidebar.refresh()
        self._refresh_history_calendar()

    def _on_walkthrough_picked(self, folder) -> None:
        """A side-panel walkthrough opens in the tab; "Open library" opens them all."""
        if folder is None:
            self.walkthrough_library_requested.emit(None)
        else:
            self.walkthrough_tab.show_walkthrough(folder, ask=True)

    def show_walkthrough(self, folder) -> None:
        """Bring the window forward on the Walkthrough tab, showing ``folder``."""
        self.restore_from_tray()
        self.tabbed_content.set_current_index(TabbedContentWidget.TAB_WALKTHROUGH)
        self.walkthrough_tab.show_walkthrough(folder, ask=True)

    def walkthrough_finished(self, folder) -> None:
        """Show a walkthrough that just finished, unless edits to another are open."""
        tab = self._screen_widgets.get("walkthrough")
        if tab is not None:
            tab.show_walkthrough(folder)

    def refresh_past_meetings(self) -> None:
        if self.tabbed_content.current_index() == TabbedContentWidget.TAB_MEETING_MODE:
            self.history_sidebar.refresh()
        self._refresh_history_calendar()

    def open_history_calendar(self) -> None:
        """Show the History calendar: every dictation, file and meeting by month."""
        dialog = self._history_calendar
        if dialog is None:
            from ui_qt.dialogs.history_calendar_dialog import HistoryCalendarDialog

            dialog = HistoryCalendarDialog(parent=self)
            dialog.entry_requested.connect(
                lambda entry: self._open_history_entry(entry, parent=self._history_calendar)
            )
            dialog.meeting_requested.connect(self.past_meeting_requested.emit)
            dialog.meeting_copy_requested.connect(self.past_meeting_copy_requested.emit)
            dialog.entry_copied.connect(self._on_history_entry_copied)
            self._history_calendar = dialog
        if dialog.isVisible():
            dialog.refresh()
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        logger.info("Opened the History calendar")

    def _refresh_history_calendar(self) -> None:
        if self._history_calendar is not None:
            self._history_calendar.refresh_if_visible()

    def _on_history_entry_selected(self, entry_id: str):
        # Or one the paired host keeps for this computer.
        entry = self.history_sidebar.entry_for(entry_id)
        if not entry:
            return
        self._open_history_entry(entry)

    def _open_history_entry(self, entry, parent=None) -> None:
        """View one History entry, from the sidebar or the calendar."""
        dialog = HistoryEntryDialog(entry, parent=parent or self)
        dialog.copied.connect(self._on_history_entry_copied_from_dialog)
        dialog.retranscribe_requested.connect(self._on_retranscribe_requested)
        dialog.delete_requested.connect(
            lambda _entry_id, entry=entry: self._delete_history_entry(entry)
        )
        # An edit or a restored version changes the text both lists show.
        dialog.version_changed.connect(lambda _entry_id: self.refresh_history())
        dialog.exec()
        logger.info(f"Opened history entry dialog: {entry.id[:8]}...")

    def _on_history_entry_copied_from_dialog(self):
        self.set_status("Copied to clipboard")
        QTimer.singleShot(2000, lambda: self.set_status("Ready to record"))
        if self.on_show_copied_animation:
            self.on_show_copied_animation()

    def _delete_history_entry(self, entry) -> None:
        """Delete an entry the entry dialog showed, here or on the host keeping it."""
        if getattr(entry, "stored_on", None):
            self.history_sidebar.delete_remote_entry(entry.id)
            return
        if history_manager.delete_entry(entry.id):
            self.refresh_history()
            self._on_history_entry_deleted(entry.id)
            logger.info(f"Deleted history entry from dialog: {entry.id[:8]}...")

    def _on_history_entry_copied(self, entry_id: str):
        self.set_status("Copied to clipboard")
        QTimer.singleShot(2000, lambda: self.set_status("Ready to record"))

    def _on_history_entry_deleted(self, entry_id: str):
        self._refresh_history_calendar()
        self.set_status("Entry deleted")
        QTimer.singleShot(2000, lambda: self.set_status("Ready to record"))

    def _on_retranscribe_requested(self, audio_path: str):
        logger.info("Re-transcribe requested: %s", audio_path)
        self.retranscribe_requested.emit(audio_path)

    def closeEvent(self, event):
        logger.info("Main window closing")
        self.tabbed_content.flush_pending_tab_selection()
        if self._force_quit:
            logger.info("Force quit - closing application")
            event.accept()
            return

        try:
            settings = settings_manager.load_all_settings()
            minimize_tray = setting_value(SettingsKey.MINIMIZE_TRAY, settings)
        except Exception as e:
            logger.error(f"Failed to load settings: {e}")
            minimize_tray = True  # Default to True on error

        if minimize_tray and self._tray_available:
            event.ignore()
            try:
                self.hide()
                logger.info("Window hidden to system tray")
            except Exception as e:
                logger.debug(f"Error hiding window: {e}")
                event.accept()
        else:
            event.accept()

    def update_hotkeys(
        self,
        record_key: str,
        cancel_key: str,
        enable_disable_key: str = "",
        minimize_key: str = "",
        walkthrough_key: str = "",
        screenshot_key: str = "",
    ):
        """
        Update the hotkey display on buttons.

        Args:
            record_key: The key for recording
            cancel_key: The key for canceling
            enable_disable_key: The key for enabling/disabling STT
            minimize_key: The key for minimizing to the system tray
            walkthrough_key: The key that starts and finishes a walkthrough
            screenshot_key: The key that takes a screenshot
        """
        self.quick_record_tab.update_hotkeys(record_key, cancel_key, enable_disable_key)
        self.update_screen("walkthrough", "set_hotkeys", walkthrough_key, screenshot_key)
        self.compact_controller.update_hotkeys(record_key, cancel_key)
        self.tray_button.set_hotkey(minimize_key)

    def _get_resize_edge(self, pos) -> tuple:
        """Determine which edge(s) the cursor is near.

        Args:
            pos: QPoint position relative to window.

        Returns:
            Tuple of (horizontal_edge, vertical_edge) where each is:
            -1 for left/top, 0 for none, 1 for right/bottom.
        """
        if self._compact_mode or self._compositor_managed:
            return (0, 0)

        rect = self.rect()
        margin = self._resize_margin

        horizontal = 0  # -1 = left, 0 = none, 1 = right
        vertical = 0    # -1 = top, 0 = none, 1 = bottom

        if pos.x() <= margin:
            horizontal = -1
        elif pos.x() >= rect.width() - margin:
            horizontal = 1

        if pos.y() <= margin:
            vertical = -1
        elif pos.y() >= rect.height() - margin:
            vertical = 1

        return (horizontal, vertical)

    def _update_cursor_for_edge(self, edge: tuple):

        h, v = edge

        if h == 0 and v == 0:
            self.setCursor(Qt.CursorShape.ArrowCursor)
        elif h != 0 and v == 0:
            self.setCursor(Qt.CursorShape.SizeHorCursor)
        elif h == 0 and v != 0:
            self.setCursor(Qt.CursorShape.SizeVerCursor)
        elif (h == -1 and v == -1) or (h == 1 and v == 1):
            self.setCursor(Qt.CursorShape.SizeFDiagCursor)
        else:  # (h == -1 and v == 1) or (h == 1 and v == -1)
            self.setCursor(Qt.CursorShape.SizeBDiagCursor)

    def _begin_resize(self, edge: tuple, global_pos) -> None:
        self._resizing = True
        self._resize_edge = edge
        self._resize_start_pos = global_pos
        self._resize_start_geometry = self.geometry()

    def _apply_resize_delta(self, global_pos) -> None:
        if not self._resizing or not self._resize_edge or not self._resize_start_geometry:
            return

        delta = global_pos - self._resize_start_pos
        geo = self._resize_start_geometry
        h, v = self._resize_edge

        new_x = geo.x()
        new_y = geo.y()
        new_width = geo.width()
        new_height = geo.height()

        if h == -1:
            new_width = max(self.minimumWidth(), geo.width() - delta.x())
            new_x = geo.x() + geo.width() - new_width
        elif h == 1:
            new_width = min(self.maximumWidth(), max(self.minimumWidth(), geo.width() + delta.x()))

        if v == -1:
            new_height = max(self.minimumHeight(), geo.height() - delta.y())
            new_y = geo.y() + geo.height() - new_height
        elif v == 1:
            new_height = max(self.minimumHeight(), geo.height() + delta.y())

        self.setGeometry(new_x, new_y, new_width, new_height)

    def _finish_resize(self) -> None:
        if not self._resizing:
            return
        self._resizing = False
        self._resize_edge = None
        self._resize_start_pos = None
        self._resize_start_geometry = None
        self._schedule_geometry_save()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            edge = self._get_resize_edge(event.position().toPoint())
            if edge != (0, 0):
                self._begin_resize(edge, event.globalPosition().toPoint())
                event.accept()
                return

        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._resizing and self._resize_edge:
            self._apply_resize_delta(event.globalPosition().toPoint())
            event.accept()
            return

        edge = self._get_resize_edge(event.position().toPoint())
        self._update_cursor_for_edge(edge)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._resizing:
            self._finish_resize()
            event.accept()
            return

        super().mouseReleaseEvent(event)

    def _schedule_geometry_save(self):
        if self._compositor_managed:
            return
        if not self._initial_show_complete:
            return
        if self._geometry_save_timer is None:
            self._geometry_save_timer = QTimer(self)
            self._geometry_save_timer.setSingleShot(True)
            self._geometry_save_timer.timeout.connect(self._save_geometry)

        self._geometry_save_timer.stop()
        self._geometry_save_timer.start(500)

    def _save_geometry(self):
        if self._compositor_managed:
            return
        if self.isMaximized() or self.isMinimized():
            return

        if self._compact_mode:
            self._save_compact_geometry()
            return
        if self._host_mode:
            self._save_host_geometry()
            return

        geo = self.geometry()
        width = geo.width()
        history_expanded = (
            hasattr(self, "history_sidebar") and self.history_sidebar.is_expanded
        )
        if history_expanded:
            width = max(self.minimumWidth(), width - self._sidebar_width)
        if self._own_width is not None:
            # A page widened the window for itself; the width is the user's own.
            width = self._own_width

        try:
            settings_manager.save_setting(
                SettingsKey.WINDOW_GEOMETRY,
                {
                    'x': geo.x(),
                    'y': geo.y(),
                    'width': width,
                    'height': geo.height(),
                    'format': self._geometry_format,
                    'history_expanded': history_expanded,
                },
            )
        except Exception as e:
            logger.warning(f"Failed to save window geometry: {e}")

    def _restore_window_geometry(self):
        if self._compositor_managed:
            if not self._initial_show_complete:
                available = self.screen().availableGeometry()
                self.resize(min(960, available.width() - 64), min(680, available.height() - 64))
            return
        try:
            geo = settings_manager.get(SettingsKey.WINDOW_GEOMETRY)
            if self._apply_saved_geometry(geo):
                return

            logger.debug("No valid saved geometry, using default")
            self._center_on_screen()
        except Exception as e:
            logger.warning(f"Failed to restore window geometry: {e}")
            self._center_on_screen()

    def _apply_saved_geometry(self, geo) -> bool:
        """Restore ``geo`` when it's complete and still on screen; returns whether it was."""
        if not (isinstance(geo, dict) and {'x', 'y', 'width', 'height'}.issubset(geo.keys())):
            return False
        from PyQt6.QtWidgets import QApplication
        from PyQt6.QtCore import QRect

        screen = QApplication.primaryScreen()
        if not screen:
            return False
        screen_geo = screen.availableGeometry()
        saved_rect = QRect(geo['x'], geo['y'], geo['width'], geo['height'])
        if not screen_geo.intersects(saved_rect):
            return False
        width = self._restored_width(geo)
        # Only a start: showing the window fits its height to the tab.
        height = max(self.minimumHeight(), min(geo['height'], screen_geo.height()))
        restore_width = width
        if (
            hasattr(self, "history_sidebar")
            and self.history_sidebar.is_expanded
        ):
            restore_width = min(
                self.maximumWidth(),
                width + self._sidebar_width,
            )
        clamped = self._clamp_geometry(
            geo["x"], geo["y"], restore_width, height
        )
        self.setGeometry(clamped)
        logger.info(f"Restored window geometry: {geo}")
        return True

    def _restored_width(self, geo: dict) -> int:
        """The saved width, without a legacy expanded sidebar, clamped to the window's limits."""
        raw_width = geo['width']
        migrated_expanded_width = False
        legacy_expanded_width = (
            config.MAIN_WINDOW_DEFAULT_WIDTH
            + config.MAIN_WINDOW_HISTORY_SIDEBAR_WIDTH
            - config.MAIN_WINDOW_HISTORY_EDGE_TAB_WIDTH
        )
        if (
            geo.get('format') != self._geometry_format
            and raw_width >= legacy_expanded_width
        ):
            raw_width -= config.MAIN_WINDOW_HISTORY_SIDEBAR_WIDTH
            migrated_expanded_width = True

        width = max(self.minimumWidth(), min(raw_width, self.maximumWidth()))
        # Normalize narrow and legacy sidebar-width restores.
        if width <= config.MAIN_WINDOW_DEFAULT_WIDTH or migrated_expanded_width:
            width = config.MAIN_WINDOW_DEFAULT_WIDTH
        return width

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # An open transcript keeps the height the user drags it to.
        if (
            not self._compact_mode
            and not self._host_mode
            and hasattr(self, "tabbed_content")
            and self._page_is_elastic(self._current_page())
            and not self._resize_animation_running()
        ):
            self._transcript_height = event.size().height()

        # A narrower window rewraps a page's text, changing how much height
        # it needs. Height-only changes cannot alter the wrap.
        if event.oldSize().width() != event.size().width():
            self._schedule_page_fit()
        if not self._resizing:
            self._schedule_geometry_save()

    def moveEvent(self, event):
        super().moveEvent(event)
        self._schedule_geometry_save()

    def showEvent(self, event):
        super().showEvent(event)

        # Skip geometry restoration on initial show (already handled in __init__)
        # This prevents interference with Qt's initial layout calculation
        if not self._initial_show_complete:
            self._initial_show_complete = True
            self._refit_page(animate=False)
            self._schedule_meeting_mode_intro()
            return

        if not self.isMaximized():
            if self._compact_mode:
                self._restore_compact_geometry()
            elif self._host_mode:
                self._restore_host_geometry()
            else:
                self._restore_window_geometry()

        self._refit_page(animate=False)

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.MouseMove and not self._resizing:
            if hasattr(event, 'globalPosition'):
                global_pos = event.globalPosition().toPoint()
                local_pos = self.mapFromGlobal(global_pos)

                if self.rect().contains(local_pos):
                    edge = self._get_resize_edge(local_pos)
                    self._update_cursor_for_edge(edge)

        return super().eventFilter(obj, event)
