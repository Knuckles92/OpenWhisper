"""Runtime fields populated exclusively from the paired host's capabilities."""
from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import QHBoxLayout, QWidget

from ui_qt.widgets.engine_field import engine_combo, engine_field

_LABELS = {"auto": "Auto", "cpu": "CPU", "cuda": "NVIDIA GPU", "en": "English"}


class RemoteEngineControls(QWidget):
    runtime_selected = pyqtSignal(str, str, dict)
    help_requested = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._state = None
        self._locked = False
        self._pending = False
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        self.device_combo = engine_combo(())
        self.compute_combo = engine_combo(())
        self.language_combo = engine_combo(())
        self._fields = {}
        for key, label, combo, tip in (
            ("device", "Device", self.device_combo, "Choose the hardware on the remote computer. Auto chooses an available device; NVIDIA GPU uses its graphics card; CPU uses its processor."),
            ("compute_type", "Quant", self.compute_combo, "Choose Whisper's precision on the remote computer. Auto chooses a supported precision for its hardware."),
            ("language", "Language", self.language_combo, "Choose the host engine's language for remote transcription. Auto detects a supported language; Moonshine supports English only."),
        ):
            field = engine_field(label, combo, tip + " Changes apply to the host and connected clients.",
                                 [("Open Settings → Remote engine", "remote_engine")], self.help_requested.emit)
            self._fields[key] = field
            layout.addWidget(field, stretch=1)
            combo.activated.connect(lambda index, key=key, combo=combo: self._activate(key, combo.itemData(index)))
        self.set_state(None)

    def set_state(self, state) -> None:
        self._state = state
        self._pending = False
        runtime = getattr(state, "runtime", None) or {}
        engine = getattr(state, "engine", None) or {}
        selected = runtime.get("selected") or {}
        family = engine.get("family") or runtime.get("family")
        self._fields["compute_type"].setVisible(family in (None, "", "local_whisper"))
        self._fields["language"].setVisible(family not in (None, "", "local_whisper"))
        device = selected.get("device") or engine.get("device")
        options = {
            "device": runtime.get("devices") or [],
            "compute_type": (runtime.get("compute_types") or {}).get(device, []),
            "language": runtime.get("languages") or [],
        }
        for key, combo in self._combos():
            value = selected.get(key) or engine.get(key)
            blocked = combo.blockSignals(True)
            combo.clear()
            values = list(options[key])
            if value and value not in values:
                values.append(value)
            for item in values:
                combo.addItem(_LABELS.get(item, item), item)
            if key == "device":
                for dependency in runtime.get("dependencies", []):
                    if dependency.get("installable") and dependency["device"] not in values:
                        combo.addItem(f"{_LABELS.get(dependency['device'], dependency['device'])} · Set up on host",
                                      "setup:" + dependency["device"])
            if not values:
                combo.addItem("Not reported" if engine else "Not connected", None)
            elif value:
                combo.setCurrentIndex(combo.findData(value))
            combo.blockSignals(blocked)
            host = getattr(state, "host", "") or "the host"
            tip = (f"Runtime on {host}. Changes apply to the host and connected clients."
                   if runtime.get("can_configure") else
                   f"Update OpenWhisper on {host} to change its runtime from here."
                   if engine else "Connect to the host to see its runtime choices.")
            if value and value not in options[key] and runtime.get("can_configure"):
                tip += " The saved choice is currently unavailable on the host."
                combo.model().item(combo.findData(value)).setEnabled(False)
            combo.setToolTip(tip)
            editable = combo.count() > 1 or (bool(options[key]) and value not in options[key])
            combo.setProperty("remoteEditable", bool(runtime.get("can_configure") and editable))
        self._sync_enabled()

    def _combos(self):
        return (("device", self.device_combo), ("compute_type", self.compute_combo),
                ("language", self.language_combo))

    def set_locked(self, locked: bool) -> None:
        self._locked = locked
        self._sync_enabled()

    def _sync_enabled(self):
        for _key, combo in self._combos():
            combo.setEnabled(bool(combo.property("remoteEditable")) and not self._locked and not self._pending)

    def _activate(self, key, value):
        runtime = getattr(self._state, "runtime", None) or {}
        combo = dict(self._combos())[key]
        if not combo.isEnabled() or value is None or value == (runtime.get("selected") or {}).get(key):
            return
        if isinstance(value, str) and value.startswith("setup:"):
            self.set_state(self._state)
            self.help_requested.emit("remote_models")
            return
        self._pending = True
        self._sync_enabled()
        self.runtime_selected.emit(runtime["family"], runtime["model"], {key: value})
