"""Two-way links between settings keys and the simple Settings controls.

A bound control saves its value through ``persist(key, value)`` the moment it
changes, and :meth:`SettingsBinder.load` puts every bound control back to the
value its settings resolve to. ``persist`` owns the loading guard: the
dialog's ``_persist`` ignores changes made while it is loading, so the
signals :meth:`~SettingsBinder.load` triggers never write anything back.

Only controls whose change does nothing but save belong here. A control that
also restyles the page, asks first, or calls back into the app keeps its own
handler.
"""
from dataclasses import dataclass
from typing import Any, Callable, Dict, List

from PyQt6.QtCore import QEvent, QObject
from PyQt6.QtWidgets import QAbstractButton, QComboBox, QLineEdit, QPlainTextEdit, QSpinBox

Settings = Dict[str, Any]


@dataclass(frozen=True)
class _Binding:
    resolve: Callable[[Settings], Any]
    apply: Callable[[Any], None]


class _FocusOutSaver(QObject):
    """Calls ``save`` whenever its editor, which owns it, loses focus."""

    def __init__(self, edit: QPlainTextEdit, save: Callable[[], None]):
        super().__init__(edit)
        self._save = save
        edit.installEventFilter(self)

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.FocusOut:
            self._save()
        return False


class SettingsBinder:
    """Bind checkboxes, combo boxes, and spin boxes to settings keys.

    Args:
        persist: Saves one key; called as ``persist(key, value)`` on every
            change of a bound control.
    """

    def __init__(self, persist: Callable[[str, Any], Any]):
        self._persist = persist
        self._bindings: List[_Binding] = []

    def checkbox(
        self,
        check: QAbstractButton,
        key: str,
        resolve: Callable[[Settings], Any],
    ) -> None:
        """Save ``bool(checked)`` on toggle; load ``resolve(settings)``."""
        check.toggled.connect(lambda checked: self._persist(key, bool(checked)))
        self._bindings.append(_Binding(resolve, check.setChecked))

    def combo(
        self,
        combo: QComboBox,
        key: str,
        resolve: Callable[[Settings], Any],
    ) -> None:
        """Save the current item's data; load the item whose data matches.

        An unknown value selects the first item.
        """
        combo.currentIndexChanged.connect(
            lambda _index: self._persist(key, combo.currentData())
        )
        self._bindings.append(_Binding(
            resolve,
            lambda value: combo.setCurrentIndex(max(0, combo.findData(value))),
        ))

    def spin(
        self,
        spin: QSpinBox,
        key: str,
        resolve: Callable[[Settings], Any],
    ) -> None:
        """Save ``int(value)`` on every committed value; load ``resolve``."""
        spin.valueChanged.connect(lambda value: self._persist(key, int(value)))
        self._bindings.append(_Binding(resolve, spin.setValue))

    def line_edit(
        self,
        edit: QLineEdit,
        key: str,
        resolve: Callable[[Settings], Any],
    ) -> None:
        """Save the trimmed text once editing finishes; load ``resolve``.

        Not on every keystroke: half-typed text never reaches the file.
        """
        edit.editingFinished.connect(lambda: self._persist(key, edit.text().strip()))
        self._bindings.append(_Binding(resolve, edit.setText))

    def plain_text(
        self,
        edit: QPlainTextEdit,
        key: str,
        resolve: Callable[[Settings], Any],
    ) -> None:
        """Save the trimmed text once the editor loses focus; load ``resolve``.

        Text that only repeats the placeholder, which shows the default,
        saves as "", so the default stays current when it changes.
        """
        def save() -> None:
            text = edit.toPlainText().strip()
            self._persist(key, "" if text == edit.placeholderText().strip() else text)

        _FocusOutSaver(edit, save)
        self._bindings.append(_Binding(resolve, edit.setPlainText))

    @property
    def binding_count(self) -> int:
        """Number of controls registered so far, for lazy page initialization."""
        return len(self._bindings)

    def load(self, settings: Settings, *, start: int = 0) -> None:
        """Set bound controls from ``settings``, beginning at ``start``."""
        for binding in self._bindings[start:]:
            binding.apply(binding.resolve(settings))
