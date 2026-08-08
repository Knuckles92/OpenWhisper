# Model Manager Text Tab — Design QA

## Comparison target

- Source visual truth: `/mnt/d/eb6682cb-002d-4224-8d4e-ff6dbec395b3.png`
- User-highlighted earlier implementation: `/mnt/c/Users/Big D/Documents/ShareX/Screenshots/2026-08/python_ZS3P6d7spF.png`
- Final implementation screenshot: `/mnt/d/coding/whisper_local/design-qa-assets/model-manager-text-implementation.png`
- Normalized side-by-side evidence: `/mnt/d/coding/whisper_local/design-qa-assets/model-manager-text-comparison.png`
- State: Text tab, OpenRouter selected, API key found, `openrouter/free` active, alphabetical sort, 400-model catalog loaded.

## Viewport and normalization

- Source pixels: 1122 × 1402. The 58 px reference title bar was removed so both sides compare app-owned client content.
- Normalized source content: 1122 × 1344 scaled proportionally to 900 × 1078.
- Implementation client capture: 900 × 1088 logical Qt pixels at device-pixel ratio 1.0.
- Combined evidence: 1800 × 1088, source on the left and implementation on the right, top-aligned on the same dark canvas.
- CSS size is not applicable because this is a native PyQt6 window; Qt logical pixels are the layout unit.

## Full-view comparison evidence

The final implementation preserves the reference's visible hierarchy: branded header, equal segmented Voice/Text tabs, blue-accented Text heading, separate numbered provider and model cards, tall credential/active-state treatment, catalog summary with a dashed connector, compact current-model card, right-aligned primary action, framed information note, and subdued Close action.

The implementation intentionally keeps OpenWhisper's microphone app mark instead of the mock's cube logo. It also uses flat Qt theme surfaces instead of the mock's raster glow/gradient effects. These are product-brand and native-rendering constraints rather than layout drift.

## Focused region evidence

A separate crop was not needed: the 1800 × 1088 combined comparison retains both 900 px-wide client views at readable size. The provider identity, green active-model banner, model selection/status row, and footer note can all be inspected without resampling again.

## Required fidelity surfaces

- Fonts and typography: Segoe UI remains the product font. Header, section, provider, field, action, and support-copy sizes now follow the reference hierarchy without clipping or unexpected wrapping.
- Spacing and layout rhythm: 32 px outer margins, two independent cards, 12–18 px internal rhythm, 52–56 px controls, and a 56 px active banner reproduce the source proportions. Persistent actions remain visible in the captured client viewport.
- Colors and visual tokens: charcoal surfaces, cool gray borders, blue selected/focus states, purple provider identity, and green credential/current states are mapped consistently to the existing dark theme.
- Image quality and asset fidelity: the existing bundled OpenWhisper icon remains crisp. UI symbols use bundled, color-tuned Tabler SVG assets under their included MIT license; no placeholder or code-drawn icons were introduced.
- Copy and content: all source-facing labels and the active provider/model state match the working product behavior. The credential indicator reports only presence and never displays the API key.

## Interaction and runtime checks

- Qt regression coverage exercised Voice/Text tab presence, provider switching, catalog population, model activation, credential-present/missing states, sort behavior, and persistence behavior.
- Refresh, provider selection, model selection, and activation controls remain connected to their existing signals and callbacks.
- Native Windows Qt capture completed without runtime or rendering errors. Browser-console checks are not applicable to this desktop application.
- Focused test result: 63 passed. The only warning was pytest being unable to write its optional cache directory.

## Comparison history

### Iteration 1

- Earlier finding [P2]: the active-model region read as a thin nested alert rather than the reference's primary success panel.
- Earlier finding [P2]: native Windows icons and inherited combo chevrons did not match the reference's coherent line-icon language.
- Earlier finding [P2]: provider/model typography was undersized and the catalog/current-model row did not follow the reference's visual balance.
- Fixes: increased the active panel to 56 px, added the green bolt treatment, bundled Tabler icons and chevron, enlarged the control/type scale, added the dashed catalog connector, resized the current-model card, and restored a two-line footnote.
- Post-fix evidence: `design-qa-assets/model-manager-text-comparison.png`.

### Final pass

- No actionable P0, P1, or P2 differences remain.
- Remaining P3: the native Qt version is flatter than the glow-heavy mock, the application mark intentionally stays microphone-based, and the OpenRouter selector uses a library stack mark rather than the mock's bespoke provider logo.

## Findings

- No blocking or moderate-severity fidelity findings remain.

## Implementation checklist

- [x] Preserve provider/model behavior and persistence.
- [x] Match the two-card hierarchy and active-state emphasis.
- [x] Replace mismatched native icons with a consistent licensed icon set.
- [x] Verify credential-found and credential-missing states.
- [x] Run focused Qt regression tests.
- [x] Compare normalized source and implementation captures.

## Follow-up polish

- P3: add subtle native shadow/elevation only if the broader dialog system adopts the same treatment.
- P3: replace the generic purple provider mark if an approved OpenRouter brand asset is added to the product asset library.

final result: passed
