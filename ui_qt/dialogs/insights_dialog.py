"""
Insights Dialog for generating meeting insights via LLM.
Allows users to generate summaries, action items, or custom insights
from meeting transcriptions.
"""
import logging
from datetime import datetime
from typing import Optional, Callable
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QComboBox,
    QTextEdit, QFrame, QApplication, QPlainTextEdit
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QFont

from ui_qt.widgets import PrimaryButton, ModernButton
from services.insights_service import InsightsService, InsightType, InsightEntry


class InsightGenerationWorker(QThread):
    """Worker thread for generating insights without blocking the UI."""
    
    finished = pyqtSignal(str)  # Emits the generated insight
    error = pyqtSignal(str)     # Emits error message
    progress = pyqtSignal(str)  # Emits progress updates
    
    def __init__(
        self, 
        insights_service: InsightsService,
        insight_type: InsightType,
        transcript: str,
        custom_prompt: str = ""
    ):
        super().__init__()
        self.insights_service = insights_service
        self.insight_type = insight_type
        self.transcript = transcript
        self.custom_prompt = custom_prompt
    
    def run(self):
        """Execute insight generation in background thread."""
        try:
            # Set up progress callback
            self.insights_service.on_progress = self._on_progress
            
            if self.insight_type == InsightType.SUMMARY:
                result = self.insights_service.generate_summary(self.transcript)
            elif self.insight_type == InsightType.ACTION_ITEMS:
                result = self.insights_service.generate_action_items(self.transcript)
            else:  # CUSTOM
                result = self.insights_service.generate_custom(
                    self.transcript, 
                    self.custom_prompt
                )
            
            self.finished.emit(result)
            
        except Exception as e:
            self.error.emit(str(e))
        finally:
            self.insights_service.on_progress = None
    
    def _on_progress(self, message: str):
        """Handle progress updates from the service."""
        self.progress.emit(message)


class InsightsDialog(QDialog):
    """Dialog for generating and displaying meeting insights."""

    def __init__(
        self,
        transcript: str,
        meeting_title: str = "",
        meeting_id: Optional[str] = None,
        parent=None
    ):
        """Initialize the insights dialog.

        Args:
            transcript: The meeting transcript text.
            meeting_title: Title of the meeting (for display).
            meeting_id: Meeting ID for persistence (optional).
            parent: Parent widget.
        """
        super().__init__(parent)
        self.logger = logging.getLogger(__name__)
        self.transcript = transcript
        self.meeting_title = meeting_title
        self.meeting_id = meeting_id

        # Track current insight state
        self._current_saved_insight: Optional[InsightEntry] = None
        self._last_custom_prompt: Optional[str] = None

        # Service and worker
        self.insights_service = InsightsService()
        self.worker: Optional[InsightGenerationWorker] = None

        self.setWindowTitle("Generate Meeting Insights")
        self.setMinimumSize(600, 550)
        self.setMaximumSize(900, 800)

        self._setup_ui()
        self._connect_signals()

        # Load saved insight for default type (Summary)
        self._load_saved_insight_for_current_type()
    
    def _setup_ui(self):
        """Setup the user interface."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)
        
        # Header
        header_layout = QVBoxLayout()
        header_layout.setSpacing(4)
        
        title = QLabel("Generate Meeting Insights")
        title_font = QFont("Segoe UI", 16)
        title_font.setBold(True)
        title.setFont(title_font)
        title.setStyleSheet("color: #e0e0ff;")
        header_layout.addWidget(title)
        
        if self.meeting_title:
            meeting_label = QLabel(f"Meeting: {self.meeting_title}")
            meeting_label.setStyleSheet("color: #00d4ff; font-size: 12px;")
            header_layout.addWidget(meeting_label)
        
        layout.addLayout(header_layout)
        
        # Insight type selection
        type_frame = QFrame()
        type_frame.setStyleSheet("""
            QFrame {
                background-color: #252538;
                border: 1px solid #404060;
                border-radius: 8px;
            }
        """)
        type_layout = QVBoxLayout(type_frame)
        type_layout.setContentsMargins(16, 12, 16, 12)
        type_layout.setSpacing(8)
        
        type_label = QLabel("Insight Type:")
        type_label.setStyleSheet("color: #e0e0ff; font-weight: bold;")
        type_layout.addWidget(type_label)
        
        self.type_combo = QComboBox()
        self.type_combo.addItems([
            "Summary - Key points and decisions",
            "Action Items - Tasks and follow-ups",
            "Custom - Your own prompt"
        ])
        self.type_combo.setMinimumHeight(36)
        self.type_combo.setStyleSheet("""
            QComboBox {
                background-color: #2d2d44;
                color: #e0e0ff;
                border: 1px solid #404060;
                border-radius: 6px;
                padding: 8px 12px;
                font-size: 13px;
            }
            QComboBox:hover {
                border: 1px solid #6366f1;
            }
            QComboBox::drop-down {
                border: none;
                padding-right: 12px;
            }
            QComboBox QAbstractItemView {
                background-color: #2d2d44;
                color: #e0e0ff;
                selection-background-color: #6366f1;
                border: 1px solid #404060;
            }
        """)
        type_layout.addWidget(self.type_combo)
        
        layout.addWidget(type_frame)
        
        # Custom prompt input (initially hidden)
        self.custom_frame = QFrame()
        self.custom_frame.setStyleSheet("""
            QFrame {
                background-color: #252538;
                border: 1px solid #404060;
                border-radius: 8px;
            }
        """)
        custom_layout = QVBoxLayout(self.custom_frame)
        custom_layout.setContentsMargins(16, 12, 16, 12)
        custom_layout.setSpacing(8)
        
        custom_label = QLabel("Your Prompt:")
        custom_label.setStyleSheet("color: #e0e0ff; font-weight: bold;")
        custom_layout.addWidget(custom_label)
        
        self.custom_prompt_input = QPlainTextEdit()
        self.custom_prompt_input.setPlaceholderText(
            "Enter your custom prompt here...\n"
            "Examples:\n"
            "- What were the main concerns raised?\n"
            "- Summarize the technical discussions\n"
            "- Who made the most contributions?"
        )
        self.custom_prompt_input.setMaximumHeight(100)
        self.custom_prompt_input.setStyleSheet("""
            QPlainTextEdit {
                background-color: #2d2d44;
                color: #e0e0ff;
                border: 1px solid #404060;
                border-radius: 6px;
                padding: 10px;
                font-size: 12px;
            }
            QPlainTextEdit:focus {
                border: 1px solid #6366f1;
            }
        """)
        custom_layout.addWidget(self.custom_prompt_input)
        
        self.custom_frame.setVisible(False)
        layout.addWidget(self.custom_frame)
        
        # Generate button
        generate_layout = QHBoxLayout()
        generate_layout.addStretch()
        
        self.generate_btn = PrimaryButton("Generate Insights")
        self.generate_btn.setMinimumWidth(180)
        generate_layout.addWidget(self.generate_btn)
        
        generate_layout.addStretch()
        layout.addLayout(generate_layout)
        
        # Status label
        self.status_label = QLabel("")
        self.status_label.setStyleSheet("color: #a0a0c0; font-size: 12px;")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.status_label)
        
        # Results area
        results_frame = QFrame()
        results_frame.setStyleSheet("""
            QFrame {
                background-color: #252538;
                border: 1px solid #404060;
                border-radius: 8px;
            }
        """)
        results_layout = QVBoxLayout(results_frame)
        results_layout.setContentsMargins(16, 12, 16, 12)
        results_layout.setSpacing(8)
        
        results_header = QHBoxLayout()
        results_label = QLabel("Results:")
        results_label.setStyleSheet("color: #e0e0ff; font-weight: bold;")
        results_header.addWidget(results_label)
        
        results_header.addStretch()
        
        self.copy_btn = ModernButton("Copy")
        self.copy_btn.setMaximumWidth(80)
        self.copy_btn.setEnabled(False)
        results_header.addWidget(self.copy_btn)
        
        results_layout.addLayout(results_header)
        
        self.results_text = QTextEdit()
        self.results_text.setReadOnly(True)
        self.results_text.setMinimumHeight(200)
        self.results_text.setPlaceholderText(
            "Generated insights will appear here...\n\n"
            "Select an insight type above and click 'Generate Insights' to begin."
        )
        self.results_text.setStyleSheet("""
            QTextEdit {
                background-color: #2d2d44;
                color: #e0e0ff;
                border: 1px solid #404060;
                border-radius: 6px;
                padding: 12px;
                font-size: 13px;
                line-height: 1.5;
            }
        """)
        results_layout.addWidget(self.results_text)
        
        layout.addWidget(results_frame, stretch=1)
        
        # Bottom button row
        button_layout = QHBoxLayout()
        button_layout.setSpacing(12)
        
        close_btn = ModernButton("Close")
        close_btn.clicked.connect(self.close)
        button_layout.addWidget(close_btn)
        
        button_layout.addStretch()
        
        layout.addLayout(button_layout)
        
        # Apply dialog styling
        self.setStyleSheet("""
            InsightsDialog {
                background-color: #1e1e2e;
            }
        """)
    
    def _connect_signals(self):
        """Connect UI signals."""
        self.type_combo.currentIndexChanged.connect(self._on_type_changed)
        self.generate_btn.clicked.connect(self._on_generate_clicked)
        self.copy_btn.clicked.connect(self._copy_results)
    
    def _on_type_changed(self, index: int):
        """Handle insight type selection change."""
        # Show custom prompt input only for Custom type
        self.custom_frame.setVisible(index == 2)

        # Load saved insight for the new type
        self._load_saved_insight_for_current_type()
    
    def _get_selected_insight_type(self) -> InsightType:
        """Get the currently selected insight type."""
        index = self.type_combo.currentIndex()
        if index == 0:
            return InsightType.SUMMARY
        elif index == 1:
            return InsightType.ACTION_ITEMS
        else:
            return InsightType.CUSTOM

    def _load_saved_insight_for_current_type(self):
        """Load and display saved insight for the current type."""
        if not self.meeting_id:
            return

        insight_type = self._get_selected_insight_type()
        custom_prompt = None

        # For custom type, we need to check if there's a saved insight
        # with an empty prompt (we can't know what prompt the user will enter)
        if insight_type == InsightType.CUSTOM:
            # Don't auto-load for custom type since we don't know the prompt yet
            self._current_saved_insight = None
            self._update_ui_for_saved_state()
            return

        # Try to load saved insight
        saved = self.insights_service.get_saved_insight(
            meeting_id=self.meeting_id,
            insight_type=insight_type,
            custom_prompt=custom_prompt
        )

        self._current_saved_insight = saved
        self._update_ui_for_saved_state()

    def _update_ui_for_saved_state(self):
        """Update UI based on whether we have a saved insight."""
        if self._current_saved_insight:
            # Display saved insight
            self.results_text.setPlainText(self._current_saved_insight.content)
            self.copy_btn.setEnabled(True)

            # Update button text
            self.generate_btn.setText("Regenerate")

            # Show saved timestamp
            try:
                saved_time = self._current_saved_insight.generated_at_datetime
                time_str = saved_time.strftime("%b %d, %Y at %I:%M %p")
                self.status_label.setText(f"Saved on {time_str}")
                self.status_label.setStyleSheet("color: #34d399; font-size: 12px;")
            except Exception:
                self.status_label.setText("Previously saved")
                self.status_label.setStyleSheet("color: #34d399; font-size: 12px;")
        else:
            # No saved insight - clear and reset
            self.results_text.clear()
            self.copy_btn.setEnabled(False)
            self.generate_btn.setText("Generate Insights")
            self.status_label.setText("")

    def _on_generate_clicked(self):
        """Handle generate button click."""
        insight_type = self._get_selected_insight_type()
        custom_prompt = ""

        if insight_type == InsightType.CUSTOM:
            custom_prompt = self.custom_prompt_input.toPlainText().strip()
            if not custom_prompt:
                self.status_label.setText("Please enter a custom prompt.")
                self.status_label.setStyleSheet("color: #ff6b6b; font-size: 12px;")
                return

        # Track the custom prompt for saving later
        self._last_custom_prompt = custom_prompt if custom_prompt else None

        # Disable UI during generation
        self.generate_btn.setEnabled(False)
        self.type_combo.setEnabled(False)
        self.custom_prompt_input.setEnabled(False)
        self.results_text.clear()
        self.copy_btn.setEnabled(False)

        self.status_label.setText("Initializing...")
        self.status_label.setStyleSheet("color: #fbbf24; font-size: 12px;")

        # Start generation in background thread
        self.worker = InsightGenerationWorker(
            self.insights_service,
            insight_type,
            self.transcript,
            custom_prompt
        )
        self.worker.finished.connect(self._on_generation_finished)
        self.worker.error.connect(self._on_generation_error)
        self.worker.progress.connect(self._on_progress)
        self.worker.start()

        self.logger.info(f"Started {insight_type.value} generation")
    
    def _on_progress(self, message: str):
        """Handle progress updates."""
        self.status_label.setText(message)
    
    def _on_generation_finished(self, result: str):
        """Handle successful generation."""
        self.results_text.setPlainText(result)

        # Auto-save if we have a meeting_id
        insight_type = self._get_selected_insight_type()
        if self.meeting_id:
            try:
                self.insights_service.save_insight(
                    meeting_id=self.meeting_id,
                    insight_type=insight_type,
                    content=result,
                    custom_prompt=self._last_custom_prompt
                )
                self.status_label.setText("Saved!")
                self.logger.info(f"Auto-saved {insight_type.value} insight")
            except Exception as e:
                self.logger.error(f"Failed to save insight: {e}")
                self.status_label.setText("Generated (save failed)")
        else:
            self.status_label.setText("Generation complete!")

        self.status_label.setStyleSheet("color: #34d399; font-size: 12px;")

        # Re-enable UI
        self.generate_btn.setEnabled(True)
        self.type_combo.setEnabled(True)
        self.custom_prompt_input.setEnabled(True)
        self.copy_btn.setEnabled(True)

        # Update button text to Regenerate since we now have a saved insight
        self.generate_btn.setText("Regenerate")

        self.worker = None
        self.logger.info("Insight generation completed successfully")
    
    def _on_generation_error(self, error_message: str):
        """Handle generation error."""
        self.status_label.setText(f"Error: {error_message}")
        self.status_label.setStyleSheet("color: #ff6b6b; font-size: 12px;")
        
        # Re-enable UI
        self.generate_btn.setEnabled(True)
        self.type_combo.setEnabled(True)
        self.custom_prompt_input.setEnabled(True)
        
        self.worker = None
        self.logger.error(f"Insight generation failed: {error_message}")
    
    def _copy_results(self):
        """Copy results to clipboard."""
        text = self.results_text.toPlainText()
        if text:
            clipboard = QApplication.clipboard()
            clipboard.setText(text)
            self.status_label.setText("Copied to clipboard!")
            self.status_label.setStyleSheet("color: #34d399; font-size: 12px;")
    
    def closeEvent(self, event):
        """Handle dialog close."""
        # Cancel any running generation
        if self.worker and self.worker.isRunning():
            self.insights_service.cancel()
            self.worker.wait(2000)  # Wait up to 2 seconds
        
        # Cleanup
        self.insights_service.cleanup()
        super().closeEvent(event)
