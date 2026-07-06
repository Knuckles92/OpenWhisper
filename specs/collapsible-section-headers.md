# Collapsible Section Headers

## Overview

Unified the Transcription and Engine Settings disclosure headers into a shared
`CollapsibleSectionToggle` widget. Both sections default to collapsed on every
launch, use matching clickable text styling, and show descriptive hover tooltips.

## Files Modified

| File | Change |
|------|--------|
| `ui_qt/widgets/collapsible_header.py` | New shared toggle widget |
| `ui_qt/widgets/cards.py` | `HeaderCard` collapsible mode uses shared toggle |
| `ui_qt/widgets/local_engine_controls.py` | Uses shared toggle; defaults collapsed; emits resize delta |
| `ui_qt/widgets/quick_record_tab.py` | Always starts transcription collapsed; forwards engine collapse |
| `ui_qt/widgets/upload_file_tab.py` | Always starts transcription collapsed; forwards engine collapse |
| `ui_qt/main_window.py` | Animates window height for both transcription and engine toggles |
| `ui_qt/widgets/__init__.py` | Export `CollapsibleSectionToggle` |
| `services/settings.py` | Removed unused `TRANSCRIPTION_COLLAPSED` key |

## Shared Toggle

`CollapsibleSectionToggle` renders `{prefix}{title}  {arrow}` as a centered
`QToolButton` with muted purple-gray styling. The entire label is clickable.

Tooltips:
- Engine Settings: "Show model, device, and quantization settings" / "Hide engine settings"
- Transcription: "Show transcription output" / "Hide transcription output"

## Collapse Defaults

Both sections start collapsed on every app launch. The previous
`transcription_collapsed` settings key was removed; collapse state is not persisted.

Default window height is `MAIN_WINDOW_DEFAULT_HEIGHT` (580px) so the collapsed
layout sits snug above the footer. Expanding transcription grows toward
`MAIN_WINDOW_TRANSCRIPTION_EXPAND_HEIGHT` (840px).

## Expand Behavior

### Transcription

`HeaderCard` clears height clamps on expand (`setMinimumHeight(0)`,
`setMaximumHeight(QWIDGETSIZE_MAX)`, `updateGeometry()`) so the transcription
body is not clipped after collapsing. Tab stretch logic (card `stretch=1` when
expanded, bottom spacer `stretch=1` when collapsed) is unchanged.

On toggle, `HeaderCard.toggled(collapsed, delta)` → tab `transcription_collapsed`
→ `MainWindow._on_transcription_collapsed` animates height using
`_collapse_freed_height` tracking.

### Engine Settings

`LocalEngineControls` wraps the combo row and resolved readout in a
`_content_widget`. On toggle it emits `toggled(collapsed, delta)` using the
difference between collapsed and expanded **minimum heights** (via `sizeHint`,
not clipped `height()`), and sets `setMinimumHeight` on itself when expanded so
the parent layout cannot compress the body below its natural size.

Signal path: `LocalEngineControls.toggled` → tab `engine_settings_collapsed`
→ `MainWindow._on_engine_settings_collapsed` animates height using independent
`_engine_collapse_freed_height` tracking so the two sections resize without
compounding errors.

First expand from launch uses the measured `delta` directly (no config constant).

## Verification

1. Launch app — both headers show collapsed (`▸`), bodies hidden.
2. Hover each header — descriptive tooltip appears.
3. Click header text — section expands with full content visible.
4. Click again — section collapses; both transcription and Engine Settings animate window height.
5. Expand/collapse each section independently — heights adjust without overshooting.
6. Switch Quick Record ↔ Upload File — collapse state stays in sync for both sections.
7. Select a non-local model — Engine Settings panel hidden entirely.
