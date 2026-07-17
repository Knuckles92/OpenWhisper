"""Offline technical profile dialog for a local Whisper model."""

from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QDesktopServices, QFont
from PyQt6.QtWidgets import (
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from services.model_catalog import ModelDetails
from ui_qt.widgets import Button, PrimaryButton


_DETAILS_STYLE = """
    QFrame#modelDetailsSection {
        background-color: rgba(44, 44, 46, 0.55);
        border: 1px solid rgba(255, 255, 255, 0.06);
        border-radius: 10px;
    }
    QLabel#modelDetailsTitle {
        color: #f5f5f7;
        background-color: transparent;
        font-size: 20px;
        font-weight: 700;
    }
    QLabel#modelDetailsTags {
        color: #6fb1ff;
        background-color: rgba(10, 132, 255, 0.12);
        border: 1px solid rgba(10, 132, 255, 0.25);
        border-radius: 6px;
        padding: 3px 8px;
        font-size: 10px;
        font-weight: 600;
    }
    QLabel#modelDetailsSectionTitle {
        color: #f5f5f7;
        background-color: transparent;
        border: none;
        font-size: 12px;
        font-weight: 700;
    }
    QLabel#modelDetailsBody,
    QLabel#modelDetailsFactValue {
        color: #d1d1d6;
        background-color: transparent;
        border: none;
        font-size: 11px;
    }
    QLabel#modelDetailsFactLabel {
        color: #8e8e93;
        background-color: transparent;
        border: none;
        font-size: 10px;
        font-weight: 600;
    }
    QLabel#modelDetailsSourceNote {
        color: #636366;
        background-color: transparent;
        border: none;
        font-size: 10px;
    }
"""


class ModelDetailsDialog(QDialog):
    """Show bundled technical and practical details for one model."""

    def __init__(self, model_details: ModelDetails, parent=None):
        """Initialize the model details dialog.

        Args:
            model_details: Immutable bundled metadata to display.
            parent: Owning Model Manager dialog.
        """
        super().__init__(parent)
        self.model_details = model_details
        self.fact_labels = {}

        self.setWindowTitle(f"{model_details.model_name} Model Details")
        self.setModal(True)
        self.setMinimumSize(600, 560)
        self.resize(640, 680)
        self.setStyleSheet(_DETAILS_STYLE)

        self._setup_ui()

    def _setup_ui(self) -> None:
        """Build the scrollable technical profile and source actions."""
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 18, 20, 16)
        outer.setSpacing(12)

        header = QHBoxLayout()
        header.setSpacing(10)
        title = QLabel(self.model_details.model_name)
        title.setObjectName("modelDetailsTitle")
        title.setAccessibleName("Model name")
        header.addWidget(title)
        header.addStretch()
        tags = QLabel(self.model_details.compact_tags)
        tags.setObjectName("modelDetailsTags")
        tags.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header.addWidget(tags)
        outer.addLayout(header)

        scroll = QScrollArea()
        scroll.setObjectName("modelDetailsScrollArea")
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(10)

        content_layout.addWidget(
            self._text_section("Overview", self.model_details.description)
        )
        content_layout.addWidget(self._technical_section())
        content_layout.addWidget(
            self._text_section("Best for", self.model_details.best_for)
        )
        limitations = "\n".join(
            f"\u2022 {item}" for item in self.model_details.limitations
        )
        content_layout.addWidget(
            self._text_section("Tradeoffs and limitations", limitations)
        )

        source_note = QLabel(
            "Technical figures are bundled from the linked upstream model "
            "cards. Speed and memory usage vary with hardware, compute type, "
            "audio, and decoding settings."
        )
        source_note.setObjectName("modelDetailsSourceNote")
        source_note.setWordWrap(True)
        content_layout.addWidget(source_note)
        content_layout.addStretch()

        scroll.setWidget(content)
        outer.addWidget(scroll, stretch=1)

        footer = QHBoxLayout()
        footer.setSpacing(8)
        self.repository_button = PrimaryButton("Open on Hugging Face")
        self.repository_button.setObjectName("modelDetailsRepositoryButton")
        self.repository_button.setToolTip(self.model_details.repository_url)
        self.repository_button.clicked.connect(self._open_repository)
        footer.addWidget(self.repository_button)

        self.origin_button = Button("View Original Model")
        self.origin_button.setObjectName("modelDetailsOriginButton")
        self.origin_button.setToolTip(self.model_details.origin_url)
        self.origin_button.clicked.connect(self._open_origin)
        footer.addWidget(self.origin_button)

        footer.addStretch()
        close_button = Button("Close")
        close_button.setObjectName("modelDetailsCloseButton")
        close_button.clicked.connect(self.accept)
        footer.addWidget(close_button)
        outer.addLayout(footer)

    def _text_section(self, title: str, text: str) -> QFrame:
        """Create a titled, word-wrapped text section."""
        frame = QFrame()
        frame.setObjectName("modelDetailsSection")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(6)

        heading = QLabel(title)
        heading.setObjectName("modelDetailsSectionTitle")
        layout.addWidget(heading)

        body = QLabel(text)
        body.setObjectName("modelDetailsBody")
        body.setWordWrap(True)
        body.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(body)
        return frame

    def _technical_section(self) -> QFrame:
        """Create the model's technical fact grid."""
        frame = QFrame()
        frame.setObjectName("modelDetailsSection")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        heading = QLabel("Technical profile")
        heading.setObjectName("modelDetailsSectionTitle")
        layout.addWidget(heading)

        facts = QGridLayout()
        facts.setContentsMargins(0, 0, 0, 0)
        facts.setHorizontalSpacing(16)
        facts.setVerticalSpacing(8)
        facts.setColumnStretch(1, 1)

        rows = (
            ("Origin", self.model_details.origin_name),
            ("Repository", self.model_details.repository_id),
            ("Maintainer", self.model_details.maintainer),
            ("Family", self.model_details.family),
            ("Languages", self.model_details.language_support),
            ("Tasks", self.model_details.task_support),
            ("Parameters", self.model_details.parameter_count),
            ("Published speed", self.model_details.relative_performance),
            ("Memory guidance", self.model_details.memory_guidance),
            ("Download size", self.model_details.download_size),
            ("Local format", self.model_details.runtime_format),
            ("License", self.model_details.license),
        )
        for row, (caption, value) in enumerate(rows):
            caption_label = QLabel(caption)
            caption_label.setObjectName("modelDetailsFactLabel")
            caption_label.setAlignment(
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
            )
            value_label = QLabel(value)
            value_label.setObjectName("modelDetailsFactValue")
            value_label.setWordWrap(True)
            value_label.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
            )
            value_label.setFont(QFont("Segoe UI", 9))
            facts.addWidget(caption_label, row, 0)
            facts.addWidget(value_label, row, 1)
            self.fact_labels[caption] = value_label

        layout.addLayout(facts)
        return frame

    def _open_repository(self) -> None:
        """Open the faster-whisper conversion repository in the browser."""
        QDesktopServices.openUrl(QUrl(self.model_details.repository_url))

    def _open_origin(self) -> None:
        """Open the original upstream model page in the browser."""
        QDesktopServices.openUrl(QUrl(self.model_details.origin_url))
