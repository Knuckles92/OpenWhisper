"""
Audio Upload Preview Dialog for PyQt6 UI.
Shows file analysis and chunk preview before transcription.
"""
import logging
from typing import Optional, Callable
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, 
    QFrame, QScrollArea, QWidget
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont

from services.audio_processor import AudioFilePreview
from ui_qt.widgets import PrimaryButton, ModernButton


class ChunkPreviewItem(QFrame):
    """A styled item showing chunk information."""
    
    def __init__(self, chunk_number: int, duration: float, parent=None):
        """Initialize chunk preview item.
        
        Args:
            chunk_number: The chunk number (1-indexed).
            duration: Duration of the chunk in seconds.
            parent: Parent widget.
        """
        super().__init__(parent)
        self.setObjectName("chunkPreviewItem")
        self.setStyleSheet("""
            QFrame#chunkPreviewItem {
                background-color: #2d2d44;
                border-radius: 6px;
                padding: 8px;
                margin: 2px 0;
            }
        """)
        
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(12)
        
        # Chunk number badge
        chunk_badge = QLabel(f"#{chunk_number}")
        chunk_badge.setStyleSheet("""
            background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                                        stop:0 #6366f1, stop:1 #8b5cf6);
            color: white;
            border-radius: 10px;
            padding: 4px 10px;
            font-weight: bold;
        """)
        chunk_badge.setFixedWidth(45)
        chunk_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(chunk_badge)
        
        # Duration label
        minutes = int(duration // 60)
        seconds = int(duration % 60)
        if minutes > 0:
            duration_text = f"{minutes}m {seconds}s"
        else:
            duration_text = f"{seconds}s"
        
        duration_label = QLabel(f"Duration: {duration_text}")
        duration_label.setStyleSheet("color: #e0e0ff; font-size: 13px;")
        layout.addWidget(duration_label)
        
        layout.addStretch()


class UploadPreviewDialog(QDialog):
    """Dialog showing audio file preview with chunk breakdown before transcription."""
    
    def __init__(self, preview: AudioFilePreview, parent=None):
        """Initialize upload preview dialog.
        
        Args:
            preview: AudioFilePreview object with file analysis.
            parent: Parent widget.
        """
        super().__init__(parent)
        self.logger = logging.getLogger(__name__)
        self.preview = preview
        self.setWindowTitle("Audio File Preview")
        self.setMinimumSize(450, 350)
        self.setMaximumSize(600, 600)
        
        # Result callback
        self.on_proceed: Optional[Callable] = None
        
        self._setup_ui()
    
    def _setup_ui(self):
        """Setup the user interface."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)
        
        # Title
        title = QLabel("Audio File Analysis")
        title_font = QFont("Segoe UI", 14)
        title_font.setBold(True)
        title.setFont(title_font)
        title.setStyleSheet("color: #e0e0ff;")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)
        
        # File info card
        info_card = QFrame()
        info_card.setObjectName("infoCard")
        info_card.setStyleSheet("""
            QFrame#infoCard {
                background-color: #252538;
                border: 1px solid #404060;
                border-radius: 8px;
            }
        """)
        info_layout = QVBoxLayout(info_card)
        info_layout.setContentsMargins(16, 16, 16, 16)
        info_layout.setSpacing(10)
        
        # File name
        filename_label = QLabel(f"File: {self.preview.file_name}")
        filename_label.setStyleSheet("color: #00d4ff; font-size: 13px; font-weight: bold;")
        filename_label.setWordWrap(True)
        info_layout.addWidget(filename_label)
        
        # File size
        size_label = QLabel(f"Size: {self.preview.file_size_formatted}")
        size_label.setStyleSheet("color: #e0e0ff; font-size: 12px;")
        info_layout.addWidget(size_label)
        
        # Duration
        duration_label = QLabel(f"Duration: {self.preview.duration_formatted}")
        duration_label.setStyleSheet("color: #e0e0ff; font-size: 12px;")
        info_layout.addWidget(duration_label)
        
        # Sample rate and channels
        audio_info = QLabel(f"Audio: {self.preview.sample_rate} Hz, {'Stereo' if self.preview.channels == 2 else 'Mono'}")
        audio_info.setStyleSheet("color: #a0a0c0; font-size: 11px;")
        info_layout.addWidget(audio_info)
        
        layout.addWidget(info_card)
        
        # Chunking info section
        if self.preview.needs_splitting:
            chunk_header = QLabel(f"Will be split into {self.preview.estimated_chunks} chunks")
            chunk_header.setStyleSheet("color: #fbbf24; font-size: 13px; font-weight: bold;")
            chunk_header.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(chunk_header)
            
            explanation = QLabel("Large files are split at silence points for optimal transcription")
            explanation.setStyleSheet("color: #a0a0c0; font-size: 11px; font-style: italic;")
            explanation.setAlignment(Qt.AlignmentFlag.AlignCenter)
            explanation.setWordWrap(True)
            layout.addWidget(explanation)
            
            # Chunk list in scrollable area
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setMaximumHeight(180)
            scroll.setStyleSheet("""
                QScrollArea {
                    background-color: transparent;
                    border: none;
                }
                QScrollBar:vertical {
                    background-color: #1e1e2e;
                    width: 8px;
                    border-radius: 4px;
                }
                QScrollBar::handle:vertical {
                    background-color: #404060;
                    border-radius: 4px;
                }
                QScrollBar::handle:vertical:hover {
                    background-color: #6366f1;
                }
            """)
            
            chunk_container = QWidget()
            chunk_layout = QVBoxLayout(chunk_container)
            chunk_layout.setContentsMargins(0, 0, 0, 0)
            chunk_layout.setSpacing(4)
            
            for i, duration in enumerate(self.preview.chunk_durations):
                chunk_item = ChunkPreviewItem(i + 1, duration)
                chunk_layout.addWidget(chunk_item)
            
            chunk_layout.addStretch()
            scroll.setWidget(chunk_container)
            layout.addWidget(scroll)
        else:
            # Single chunk message
            single_label = QLabel("File will be transcribed in one pass")
            single_label.setStyleSheet("color: #34d399; font-size: 13px; font-weight: bold;")
            single_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(single_label)
        
        layout.addStretch()
        
        # Button layout
        button_layout = QHBoxLayout()
        button_layout.setSpacing(12)
        
        cancel_btn = ModernButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        button_layout.addWidget(cancel_btn)
        
        button_layout.addStretch()
        
        proceed_btn = PrimaryButton("Transcribe")
        proceed_btn.setMinimumWidth(120)
        proceed_btn.clicked.connect(self._on_proceed)
        button_layout.addWidget(proceed_btn)
        
        layout.addLayout(button_layout)
        
        # Apply dialog styling
        self.setStyleSheet("""
            UploadPreviewDialog {
                background-color: #1e1e2e;
            }
        """)
    
    def _on_proceed(self):
        """Handle proceed button click."""
        self.logger.info(f"Proceeding with transcription: {self.preview.file_path}")
        if self.on_proceed:
            self.on_proceed(self.preview.file_path)
        self.accept()

