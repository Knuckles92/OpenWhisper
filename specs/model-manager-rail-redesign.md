# Model Manager — left-rail redesign

Design source: `mockups/model-manager-v2/10-left-rail-full.html` (assignment window) and
`09-downloads.html` (catalog window). Gallery: `mockups/model-manager-v2/index.html`.

## Diagnosis

`ModelManagerDialog` opens at 900 × 620 (`model_manager_dialog.py:161`) and wraps the entire On-demand and
Meeting pages in a `QScrollArea` (`_make_scroll_page`, `:362-389`). Those pages need roughly 700 px, so both
scroll on open before the user touches anything.

The height is dominated by `TextModelPicker`, which is a two-column, two-step widget with 44 px combos, a
provider identity card, and a 56 px "Active now" banner — and it is instantiated twice, once per mode tab.

`tests/test_model_manager_dialog.py:220-222` currently *asserts* that each page's content is taller than its
scroll area. The test suite encodes the bug.

The surface owns twelve settings, a 16-row download catalog, three optional components, and endpoint CRUD.
That is not enough content to justify a scrolling page.

## Target shape

Two windows, both non-modal single instances (downloads are long-running; the user must be able to keep
recording).

### Window 1 — Model Manager (assignment only)

Left rail with five destinations, right pane with a `QStackedWidget`. Each rail item's subtitle is its live
value, so the whole configuration is readable without navigating.

| Destination | Controls | Settings |
| --- | --- | --- |
| On-demand · Voice | engine, Whisper model | `SELECTED_MODEL`, `WHISPER_MODEL` |
| On-demand · Text cleanup | endpoint (+CRUD), model, refresh | `TRANSCRIPT_CLEANUP_PROVIDER`, `_MODEL`, `_MODEL_SORT`, `TEXT_LLM_PROFILES` |
| Meeting · Voice | Whisper model, spoken language, speaker ID | `MEETING_WHISPER_MODEL`, `MEETING_LANGUAGE`, `MEETING_SPEAKER_ID_BACKEND` |
| Meeting · Intelligence | endpoint (+CRUD), model, agent core | `MEETING_LLM_PROVIDER`, `_MODEL`, `MEETING_AGENT_CORE` |
| Shared · Runtime | device, quantization, cache summary | `WHISPER_DEVICE`, `WHISPER_COMPUTE_TYPE` |

Rail footer: a **Downloads…** button, the three component status dots, and a `3 of 16 models · 2.2 GB` line.

Device and quantization move out of the Library tab into their own destination. They are shared runtime
settings, not download settings, and today they sit above a scrolling catalog where nobody looks.

### Window 2 — Downloads (new `ui_qt/dialogs/downloads_dialog.py`)

Header (stats, cache path, Open folder), toolbar (search, status filter, sort), the model list as the only
scroller in the catalog column, and a 300 px right inspector. The inspector absorbs `ModelDetailsDialog`, so a
per-model technical profile stops being a third stacked dialog. Components become a status strip along the
bottom.

## Sizing contract

This is the thing that must not regress, and it is enforced by tests rather than by review.

- Model Manager: `DEFAULT_SIZE = QSize(980, 660)`, `MINIMUM_SIZE = QSize(840, 620)`. The height floor is
  measured — the tallest destination (Meeting · Intelligence) needs 601 px under real Windows font metrics, so
  620 clears it. The width floor is a legibility choice, well above Qt's own 638 px minimum: eliding combos and
  labels will shrink past the point where a model id or an endpoint URL is still readable.
- No `QScrollArea` anywhere in the Model Manager window. `SCROLLBAR_GUTTER` / `PAGE_SIDE_MARGIN` existed only
  to reserve room for a scroll bar and are gone with it.
- Every destination's `sizeHint()` must fit inside `MINIMUM_SIZE`, asserted per destination.
- Downloads: `DEFAULT_SIZE = QSize(1060, 680)`, `MINIMUM_SIZE = QSize(980, 560)`. The width is the widest
  catalog row plus its scroll bar plus the inspector — again above Qt's own 822 px minimum, since the rows now
  elide rather than clip and would shrink to something unreadable.
- Only two things scroll there: the catalog list and the inspector's profile column. The header, toolbar, and
  component strip stay put.
- The toolbar's right gutter tracks the list's real scroll-bar width at runtime instead of hard-coding the
  themed width, so the filters and the rows they filter share one right edge whether or not the bar is showing.

If a future feature cannot fit a destination, the answer is a new destination, not a scroll area.

## Width discipline

Two widget habits quietly set the window's floor, and both bit during implementation:

- A non-editable `QComboBox` sizes its minimum to its longest item. A 400-model OpenRouter catalog therefore
  made the window unshrinkable. Use `ElidingComboBox`.
- A `QLabel` reports the full width of its text as its minimum, so one long secondary line (a repo id, a
  cache path, an endpoint URL) raises the minimum width of every ancestor up to the window. Use
  `ElidingLabel` for single-line values and `WrappedLabel` for prose.

When a dialog will not shrink to its stated `MINIMUM_SIZE`, walk `minimumSizeHint()` down the tree; it is
almost always one of these two.

## Work phases

### 1 · Extract the Downloads window

Move out of `ModelManagerDialog` into `DownloadsDialog`: `rows`, `list_layout`, `library_scroll_area`,
`filter_edit`, `status_filter_combo`, `sort_combo`, `downloaded_stat`, `disk_stat`, `env_banner`, the cache
path label, `_build_components_section`, `_apply_filter` (`:1550-1565`), `_sort_key` (`:1567-1579`),
`set_downloading` / `finish_download` / `show_delete_result` (`:1531-1548`), `_build_components_section`
(`:218`), `refresh_components` / `set_component_progress` / `finish_component_install` (`:258-276`), and
`_usage_for` (`:1471`).

Add the inspector: `services.model_catalog.get_model_details()` already returns everything
`ModelDetailsDialog` renders, with no network call. Delete `ui_qt/dialogs/model_details_dialog.py` and its
export in `ui_qt/dialogs/__init__.py`; `ModelRowWidget.details_requested` becomes a selection signal.

`ModelRowWidget` keeps Download / Delete and its usage chip. `Set Active` is already hidden in Library
(`:1521`) and can be removed outright.

### 2 · Rebuild the Model Manager shell

Replace `QTabWidget` + `_ModeTabBar` (`:111-139`, `:325-348`) with a rail + `QStackedWidget`.

Rail implementation — recommend `QListWidget` with `setItemWidget` and a small `_RailItem` widget (two
labels), matching how `ModelRowWidget` and `ComponentRowWidget` are already built. This keeps arrow-key
navigation for free. Caveat: an item widget paints over the item background, so selection styling has to be
applied to `_RailItem` itself on `currentRowChanged`, not left to the view. Group headers
(`ON-DEMAND` / `MEETING MODE` / `SHARED`) are non-selectable items with `Qt.ItemFlag.NoItemFlags`.

`_make_scroll_page` is deleted. `_make_mode_card` (`:391-421`) is no longer needed either — a destination is
a page heading plus fields, not a card inside a card. `_labeled_combo` (`:423-433`) generalizes to `_field`,
which labels any widget rather than only a combo.

Rename and re-point the navigation API (`:1268-1293`):

| Today | Becomes |
| --- | --- |
| `show_ondemand_tab()` | `show_destination("od-voice")` |
| `show_text_tab()` | `show_destination("od-text")` |
| `show_meeting_tab()` | `show_destination("mm-voice")` |
| `show_library_tab()` | gone — callers open the Downloads window |

`_on_manager_tab_changed` (`:1281-1293`) becomes `_on_destination_changed`, prefetching the cleanup catalog on
`od-text` and the meeting catalog on `mm-text`. The provider-changed guards at `:1296` and `:1302` switch from
`currentWidget() is …_tab` to the destination id.

`refresh()` (`:1481-1529`) splits: assignment state stays here and now also updates the rail subtitles; the
catalog rows, stats, and env banner move to the Downloads window's own `refresh()`.

### 3 · Single-column `TextModelPicker`

`text_model_picker.py` is used in exactly two places, both of which become narrow destinations, so rewrite
rather than add a mode flag.

Drop `_step_heading` (`:83-95`), `provider_identity_card` (`:139-181`), and the standalone
`active_summary_card` (`:184-201`). The provider description and base URL move into the combo item tooltip;
credential status becomes an inline line on the endpoint action row; "Active now" becomes a single state row
at the bottom of the destination.

Keep: provider combo, Add / Edit / Delete, `SearchableComboBox` for the model, Refresh, catalog summary text,
and the sort combo. Sort stays a real control for OpenRouter — folding it into a caption would remove a
feature. Put it next to Refresh and show the current order in the field's label line.

### 4 · Rewire call sites

`ui_controller.py:542-585` — `open_model_manager_dialog(tab=…)` takes destination ids. Legacy mapping:
`"text"` → `od-text`, `"meeting"` → `mm-voice`, `"ondemand"` → `od-voice`, and `"library"` / `"voice"` open
the Downloads window instead. Settings' deferred open (`settings_dialog.py:1065-1078`, dispatched at
`ui_controller.py:505`) needs no change.

Add `open_downloads_dialog()` and redirect the six progress forwards at `ui_controller.py:620-666` to it.

`LocalModelPicker.manage_downloads_requested` and `LocalEngineControls`' "Manage models…" both currently open
the Library tab; they now open the Downloads window. The Meeting page's "Open shared runtime" button
(`:584-589`) is replaced by rail navigation.

### 5 · Theme

Delete the `#modelManagerTabs` block (`theme.qss:318-349`). Add `#modelManagerRail`, `#modelManagerRailItem`
(+ `:selected`), `#modelManagerRailGroup`, `#modelManagerRailFooter`, and the Downloads window's object names.
Keep the existing navy palette — the mocks use it unchanged, so no color decisions are open.

## Tests

`tests/test_model_manager_dialog.py` (1001 lines) needs structural rework:

- `:179-184` tab-bar width → rail width assertions.
- `:204` library scroll bar → moves to a Downloads test.
- `:211-222` `test_tall_tabs_scroll_instead_of_growing_dialog` — **invert**. Replace with
  `test_every_destination_fits_minimum_size`, iterating the stack and asserting
  `page.sizeHint().height() <= available` at `MINIMUM_SIZE`, plus
  `page.findChildren(QScrollArea) == []`.
- `:558-564` tab labels/icons → rail item names and icons.
- `:938-955` `show_*_tab` → `show_destination`.
- `:307-320` download-progress tests move to `tests/test_downloads_dialog.py`.

New coverage: rail subtitles reflect persisted settings after `refresh()`; legacy `tab=` values route
correctly; the Downloads inspector renders `get_model_details()` for a selected row.

`tests/test_model_details_dialog.py` (152 lines) becomes inspector tests in `tests/test_downloads_dialog.py`.

## Docs

- `CHANGELOG.md` — Unreleased → Changed. Note that the manager no longer scrolls, that downloads and
  components moved to their own window, and that device/quantization moved to a Runtime screen.
- `CLAUDE.md` and `AGENTS.md` — both describe Model Manager as three tabs with a Library tab owning the
  runtime. Update the dialogs list and the "Model Manager is organized by product mode" framing.
- `design-qa.md` and `design-qa-assets/` graded fidelity against a reference normalized to a 1078 px-tall
  client that no shipped window ever had, for a Voice/Text tab layout that no longer exists. Deleted.

## Resolved decisions

1. **Rail widget** — `QListWidget` + `setItemWidget` with a `_RailItem` widget, matching how `ModelRowWidget`
   and `ComponentRowWidget` are already built, so arrow-key navigation comes for free. The item widget covers
   the view's selection painting as expected, so selection is a property on `_RailItem`; a plain `QWidget`
   also needs `WA_StyledBackground` before QSS will paint the selected pill at all.
2. **Downloads as a second window** — kept as its own window rather than a sixth destination. It lets the
   manager be sized purely for assignment, and a multi-gigabyte download must not lock the user out of
   recording, which a non-modal second window gets for free.
3. **Deleting `ModelDetailsDialog`** — deleted. The inspector's 300 px column carries the same facts; its
   profile prose scrolls in place, so no "Full profile ↗" escape hatch was needed.
4. **Size floors** — 840 × 620 for the manager, 980 × 560 for Downloads. Heights measured under real Windows
   font metrics; widths set for legibility rather than taken from Qt's minimum. See the sizing contract above.
