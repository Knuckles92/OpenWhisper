"""Dialog copy that shows in full when it fits, and scrolls when it does not."""
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QDialog, QFrame, QLabel, QScrollArea, QVBoxLayout, QWidget

#: The tallest the copy grows before it scrolls, at 100% font.
DEFAULT_MAX_HEIGHT = 420
#: Room kept for the window frame and a desktop panel.
_SCREEN_MARGIN = 160


class ScrollingBody(QScrollArea):
    """A frameless, vertical-only scroll area for a dialog's explanation.

    A wrapped ``QLabel`` placed straight in a dialog layout reports a height
    for a guessed width, so long copy got less room than it needed and was
    clipped at both ends. Here the dialog grows to the copy's real height at
    its real width (see :meth:`fit_dialog`), up to ``max_height``, and a
    scrollbar takes over past that, so every line stays readable.
    """

    def __init__(self, max_height: int = DEFAULT_MAX_HEIGHT, parent=None):
        super().__init__(parent)
        self.setObjectName("dialogScrollingBody")
        self._max_height = max_height
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.content = QWidget()
        self.content.setObjectName("dialogScrollingBodyContent")
        self.content_layout = QVBoxLayout(self.content)
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        self.content_layout.setSpacing(12)
        self.setWidget(self.content)
        self.setMinimumHeight(96)

    def add_widget(self, widget: QWidget) -> None:
        self.content_layout.addWidget(widget)

    def _content_height(self, width: int) -> int:
        """The copy's height at ``width``, measured line by line.

        Wrapped labels are asked for their height at that width directly:
        their size hints follow the width they last had, which a dialog
        that has not been shown yet has not given them.
        """
        layout = self.content_layout
        margins = layout.contentsMargins()
        inner = width - margins.left() - margins.right()
        heights = []
        for index in range(layout.count()):
            widget = layout.itemAt(index).widget()
            if widget is None or widget.isHidden():
                continue
            if isinstance(widget, QLabel) and widget.wordWrap():
                heights.append(QLabel.heightForWidth(widget, inner))
            else:
                heights.append(widget.sizeHint().height())
        spacing = layout.spacing() * max(0, len(heights) - 1)
        return sum(heights) + spacing + margins.top() + margins.bottom()

    def fit_dialog(self, dialog: QDialog) -> None:
        """Resize ``dialog`` so the copy shows in full, up to the cap.

        Call before the dialog is first shown, after its width is final.
        """
        layout = dialog.layout()
        layout.activate()
        dialog.adjustSize()
        # Place the children so the copy is measured at the width it gets.
        layout.setGeometry(dialog.rect())
        # The viewport is placed only once the window shows, so measure the
        # area itself: with no frame, it is the copy's width while it fits.
        width = self.width() - 2 * self.frameWidth()
        if width <= 0:
            return
        needed = self._content_height(width)
        cap = self._max_height
        screen = dialog.screen()
        if screen is not None:
            cap = min(cap, screen.availableGeometry().height() - _SCREEN_MARGIN)
        shown = max(self.minimumHeight(), min(needed, cap))
        target = dialog.height() - self.height() + shown
        dialog.resize(dialog.width(), max(target, dialog.minimumSizeHint().height()))
