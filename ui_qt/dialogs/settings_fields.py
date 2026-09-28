"""Small layout pieces the Settings pages share: fields, captions, titles."""
from PyQt6.QtWidgets import QLabel, QVBoxLayout, QWidget

from ui_qt.widgets.wrapped_label import WrappedLabel


def settings_field(label: str, widget: QWidget) -> QWidget:
    """Wrap a control with its field label above it."""
    wrapper = QWidget()
    wrapper.setObjectName("modelManagerFieldGroup")
    col = QVBoxLayout(wrapper)
    col.setContentsMargins(0, 0, 0, 0)
    col.setSpacing(5)
    caption = QLabel(label)
    caption.setObjectName("textModelFieldLabel")
    col.addWidget(caption)
    col.addWidget(widget)
    return wrapper


def settings_caption(text: str) -> WrappedLabel:
    """A wrapped secondary line under a control or group title."""
    label = WrappedLabel(text)
    label.setObjectName("infoLabel")
    return label


def group_title(layout: QVBoxLayout, text: str) -> QLabel:
    """Add an uppercase group eyebrow to ``layout`` and return it."""
    # Qt stylesheets have no text-transform, so the eyebrow case is set here.
    caption = QLabel(text.upper())
    caption.setObjectName("settingsTileGroupTitle")
    layout.addWidget(caption)
    return caption
