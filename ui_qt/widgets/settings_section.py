"""Keyboard-accessible disclosure used by grouped Settings pages."""
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QTransform
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ui_qt.utils.icons import design_icon
from ui_qt.widgets.eliding_label import ElidingLabel


class SectionHeader(QPushButton):
    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.click()
            event.accept()
            return
        super().keyPressEvent(event)


class SettingsSection(QFrame):
    expanded_changed = pyqtSignal(bool)

    def __init__(self, title: str, summary: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("settingsSection")
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        self.header = SectionHeader()
        self.header.setAutoDefault(False)
        self.header.setDefault(False)
        self.header.setObjectName("settingsSectionHeader")
        self.header.setCheckable(True)
        self.header.setAccessibleName(title)
        self.header.setMinimumHeight(54)
        row = QHBoxLayout(self.header)
        row.setContentsMargins(16, 12, 16, 12)
        row.setSpacing(12)
        self.chevron = QLabel()
        self._chevron_icon = design_icon("chevron-down-gray.svg")
        self.chevron.setPixmap(self._chevron_icon.pixmap(14, 14).transformed(QTransform().rotate(-90)))
        self.chevron.setObjectName("settingsSectionChevron")
        self.chevron.setFixedWidth(18)
        self.title_label = QLabel(title)
        self.title_label.setObjectName("settingsSectionTitle")
        self.summary_label = ElidingLabel(summary)
        self.summary_label.setObjectName("settingsSectionSummary")
        self.summary_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self.chevron)
        row.addWidget(self.title_label)
        row.addWidget(self.summary_label, 1)
        for label in (self.chevron, self.title_label, self.summary_label):
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        column.addWidget(self.header)
        self.body = QWidget()
        self.body.setObjectName("settingsSectionBody")
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(8, 0, 8, 12)
        self.body_layout.setSpacing(10)
        column.addWidget(self.body)
        self.body.hide()
        self.header.toggled.connect(self._toggle)
        self.set_summary(summary)

    def _toggle(self, expanded: bool) -> None:
        pixmap = self._chevron_icon.pixmap(14, 14)
        self.chevron.setPixmap(pixmap if expanded else pixmap.transformed(QTransform().rotate(-90)))
        self.summary_label.setVisible(not expanded)
        self.body.setVisible(expanded)
        self.header.setAccessibleDescription(
            ("Expanded" if expanded else "Collapsed") + ". " + self.summary_label.text()
        )
        self.expanded_changed.emit(expanded)

    def is_expanded(self) -> bool:
        return self.header.isChecked()

    def set_expanded(self, expanded: bool) -> None:
        self.header.setChecked(expanded)

    def set_summary(self, text: str) -> None:
        self.summary_label.setText(text)
        self.header.setToolTip(text)
        self.header.setAccessibleDescription(
            ("Expanded" if self.is_expanded() else "Collapsed") + ". " + text
        )


def expand_settings_ancestors(widget: QWidget) -> None:
    """Reveal a search target, including nested model options."""
    parent = widget.parentWidget()
    sections = []
    while parent is not None:
        if isinstance(parent, SettingsSection):
            sections.append(parent)
        parent = parent.parentWidget()
    for section in reversed(sections):
        section.set_expanded(True)
