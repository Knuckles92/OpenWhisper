"""ElidingComboBox: elided closed state, full-width popup."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from ui_qt.widgets.no_wheel import ElidingComboBox


def test_popup_widens_to_the_longest_item_in_a_narrow_field():
    app = QApplication.instance() or QApplication([])
    combo = ElidingComboBox()
    combo.addItems([
        "gpt-transcribe",
        "gpt-4o-mini-transcribe (retiring Feb 26, 2027)",
    ])
    view = combo.view()
    # The offscreen plugin's item metrics disagree with its font metrics, so
    # measure against Qt's own column hint rather than a pixel constant.
    widest = view.sizeHintForColumn(0)
    combo.resize(widest // 2, 36)
    combo.show()
    app.processEvents()
    try:
        combo.showPopup()
        app.processEvents()
        assert view.minimumWidth() >= widest
        assert view.minimumWidth() > combo.width()
    finally:
        combo.hidePopup()
        combo.close()
