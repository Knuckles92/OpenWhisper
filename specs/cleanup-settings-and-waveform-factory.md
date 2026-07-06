# Cleanup: Settings Bloat and Waveform Style Factory

## Overview

Two localized bloat hot-spots identified in the codebase. Both are about removing
ceremony, not changing behavior.

1. **`services/settings.py` (534 LOC)** — ~30 hand-rolled `save_X` / `load_X`
   wrappers, six of which have zero callers. Per-key methods that just forward to
   `load_all_settings` / `save_all_settings` add no value over a generic
   `get(key, default)` accessor.
2. **`ui_qt/waveform_styles/style_factory.py` (65 LOC)** — a registry + factory +
   `register_style()` for a single concrete `ParticleStyle`. Textbook premature
   abstraction; the only caller is `waveform_overlay.py`, which can construct
   `ParticleStyle` directly.

Goal: cut roughly 250 LOC across `services/settings.py` + `ui_qt/waveform_styles/`,
remove unused indirection, and leave behavior unchanged.

## Settings refactor

### Methods with no external callers — DELETE

Verified via repo-wide grep for `settings_manager.<name>(`:

| Method                         | External callers |
| ------------------------------ | ---------------- |
| `save_waveform_style_settings` | 0 (only used internally by `save_style_config`, also dead) |
| `get_style_config`             | 0 |
| `save_style_config`            | 0 |
| `save_audio_input_device`      | 0 |
| `load_streaming_settings`      | 0 |
| `save_streaming_settings`      | 0 |

These six methods total ~190 LOC and are pure dead weight. Removing them is safe.

### Trivial 1-caller wrappers — INLINE

Each of these forwards a tiny dict-build / dict-read to `load_all_settings` /
`save_all_settings`. Inlining them at the single callsite, via a generic
`get(key, default)` and the existing `save_setting(key, value)`, removes a layer
of indirection without losing any logic.

| Method                              | Callsite |
| ----------------------------------- | -------- |
| `load_window_geometry`              | `ui_qt/main_window.py:_restore_window_geometry` |
| `save_window_geometry`              | `ui_qt/main_window.py:_save_window_geometry` |
| `load_streaming_overlay_position`   | `ui_qt/overlays/streaming_text_overlay.py:_restore_position` |
| `save_streaming_overlay_position`   | `ui_qt/overlays/streaming_text_overlay.py:_save_position` |

### Methods with real domain logic — KEEP

These have validation, default-merging, or multi-caller usage that justifies the
wrapper:

- `load_hotkey_settings` / `save_hotkey_settings` — defaults from `config.DEFAULT_HOTKEYS`, 3+2 callers (incl. tests).
- `load_model_selection` / `save_model_selection` — validates against `config.MODEL_VALUE_MAP`.
- `load_waveform_style_settings` — non-trivial merge of saved configs over `config.WAVEFORM_STYLE_CONFIGS` defaults.
- `load_audio_input_device` — small `int` type-check; one caller, but worth keeping for clarity.
- `load_all_settings` / `save_all_settings` / `save_setting` — generic primitives.

### New: generic `get(key, default=None)`

Single accessor that reads the JSON file once and returns one value with a
default. Replaces the trivial per-key load helpers above.

```python
def get(self, key: str, default: Any = None) -> Any:
    return self.load_all_settings().get(key, default)
```

### `SettingsKey`

Leaving `SettingsKey` intact — every constant is referenced somewhere via
`settings.get(SettingsKey.X, default)`.

### Estimated impact

`services/settings.py`: 534 → ~270 LOC.

## Waveform factory removal

### Current

```
ui_qt/waveform_styles/
  __init__.py        (8 LOC)
  base_style.py      (237 LOC)  — abstract base + shared draw helpers
  style_factory.py   (65 LOC)   — registry, create_style, register_style, get_available_styles
  particle_style.py  (461 LOC)  — only concrete style
```

`style_factory.create_style()` is called twice in `ui_qt/overlays/waveform_overlay.py`
(once for the configured style, once for a fallback). `register_style()` and
`get_available_styles()` have zero callers.

### Refactor

- **Delete `style_factory.py`** entirely.
- **Update `waveform_overlay.py`** to instantiate `ParticleStyle` directly.
  The "configurable style" path collapses to a single `ParticleStyle(...)` call,
  since `particle` is the only registered style and the settings file's
  `current_waveform_style` value is validated against
  `config.WAVEFORM_STYLE_CONFIGS` inside `load_waveform_style_settings`.

### Keep

- `BaseWaveformStyle` — provides shared state (audio levels, animation time,
  cancel progress) and shared draw helpers (`draw_canceling_state`,
  `draw_stt_enable_state`, `draw_stt_disable_state`). Real shared code, not
  ceremony. The `@abstractmethod` decorators document the polymorphic seam for
  any future style. Not bloat.

### Estimated impact

`ui_qt/waveform_styles/`: -65 LOC, one fewer import indirection.

## Files modified

- `services/settings.py` — remove 6 dead methods, add `get()`, drop the 4 trivial geometry/position helpers.
- `ui_qt/main_window.py` — inline `_save_window_geometry` / `_restore_window_geometry` against the generic settings API.
- `ui_qt/overlays/streaming_text_overlay.py` — inline `_save_position` / `_restore_position` against the generic settings API.
- `ui_qt/overlays/waveform_overlay.py` — replace `style_factory.create_style(...)` with direct `ParticleStyle(...)`, drop the `style_factory` import.
- `ui_qt/waveform_styles/style_factory.py` — **delete**.
- `ui_qt/waveform_styles/__init__.py` — already exports `BaseWaveformStyle` and `ParticleStyle`; no change needed.

## Verification

1. Static: every removed method must have **zero** call sites in the repo (already verified by grep).
2. Static: every modified file must import cleanly (`python -c "import <module>"` from the venv).
3. Tests: `python -m pytest tests/test_settings.py` — only touches `load_hotkey_settings`, `save_hotkey_settings`, `load_all_settings`, `save_all_settings`, all preserved.
4. Smoke: launch `python app_qt.py`, confirm waveform overlay opens, window geometry saves/restores, streaming overlay position saves/restores.

## Out of scope

- Streaming overlay's `_save_position` / `_restore_position` keep their position-validation logic (`_validate_position`); only the settings persistence call gets inlined.
- `BaseWaveformStyle` is left untouched — it's not the bloat.
- The `Runtime` suffix layer (`services/runtime/`) and the size of `main_window.py` are separate concerns called out elsewhere in the bloat review; not addressed here.
