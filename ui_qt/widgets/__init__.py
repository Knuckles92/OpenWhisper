"""
PyQt6 custom widgets package.
"""
from ui_qt.widgets.buttons import (
    Button,
    HotkeyHintFilter,
    PrimaryButton,
    DangerButton,
    SplitButton,
    SuccessButton,
    WarningButton,
    IconButton,
)
from ui_qt.widgets.cards import (
    Card,
    ControlPanel,
    HeaderCard,
    StatCard,
)
from ui_qt.widgets.hotkey_display import HotkeyDisplay
from ui_qt.widgets.history_sidebar import (
    HistorySidebar,
    HistoryEdgeTab,
    HistoryItemWidget,
)
from ui_qt.widgets.stats_display import TranscriptionStatsWidget
from ui_qt.widgets.local_engine_controls import LocalEngineControls
from ui_qt.widgets.model_row_widget import ModelRowWidget
from ui_qt.widgets.collapsible_header import CollapsibleSectionToggle
from ui_qt.widgets.tabbed_content import TabbedContentWidget
from ui_qt.widgets.transcription_tab_base import TranscriptionTabBase
from ui_qt.widgets.quick_record_tab import QuickRecordTab
from ui_qt.widgets.upload_file_tab import UploadFileTab
from ui_qt.widgets.compact_record_controller import CompactRecordController
from ui_qt.widgets.no_wheel import NoWheelComboBox, NoWheelSpinBox

__all__ = [
    "Button",
    "HotkeyHintFilter",
    "PrimaryButton",
    "DangerButton",
    "SplitButton",
    "SuccessButton",
    "WarningButton",
    "IconButton",
    "Card",
    "ControlPanel",
    "HeaderCard",
    "StatCard",
    "HotkeyDisplay",
    "HistorySidebar",
    "HistoryEdgeTab",
    "HistoryItemWidget",
    "TranscriptionStatsWidget",
    "CollapsibleSectionToggle",
    "LocalEngineControls",
    "ModelRowWidget",
    "TabbedContentWidget",
    "TranscriptionTabBase",
    "QuickRecordTab",
    "UploadFileTab",
    "CompactRecordController",
    "NoWheelComboBox",
    "NoWheelSpinBox",
]
