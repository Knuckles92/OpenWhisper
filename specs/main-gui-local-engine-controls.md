# Main-GUI Local Engine Controls — Implementation Plan

## Overview

The most useful **Local Whisper** knobs — **Model**, **Device (GPU/CPU)**, and **Compute type (quantization)** — are currently buried in **Settings → Advanced** ([settings_dialog.py:340-380](../ui_qt/dialogs/settings_dialog.py)). This plan surfaces them as a compact, sleek **"Local engine" sub-panel** directly on the main GUI, shown beneath the existing "Transcription Model" card whenever the **Local Whisper** backend is active.

Goals:
- Expose `WHISPER_MODEL`, `WHISPER_DEVICE`, `WHISPER_COMPUTE_TYPE` inline on the main window.
- Auto-apply on change (no extra "Apply" click) by reusing the existing reload hook.
- Keep the reload **off the UI thread** so the window never freezes during model swaps.
- Reuse one widget across **both** the Quick Record and Upload File tabs (mirroring how the model combo is already kept in sync).
- Leave the Advanced tab functional as the canonical full settings; keep both views in sync.

### Selected design decisions (recommended defaults)

| Decision | Choice |
|---|---|
| Which controls | **Model + Device + Quant** (model is the biggest speed/accuracy lever) |
| Apply behavior | **Auto-apply** on change; reload runs on a background thread with status feedback |
| Placement | **Reusable widget** in both Quick Record + Upload File tabs |

> If scope needs trimming later, the Model combo can be dropped to land exactly "Device + Quant", and auto-apply can be swapped for an explicit Apply button (removes the threading requirement).

---

## Why this is low-risk: the reload path already exists

The settings dialog never talks to the backend directly. It writes the three keys to JSON, then fires a callback chain that re-reads them:

```
SettingsDialog._save_settings()            # writes WHISPER_MODEL/DEVICE/COMPUTE_TYPE to JSON
  → settings_changed signal (_whisper_settings_changed flag)
    → UIController.open_settings_dialog → on_whisper_settings_changed()
      → ApplicationController.reload_whisper_model()      # application_controller.py:107
        → local_backend.reload_model()                   # re-reads settings JSON
        → ui_controller.set_device_info(backend.device_info)
```

`LocalWhisperBackend.reload_model()` ([local_backend.py:331](../transcriber/local_backend.py)) reads `WHISPER_MODEL` from settings and `_detect_hardware()` reads `WHISPER_DEVICE` / `WHISPER_COMPUTE_TYPE`. **So a new inline control only needs to: (1) write the key(s) to settings, (2) trigger the same reload hook.** No new backend plumbing.

The hidden `device_info_label` in the Quick Record model card ([quick_record_tab.py:84](../ui_qt/widgets/quick_record_tab.py)) already shows the *resolved* engine (e.g. `base | cuda (float16)`) — we keep it as the "what auto resolved to" readout under the new combos.

---

## UI Design

A compact, collapsible row under the model card. Combos are small and inline; the resolved-engine label sits beneath.

```
┌─ Transcription Model ─────────────────────────┐
│  [  Local Whisper                        ▼ ]  │
│                                               │
│  ⚙ Local engine                          [▾]  │   ← disclosure toggle (remembers state)
│  Model [ turbo ▼ ]  Device [ auto ▼ ]         │
│  Quant [ float16 ▼ ]                          │
│  base · cuda (float16)                        │   ← resolved device_info_label
└───────────────────────────────────────────────┘
```

Behavior:
- **Visible / enabled only when the active backend is `local_whisper`.** For API backends the whole sub-panel hides (mirrors the existing `set_device_info("")` behavior).
- **Disabled while recording or while a reload is in flight** (prevents overlapping reloads).
- Combos show the user's *choice* (which may be `auto`); the small label shows what `auto` actually resolved to after load.
- Device combo is platform-aware: `auto`/`cpu` on macOS, `auto`/`cuda`/`cpu` elsewhere (same rule as the Advanced tab, [settings_dialog.py:359](../ui_qt/dialogs/settings_dialog.py)).

---

## Files to create / modify

### 1. NEW: `ui_qt/widgets/local_engine_controls.py`

A dumb, reusable widget. It reads/writes the three settings keys and emits one signal when any value changes. It does **not** trigger the reload itself — that stays a controller responsibility (single source of truth for threading).

```python
"""Compact inline controls for the local Whisper engine (model / device / quant)."""
import logging
import sys
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QComboBox, QToolButton
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFont

from config import config
from services.settings import SettingsKey, settings_manager

logger = logging.getLogger(__name__)


class LocalEngineControls(QWidget):
    """Inline model/device/quant controls for the local Whisper backend.

    Persists changes to settings and emits ``engine_settings_changed`` so the
    controller can reload the backend. Designed to be instantiated once per tab
    and kept in sync via ``set_values`` (signals blocked during sync).
    """

    engine_settings_changed = pyqtSignal()  # emitted after a user change is persisted

    COMPUTE_CHOICES = ["auto", "float16", "float32", "int8"]

    def __init__(self, parent=None):
        super().__init__(parent)
        self._expanded = True
        self._setup_ui()
        self.load_from_settings()
        self._connect_signals()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(6)

        # Disclosure header
        header = QHBoxLayout()
        self.toggle_btn = QToolButton()
        self.toggle_btn.setText("⚙ Local engine")
        self.toggle_btn.setCheckable(True)
        self.toggle_btn.setChecked(True)
        self.toggle_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.toggle_btn.setStyleSheet("color: #a0a0c0; border: none; font-size: 11px;")
        header.addWidget(self.toggle_btn)
        header.addStretch()
        layout.addLayout(header)

        # Body (collapsible)
        self.body = QWidget()
        body_layout = QHBoxLayout(self.body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(10)

        self.model_combo = self._make_combo(config.WHISPER_MODEL_CHOICES)
        device_choices = ["auto", "cpu"] if sys.platform == "darwin" else ["auto", "cuda", "cpu"]
        self.device_combo = self._make_combo(device_choices)
        self.compute_combo = self._make_combo(self.COMPUTE_CHOICES)

        body_layout.addWidget(self._labeled("Model", self.model_combo))
        body_layout.addWidget(self._labeled("Device", self.device_combo))
        body_layout.addWidget(self._labeled("Quant", self.compute_combo))
        body_layout.addStretch()
        layout.addWidget(self.body)

        # Resolved-engine readout (reuses the look of the old device_info_label)
        self.resolved_label = QLabel("")
        self.resolved_label.setFont(QFont("Segoe UI", 9))
        self.resolved_label.setStyleSheet("color: #8888aa;")
        layout.addWidget(self.resolved_label)

    def _make_combo(self, items):
        combo = QComboBox()
        combo.addItems(items)
        combo.setMinimumHeight(28)
        combo.setFont(QFont("Segoe UI", 10))
        return combo

    def _labeled(self, text, combo):
        w = QWidget()
        col = QVBoxLayout(w)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(2)
        lbl = QLabel(text)
        lbl.setStyleSheet("color: #a0a0c0; font-size: 10px;")
        col.addWidget(lbl)
        col.addWidget(combo)
        return w

    def _connect_signals(self):
        self.model_combo.currentTextChanged.connect(self._on_changed)
        self.device_combo.currentTextChanged.connect(self._on_changed)
        self.compute_combo.currentTextChanged.connect(self._on_changed)
        self.toggle_btn.toggled.connect(self._on_toggle)

    def _on_toggle(self, checked: bool):
        self._expanded = checked
        self.body.setVisible(checked)
        self.resolved_label.setVisible(checked)

    def _on_changed(self, _text: str):
        """Persist the three keys and notify listeners."""
        settings = settings_manager.load_all_settings()
        settings[SettingsKey.WHISPER_MODEL] = self.model_combo.currentText()
        settings[SettingsKey.WHISPER_DEVICE] = self.device_combo.currentText()
        settings[SettingsKey.WHISPER_COMPUTE_TYPE] = self.compute_combo.currentText()
        settings_manager.save_all_settings(settings)
        self.engine_settings_changed.emit()

    # ---- Public API ----

    def load_from_settings(self):
        settings = settings_manager.load_all_settings()
        self._select(self.model_combo, settings.get(SettingsKey.WHISPER_MODEL, config.DEFAULT_WHISPER_MODEL))
        self._select(self.device_combo, settings.get(SettingsKey.WHISPER_DEVICE, "auto"), fallback="auto")
        self._select(self.compute_combo, settings.get(SettingsKey.WHISPER_COMPUTE_TYPE, "auto"))

    def set_values(self, model: str, device: str, compute: str):
        """Reflect values without emitting (used to mirror the other tab)."""
        for combo, val, fb in (
            (self.model_combo, model, None),
            (self.device_combo, device, "auto"),
            (self.compute_combo, compute, None),
        ):
            combo.blockSignals(True)
            self._select(combo, val, fallback=fb)
            combo.blockSignals(False)

    def current_values(self):
        return (
            self.model_combo.currentText(),
            self.device_combo.currentText(),
            self.compute_combo.currentText(),
        )

    def set_resolved_info(self, info: str):
        self.resolved_label.setText(info)

    def set_busy(self, busy: bool):
        """Disable combos during reload / recording."""
        for c in (self.model_combo, self.device_combo, self.compute_combo):
            c.setEnabled(not busy)

    def _select(self, combo: QComboBox, value: str, fallback: str = None):
        idx = combo.findText(value)
        if idx < 0 and fallback is not None:
            idx = combo.findText(fallback)
        if idx >= 0:
            combo.setCurrentIndex(idx)
```

Register it in [ui_qt/widgets/__init__.py](../ui_qt/widgets/__init__.py) (import + `__all__`).

### 2. `ui_qt/widgets/quick_record_tab.py` — embed the widget

In `_setup_ui`, after the existing `device_info_label` block, add the panel into the model card and expose a re-emit signal. The old `device_info_label` can be removed (its job moves to `LocalEngineControls.resolved_label`) **or** kept hidden for backward compat — recommend removing to avoid two labels.

```python
# signals (top of class)
engine_settings_changed = pyqtSignal()

# in _setup_ui, inside the model_card after model_combo:
from ui_qt.widgets.local_engine_controls import LocalEngineControls
self.local_engine = LocalEngineControls()
model_card.layout.addWidget(self.local_engine)

# in _connect_signals:
self.local_engine.engine_settings_changed.connect(self.engine_settings_changed)

# update set_device_info to forward to the new label:
def set_device_info(self, device_info: str):
    self.local_engine.set_resolved_info(device_info)

# new helper to show/hide the whole panel based on backend:
def set_local_engine_visible(self, visible: bool):
    self.local_engine.setVisible(visible)
```

Do the same in [ui_qt/widgets/upload_file_tab.py](../ui_qt/widgets/upload_file_tab.py) (it already mirrors the model combo and `set_device_info`).

### 3. `ui_qt/main_window.py` — route + sync both tabs

```python
# new signal
whisper_engine_changed = pyqtSignal()

# in _setup_ui, after connecting the tab model signals:
self.quick_record_tab.engine_settings_changed.connect(self._on_engine_settings_changed)
self.upload_file_tab.engine_settings_changed.connect(self._on_engine_settings_changed)

def _on_engine_settings_changed(self):
    """One tab changed an engine combo: mirror to the other tab, then notify."""
    model, device, compute = self.sender().local_engine.current_values() \
        if hasattr(self.sender(), "local_engine") else (None, None, None)
    # Simpler: read from whichever tab emitted, then push to both.
    src = self.quick_record_tab if self.sender() is self.quick_record_tab else self.upload_file_tab
    values = src.local_engine.current_values()
    for tab in (self.quick_record_tab, self.upload_file_tab):
        if tab is not src:
            tab.local_engine.set_values(*values)
    self.whisper_engine_changed.emit()
```

Also extend `_on_model_changed` (backend switch) to toggle panel visibility on both tabs:

```python
def _on_model_changed(self, model_name: str):
    ...  # existing sync logic
    is_local = config.MODEL_VALUE_MAP.get(model_name) == "local_whisper"
    self.quick_record_tab.set_local_engine_visible(is_local)
    self.upload_file_tab.set_local_engine_visible(is_local)
    self.model_changed.emit(model_name)
```

And on startup, `_load_saved_settings` should set initial visibility based on the saved backend.

### 4. `ui_qt/ui_controller.py` — connect the new signal + busy state

```python
# in _setup_connections:
self.main_window.whisper_engine_changed.connect(self._on_whisper_engine_changed)

def _on_whisper_engine_changed(self):
    if self.on_whisper_settings_changed:
        self.on_whisper_settings_changed()   # reuses the existing reload hook

# make set_device_info also clear/set busy as needed; add a helper:
def set_engine_busy(self, busy: bool):
    self.main_window.quick_record_tab.local_engine.set_busy(busy)
    self.main_window.upload_file_tab.local_engine.set_busy(busy)
```

After a SettingsDialog save that changed whisper settings, also refresh the inline widgets so the two views never diverge — in `open_settings_dialog`'s `on_settings_changed`:

```python
if settings.get('_whisper_settings_changed', False):
    if self.on_whisper_settings_changed:
        self.on_whisper_settings_changed()
    # keep inline controls in sync with the dialog's new values
    self.main_window.quick_record_tab.local_engine.load_from_settings()
    self.main_window.upload_file_tab.local_engine.load_from_settings()
```

### 5. `services/application_controller.py` — reload off the UI thread

This is the only behavioral change. Today `reload_whisper_model()` runs the ~1s+ `cleanup()` + model load synchronously on the UI thread. Move it onto the existing `self.executor` and report back via signals (thread-safe), with a debounce + in-flight guard to coalesce rapid combo changes.

```python
# add signals near the other pyqtSignals:
device_info_update = pyqtSignal(str)
engine_busy_changed = pyqtSignal(bool)

# in __init__: a debounce timer + guard
self._reload_in_flight = False
self._reload_timer = QTimer(self)          # from PyQt6.QtCore import QTimer
self._reload_timer.setSingleShot(True)
self._reload_timer.timeout.connect(self._do_reload_whisper_model)

# connect new signals in _connect_signals():
self.device_info_update.connect(self.ui_controller.set_device_info)
self.engine_busy_changed.connect(self.ui_controller.set_engine_busy)

def reload_whisper_model(self) -> None:
    """Debounced entry point — coalesces rapid combo changes."""
    if self.recorder.is_recording or (self.current_backend and self.current_backend.is_transcribing):
        self.ui_controller.set_status("Finish recording before changing the engine")
        # re-sync combos back to the live settings so UI matches reality
        self.engine_busy_changed.emit(False)
        return
    self._reload_timer.start(400)  # coalesce bursts of changes

def _do_reload_whisper_model(self) -> None:
    if self._reload_in_flight:
        self._reload_timer.start(400)  # retry shortly
        return
    self._reload_in_flight = True
    self.engine_busy_changed.emit(True)
    self.status_update.emit("Reloading whisper engine…")
    self.executor.submit(self._reload_worker)

def _reload_worker(self) -> None:
    try:
        local_backend = self.transcription_backends.get("local_whisper")
        if local_backend:
            local_backend.reload_model()
            info = getattr(local_backend, "device_info", "")
            self.device_info_update.emit(info)
            self.status_update.emit("Whisper engine ready")
            logger.info(f"Whisper reloaded: {info}")
        else:
            self.status_update.emit("Ready")
    except Exception as exc:
        logger.error(f"Whisper reload failed: {exc}")
        self.status_update.emit("Engine reload failed")
    finally:
        self._reload_in_flight = False
        self.engine_busy_changed.emit(False)
```

> Note: `set_device_info` / `set_status` must only be touched on the UI thread. Emitting `device_info_update` / `status_update` / `engine_busy_changed` from the worker is safe because Qt queues cross-thread signal deliveries to the main thread. Do **not** call `ui_controller.set_*` directly from `_reload_worker`.

---

## Edge cases & decisions

1. **Recording / transcribing in progress** — `reload_whisper_model` refuses and tells the user; combos are re-enabled and re-synced to live settings. (Combos are also disabled during recording via `set_busy`, so this is a backstop.)
2. **Rapid changes** (flip device then quant) — 400 ms debounce coalesces into one reload; the in-flight guard prevents overlap.
3. **API backend selected** — sub-panel hidden; no reload triggered (changing API backends already calls `set_device_info("")`).
4. **`auto` values** — combos persist `auto`; the resolved label shows the real resolution after load (e.g. `turbo | cuda (float16)`), so users understand what auto picked.
5. **Two-way sync with Advanced tab** — inline widget reloads from settings after a dialog save; the dialog already reloads from settings each time it opens. Both write the same three JSON keys.
6. **Unsupported compute type on hardware** — already handled in the backend via `_select_best_compute_type` fallback ([local_backend.py:65](../transcriber/local_backend.py)); the resolved label will show the actual type used, which may differ from the chosen one. Consider a follow-up tooltip noting "falls back if unsupported".
7. **Startup visibility** — set panel visibility from the saved backend in `_load_saved_settings`, and push initial `device_info` for local_whisper (the runtime already does this on model change at [transcription.py:361](../services/runtime/transcription.py)).

---

## Verification steps

Always activate the venv first: `.\venv\Scripts\activate`

1. **Launch** `python app_qt.py` with **Local Whisper** selected → the "Local engine" panel appears under the model card with Model/Device/Quant populated from settings and the resolved label showing the live engine.
2. **Switch backend** to "API: Whisper" → panel hides; switch back → panel returns.
3. **Change Quant** from the panel → status shows "Reloading whisper engine…", window stays responsive (no freeze), then "Whisper engine ready" and the resolved label updates. Confirm in `openwhisper.log` that `reload_model` ran with the new compute type.
4. **Change Device to cpu** (on a CUDA machine) → resolved label flips to `... | cpu (int8)`; transcribe a short clip to confirm it still works.
5. **Two-tab sync** — change Model on Quick Record, switch to Upload File → its panel reflects the same value.
6. **Dialog sync** — open Settings → Advanced, change Model, Save → inline panel reflects the new model without restart.
7. **Guard** — start recording, then try changing a combo → combos are disabled; the change is blocked with a status hint.
8. **Rapid changes** — quickly change Device then Quant → only one reload occurs (check log), no overlap/crash.
9. **Tests** — `python -m pytest tests/test_application_controller.py tests/test_settings.py` still pass; add a unit test that `reload_whisper_model` submits to the executor and emits `device_info_update` (mock the backend).

---

## Out of scope (possible follow-ups)

- Persisting the disclosure expanded/collapsed state to settings.
- A "falls back if unsupported" tooltip on the Quant combo.
- Surfacing VAD toggle / beam size (the "power user" option).
- Removing the three controls from the Advanced tab entirely (currently kept for parity).
