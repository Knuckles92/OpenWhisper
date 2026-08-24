"""Popup profile for one optional Downloads component.

Mirrors the per-model inspector: bundled description, facts, best-for,
tradeoffs, and two outbound links (source and original site). The install
and remove actions stay on the component row.
"""

from typing import Dict

from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from services.component_catalog import ComponentDetails, get_component_details
from services.components import component_coordinator
from services.format_utils import format_size_bytes
from ui_qt.utils.app_icon import app_icon
from ui_qt.widgets import Button
from ui_qt.widgets.wrapped_label import WrappedLabel


class ComponentDetailsDialog(QDialog):
    """Show the bundled profile for one downloadable component."""

    def __init__(self, component_id: str, parent=None):
        super().__init__(parent)
        self.component_id = component_id
        self._details: ComponentDetails = get_component_details(component_id)

        self.setObjectName("componentDetailsDialog")
        self.setWindowTitle(self._details.display_name)
        self.setWindowIcon(app_icon())
        self.setModal(True)
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setMinimumWidth(460)
        self.setMaximumWidth(520)
        self.setMinimumHeight(420)

        self._setup_ui()
        self._render()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 18, 22, 16)
        layout.setSpacing(10)

        eyebrow = QLabel("Component")
        eyebrow.setObjectName("downloadsEyebrow")
        layout.addWidget(eyebrow)

        self.name_label = QLabel(self._details.display_name)
        self.name_label.setObjectName("downloadsInspectorName")
        self.name_label.setWordWrap(True)
        layout.addWidget(self.name_label)

        self.tags_label = QLabel("")
        self.tags_label.setObjectName("downloadsInspectorTags")
        self.tags_label.setVisible(False)
        layout.addWidget(self.tags_label)

        detail_scroll = QScrollArea()
        detail_scroll.setObjectName("downloadsInspectorScroll")
        detail_scroll.setWidgetResizable(True)
        detail_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        detail_scroll.setMinimumHeight(180)

        content = QWidget()
        detail = QVBoxLayout(content)
        detail.setContentsMargins(0, 0, 6, 0)
        detail.setSpacing(12)

        self.description_label = WrappedLabel("")
        self.description_label.setObjectName("downloadsInspectorBody")
        detail.addWidget(self.description_label)

        self.facts = QGridLayout()
        self.facts.setContentsMargins(0, 0, 0, 0)
        self.facts.setHorizontalSpacing(12)
        self.facts.setVerticalSpacing(5)
        self.facts.setColumnStretch(1, 1)
        self.fact_labels: Dict[str, QLabel] = {}
        detail.addLayout(self.facts)

        best_heading = QLabel("Best for")
        best_heading.setObjectName("downloadsSectionTitle")
        self.best_for_label = WrappedLabel("")
        self.best_for_label.setObjectName("downloadsInspectorBody")
        detail.addWidget(best_heading)
        detail.addWidget(self.best_for_label)

        tradeoffs_heading = QLabel("Tradeoffs")
        tradeoffs_heading.setObjectName("downloadsSectionTitle")
        self.tradeoffs_label = WrappedLabel("")
        self.tradeoffs_label.setObjectName("downloadsInspectorBody")
        detail.addWidget(tradeoffs_heading)
        detail.addWidget(self.tradeoffs_label)

        self.source_note = WrappedLabel("")
        self.source_note.setObjectName("downloadsSourceNote")
        detail.addWidget(self.source_note)
        detail.addStretch()

        detail_scroll.setWidget(content)
        layout.addWidget(detail_scroll, stretch=1)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.source_button = Button(self._details.source_label)
        self.source_button.setObjectName("downloadsRepoButton")
        self._compact_button(self.source_button)
        self.source_button.clicked.connect(self._open_source)
        actions.addWidget(self.source_button, stretch=1)

        self.origin_button = Button(self._details.origin_label)
        self.origin_button.setObjectName("downloadsOriginButton")
        self._compact_button(self.origin_button)
        self.origin_button.clicked.connect(self._open_origin)
        actions.addWidget(self.origin_button, stretch=1)

        close_button = Button("Close")
        close_button.setObjectName("downloadsCloseButton")
        self._compact_button(close_button)
        close_button.clicked.connect(self.accept)
        actions.addWidget(close_button)
        layout.addLayout(actions)

    @staticmethod
    def _compact_button(button: Button) -> None:
        button.set_base_minimum_size(0, 34)
        button.ensurePolished()
        height = max(34, button.sizeHint().height())
        button.setMinimumHeight(height)
        button.setMaximumHeight(height)

    def _render(self) -> None:
        details = self._details
        info = component_coordinator.describe(details.component_id)

        self.tags_label.setText(details.compact_tags)
        self.tags_label.setVisible(bool(details.compact_tags))
        self.description_label.setText(details.description)
        self.best_for_label.setText(details.best_for)
        self.tradeoffs_label.setText(
            "\n".join(f"\u2022 {item}" for item in details.limitations)
        )
        self.source_note.setText(details.source_note)
        self.source_button.setToolTip(details.source_url)
        self.origin_button.setToolTip(details.origin_url)

        download = (
            format_size_bytes(info.download_bytes)
            if info.download_bytes
            else "—"
        )
        install = (
            format_size_bytes(info.install_bytes)
            if info.install_bytes
            else "—"
        )
        rows = (
            ("Origin", details.origin_name),
            ("Source", details.source_name),
            ("Maintainer", details.maintainer),
            ("Requires", details.requires),
            ("Payload", details.payload),
            ("Download size", download),
            ("Install size", install),
            ("Local format", details.local_format),
            ("License", details.license),
        )
        for index, (caption, value) in enumerate(rows):
            caption_label = QLabel(caption)
            caption_label.setObjectName("downloadsFactLabel")
            value_label = WrappedLabel(value)
            value_label.setObjectName("downloadsFactValue")
            value_label.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
            )
            self.facts.addWidget(caption_label, index, 0)
            self.facts.addWidget(value_label, index, 1)
            self.fact_labels[caption] = value_label

    def _open_source(self) -> None:
        QDesktopServices.openUrl(QUrl(self._details.source_url))

    def _open_origin(self) -> None:
        QDesktopServices.openUrl(QUrl(self._details.origin_url))
