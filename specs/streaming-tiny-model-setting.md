# Plan: Add "Use tiny.en model for streaming preview" Setting

## Overview
Add a checkbox in the Advanced tab that enables using the lightweight `tiny.en` model for streaming preview while keeping the user's chosen model for final transcription.

**Setting key:** `streaming_tiny_model_enabled` (default: `False`)

---

## Files to Modify

### 1. `ui_qt/dialogs/settings_dialog.py`

#### A. Add checkbox in `_create_advanced_tab()` (after line 515)
Insert after the Whisper Engine section (after "Changes require restarting the whisper engine" label):

```python
# Streaming Preview Model section
layout.addSpacing(16)
separator_streaming = QFrame()
separator_streaming.setFrameShape(QFrame.Shape.HLine)
separator_streaming.setStyleSheet("background-color: #404060;")
layout.addWidget(separator_streaming)

layout.addSpacing(12)
streaming_model_title = QLabel("Streaming Preview Model")
streaming_model_title.setStyleSheet("color: #a0a0c0; font-weight: bold;")
layout.addWidget(streaming_model_title)

self.streaming_tiny_model_check = QCheckBox("Use tiny.en model for streaming preview")
self.streaming_tiny_model_check.setStyleSheet("color: #e0e0ff;")
layout.addWidget(self.streaming_tiny_model_check)

streaming_model_info = QLabel(
    "Uses the fast tiny.en model for real-time preview while keeping\n"
    "your main model for final transcription. Uses additional memory."
)
streaming_model_info.setStyleSheet("color: #808090; font-size: 10px;")
streaming_model_info.setWordWrap(True)
layout.addWidget(streaming_model_info)
```

#### B. Update `_on_streaming_enabled_changed()` (line 605-611)
Add enabling/disabling the new checkbox:
```python
self.streaming_tiny_model_check.setEnabled(streaming_enabled)
```

#### C. Update `_load_settings()` (after line 660)
Load the setting and set checkbox state:
```python
streaming_tiny_enabled = settings.get('streaming_tiny_model_enabled', False)
self.streaming_tiny_model_check.setChecked(streaming_tiny_enabled)
self.streaming_tiny_model_check.setEnabled(streaming_enabled)
```

#### D. Update `_save_settings()` (lines 762-767 and 774-775)
Add to change detection:
```python
old_streaming_tiny = settings.get('streaming_tiny_model_enabled', False)
new_streaming_tiny = self.streaming_tiny_model_check.isChecked()
streaming_settings_changed = (
    old_streaming_enabled != self.streaming_enabled_check.isChecked() or
    old_streaming_paste != self.streaming_paste_check.isChecked() or
    old_streaming_tiny != new_streaming_tiny
)
```

Save the setting:
```python
settings['streaming_tiny_model_enabled'] = self.streaming_tiny_model_check.isChecked()
```

---

### 2. `app_qt.py`

#### A. Add instance variable in `__init__` (around line 181)
```python
self._streaming_backend: Optional[LocalWhisperBackend] = None
```

#### B. Update `_setup_streaming()` (lines 305-328)
- Load `streaming_tiny_model_enabled` setting
- If enabled, create separate `LocalWhisperBackend(model_name='tiny.en')`
- Store in `self._streaming_backend`
- Pass to `StreamingTranscriber` instead of `self.current_backend`

#### C. Update `reconfigure_streaming()` (lines 330-381)
- Add cleanup of `self._streaming_backend` before reconfiguring
- Same logic as `_setup_streaming()` for creating separate backend

#### D. Update `cleanup()` (after line 1107)
Add cleanup for streaming backend:
```python
try:
    if self._streaming_backend:
        self._streaming_backend.cleanup()
        self._streaming_backend = None
except Exception as e:
    logging.debug(f"Error during streaming backend cleanup: {e}")
```

---

## Implementation Notes

1. **Memory Impact**: When enabled, both `tiny.en` (~75MB) and main model (e.g., turbo ~1.5GB) are loaded. The info label warns users.

2. **Checkbox State**: Only enabled when streaming is enabled in General tab. Preserves user preference when streaming is toggled.

3. **No changes needed** to:
   - `StreamingTranscriber` - already accepts any backend
   - `LocalWhisperBackend` - already supports `model_name` parameter
   - `settings.py` - generic load/save handles new keys automatically

---

## Verification

1. **UI Test**: Open Settings > Advanced tab, verify checkbox appears after Whisper Engine section
2. **Enable/Disable**: Toggle streaming in General tab, verify tiny model checkbox enables/disables
3. **Functional Test**:
   - Enable both streaming and tiny model setting
   - Start recording, verify streaming preview works
   - Check logs show "Creating dedicated tiny.en backend for streaming"
   - Stop recording, verify final transcription uses main model
4. **Memory Check**: With tiny model enabled, observe ~75MB additional memory usage
5. **Reconfigure Test**: Change setting while not recording, verify reconfiguration works
