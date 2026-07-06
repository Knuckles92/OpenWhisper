# History Sidebar Redesign

## Overview

The history sidebar's expand/collapse animation is janky, especially on first
open. Root causes:

1. **Two unsynchronized animations** — `HistorySidebar` animates its own
   min/max width (250ms, OutCubic) while `MainWindow._resize_for_sidebar()`
   separately animates window geometry (300ms, InOutCubic). The mismatch makes
   the main content area (stretch=1) wobble during every toggle.
2. **Layout-constraint animation is expensive** — animating
   `minimumWidth`/`maximumWidth` re-runs the whole window layout each frame,
   re-wrapping every word-wrapped history label. The
   `_lock_quick_record_layout()` hack only partially masked this.
3. **First-open pop-in** — content population is deferred until after the
   first expand animation finishes (`_refresh_pending`), so the first open
   animates an empty shell and then all items pop in at once.

## Design

### Single animation clock (lockstep window resize)

- `HistorySidebar` keeps one `QPropertyAnimation` on `sidebarWidth`, now using
  the shared `SECTION_COLLAPSE_DURATION_MS` / `SECTION_COLLAPSE_EASING`
  constants so it matches every other collapse animation in the app.
- Each frame, `_set_sidebar_width()` emits a new `width_animated(int)` signal.
- `MainWindow` no longer runs its own geometry animation for the sidebar.
  `toggle_history()` captures `_sidebar_base_width = window width - current
  sidebar width`, and a `_on_sidebar_width_animated(w)` slot sets the window
  width to `base + w` each frame. Main-area width is therefore constant on
  every frame — no wobble, near-zero relayout cost.
- `_resize_for_sidebar()` is deleted. `_animate_resize()` remains for the
  section-collapse height animations only.

### Clip, don't re-layout (fixed-width content)

- The sidebar's content widget is a manually-positioned child pinned at
  `(0, 0, EXPANDED_WIDTH, height)` in `resizeEvent` — not in a layout. Qt
  clips children to the parent rect, so animating the sidebar width just
  reveals/hides pre-laid-out content. No per-frame relayout of the content,
  no text re-wrapping.
- The `_lock_quick_record_layout()` / `_unlock_quick_record_layout()` /
  `_cache_quick_record_lock_width()` hacks are deleted.

### Populate before animating

- `expand()` runs a pending `refresh()` *before* starting the animation and
  forces layout activation + style polish, so the first open animates fully
  rendered content instead of popping it in afterwards.
- History list rendering is capped at `MAX_HISTORY_ITEMS = 100` widgets with a
  "showing N of M" hint; search still filters the full history so older
  entries remain reachable.

### Visual refresh (no feature loss)

- Search input moves up under the main "History" header (always visible, does
  not scroll away); still debounced 250ms and filters history entries.
- Recordings + history sections both live inside one scroll area so long
  recording lists can no longer push the history section off-screen.
- Section headers show live counts (e.g. `RECENT RECORDINGS (3)`).
- History cards get a model badge chip (Local / API / GPT-4o…) in the top row;
  context menu (Copy / Re-transcribe / Delete), click-to-select, and audio
  indicator all preserved.
- Slim styled scrollbar, refined empty states, `WA_StyledBackground` set so
  the sidebar QSS background/border actually paint.

### Horizontal overflow guard (post-review fix)

Stored model values carry device detail (e.g.
`local_whisper (turbo | cuda (float16))`). Rendering that raw string in the
model badge pushed every card's minimum width past the ~340px scroll viewport;
with the horizontal scrollbar hidden, `QScrollArea` sized the content to its
minimum hint and the right side of the panel was clipped. Fixes:

- `_format_model_name()` parses `base (detail)` into a compact badge
  (`Local · turbo`); the full raw string moves to the badge tooltip and the
  context-menu "Model:" line.
- The badge text is elided at 120px as a hard cap.
- The scroll content uses an `Ignored` horizontal size policy so the scroll
  area always sizes it to the viewport width — any future over-wide child
  (e.g. an unbreakable URL in a preview) clips inside its own card instead of
  pushing the whole panel past the sidebar edge.

## Files to modify

- `ui_qt/widgets/history_sidebar.py` — rewrite `HistorySidebar` internals,
  polish `HistoryItemWidget`; keep all public signals
  (`entry_selected`, `entry_copied`, `entry_deleted`,
  `retranscribe_requested`) plus new `width_animated`.
- `ui_qt/main_window.py` — `toggle_history()` simplification, new
  `_on_sidebar_width_animated()` slot, delete `_resize_for_sidebar()`.

## Verification

1. `python -m pytest tests/` (venv active).
2. Launch `python app_qt.py`; first click of the edge tab should animate
   smoothly with content already visible (no pop-in).
3. Toggle rapidly mid-animation — reversal continues from the current width.
4. Expand, resize window wider, collapse — window returns to (new width − 380).
5. Search filtering, copy/delete/re-transcribe context actions, recording
   Transcribe buttons, and geometry save/restore all behave as before.
