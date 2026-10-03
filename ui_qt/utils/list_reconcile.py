"""Reuse unchanged bounded-list cards while keeping their display order."""
from copy import deepcopy

from PyQt6.QtCore import QObject, pyqtSignal


class HistoryDelivery(QObject):
    loaded = pyqtSignal(int, str, object, str)


class MeetingDelivery(QObject):
    loaded = pyqtSignal(int, object, str, bool)


def reconcile_cards(layout, records, previous, create, fingerprint):
    current = {}
    ordered = []
    for record in records:
        record_id = str(record.get("id", "")) if isinstance(record, dict) else record.id
        signature = fingerprint(record)
        old = previous.get(record_id)
        if old is not None and old[0] == signature:
            card = old[1]
            if isinstance(record, dict):
                card.meeting = dict(record)
            else:
                card.entry = record
        else:
            card = create(record)
        current[record_id] = (deepcopy(signature), card)
        ordered.append(card)
    wanted = set(ordered)
    for index in range(layout.count() - 1, -1, -1):
        widget = layout.itemAt(index).widget()
        if widget not in wanted:
            layout.takeAt(index)
            if widget is not None:
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()
    for index, card in enumerate(ordered):
        existing = layout.itemAt(index)
        if existing is None or existing.widget() is not card:
            layout.insertWidget(index, card)
    return current
