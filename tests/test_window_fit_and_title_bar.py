"""The Settings window fits its screen, and the title bar's stylesheet parses.

Both from the Arch laptop in the report: a 1920x1080 panel at 150% is
1280x720, smaller than Settings' 1315x814 default, so the window was cut
off on the right and Hyprland placed it at y=-34. And every launch logged
"Could not parse stylesheet of object CustomTitleBar", on Windows as well.
"""
from PyQt6.QtCore import QRect, QSize, qInstallMessageHandler
from PyQt6.QtWidgets import QApplication, QVBoxLayout, QWidget

from ui_qt.dialogs.settings_dialog import SettingsDialog


class _Screen:
    def __init__(self, width, height):
        self._rect = QRect(0, 0, width, height)

    def availableGeometry(self):
        return self._rect


class _Window:
    """The parts of a QDialog that ``_fit_to_screen`` touches."""

    MINIMUM_SIZE = SettingsDialog.MINIMUM_SIZE

    def __init__(self, size, screen):
        self._size, self._screen, self.minimum = size, screen, None

    def parentWidget(self):
        return None

    def screen(self):
        return self._screen

    def size(self):
        return self._size

    def resize(self, size):
        self._size = size

    def setMinimumSize(self, size):
        self.minimum = size


def test_a_laptop_panel_at_150_percent_shrinks_the_window():
    window = _Window(SettingsDialog.DEFAULT_SIZE, _Screen(1280, 720))

    SettingsDialog._fit_to_screen(window)

    assert window.size().width() <= 1280 - 64 and window.size().height() <= 720 - 64
    assert window.minimum == SettingsDialog.MINIMUM_SIZE


def test_a_big_screen_leaves_the_window_alone():
    window = _Window(SettingsDialog.DEFAULT_SIZE, _Screen(2560, 1440))

    SettingsDialog._fit_to_screen(window)

    assert window.size() == SettingsDialog.DEFAULT_SIZE


def test_a_screen_smaller_than_the_minimum_lowers_the_minimum():
    window = _Window(SettingsDialog.DEFAULT_SIZE, _Screen(800, 480))

    SettingsDialog._fit_to_screen(window)

    assert window.minimum == QSize(736, 416)
    assert window.size() == QSize(736, 416)


def test_the_real_dialog_opens_inside_the_screen():
    dialog = SettingsDialog(get_loaded_model=lambda: None, background_cache_scan=False)
    try:
        dialog.show()
        QApplication.processEvents()
        available = dialog.screen().availableGeometry()
        assert dialog.width() <= available.width()
        assert dialog.height() <= available.height()
    finally:
        dialog.close()


def test_a_window_already_on_screen_stays_put():
    from ui_qt.utils.desktop import on_screen_position

    frame = QRect(40, 30, 900, 500)
    assert on_screen_position(frame, QRect(0, 0, 1024, 640)) == frame.topLeft()


def test_a_window_off_the_right_edge_is_centred_on_the_screen():
    """The Linux report: at 1024x640 Settings opened off the right edge."""
    from PyQt6.QtCore import QPoint
    from ui_qt.utils.desktop import on_screen_position

    available = QRect(0, 0, 1024, 640)
    frame = QRect(300, 120, 960, 576)
    position = on_screen_position(frame, available)
    assert position == QPoint(32, 32)
    assert available.contains(QRect(position, frame.size()))


def test_a_window_bigger_than_the_screen_keeps_its_title_bar_on_it():
    from PyQt6.QtCore import QPoint
    from ui_qt.utils.desktop import on_screen_position

    available = QRect(1920, 24, 1024, 616)
    assert on_screen_position(QRect(2500, 300, 1315, 850), available) == QPoint(1920, 24)


def test_the_real_dialog_moves_wholly_onto_a_small_screen(monkeypatch):
    monkeypatch.setattr("ui_qt.utils.desktop.compositor_managed", lambda: False)
    dialog = SettingsDialog(get_loaded_model=lambda: None, background_cache_scan=False)
    available = QRect(0, 0, 1024, 640)
    try:
        dialog.show()
        QApplication.processEvents()
        dialog.resize(960, 560)
        dialog.move(500, 300)
        QApplication.processEvents()

        dialog._place_on_screen(available)
        QApplication.processEvents()

        frame = dialog.frameGeometry()
        if frame == dialog.geometry():
            frame = frame.adjusted(0, -SettingsDialog.TITLE_BAR_ALLOWANCE, 0, 0)
        assert available.contains(frame)
        done = dialog.findChild(QWidget, "modelManagerCloseButton")
        assert done.toolTip() == "Close Settings (Esc)"
    finally:
        dialog.close()


def test_title_bar_stylesheet_parses():
    from ui_qt.main_window import CustomTitleBar

    messages = []
    previous = qInstallMessageHandler(lambda _type, _context, text: messages.append(text))
    try:
        host = QWidget()
        layout = QVBoxLayout(host)
        bar = CustomTitleBar(host)
        layout.addWidget(bar)
        host.show()
        QApplication.processEvents()
    finally:
        qInstallMessageHandler(previous)
        host.close()

    assert not [m for m in messages if "Could not parse stylesheet" in m]
    assert "@" not in bar.styleSheet()
