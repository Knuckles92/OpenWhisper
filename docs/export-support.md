# Export support

Meeting exports are available from the dashboard/history and the desktop Past Meetings export dialog. History exports cover dictation and uploaded recordings. Desktop dialogs support selection, date filters, and combined or separate files.

| Content | Meeting Markdown | Meeting TXT | Meeting JSON | Full meeting download |
| --- | --- | --- | --- | --- |
| Current transcript, timestamps, speaker names | Yes, optional | Yes | Yes | Yes |
| Original transcript before reversible term corrections | No | No | Yes, `original_text` | No |
| Topic/history, summary, all seven card types | Yes, optional | No | Yes | Yes |
| Action owners/deadlines, risk severity, timeline times | Yes | No | Yes | Yes |
| Spoken note attribution | Yes | Transcript only | Yes | Yes |
| Active user clarifications and review uncertainty | Yes | No | Yes | Yes |
| Citation advisories for the current item revision | Yes | No | Full assessment | Yes |
| Saved meeting pulses | Timestamp, kind, text | No | Full pulse and assessment | Timestamp, kind, text |
| Questions, suggested answers, resolved answers | Yes | No | Yes | Yes |
| Evidence IDs, removed items, review history, finalization diagnostics | No | No | Yes | Visible content only |

Summary downloads print the selected Ribbon, Brief, or Signal report. Full meeting downloads add the complete document and are enabled only after the full transcript loads. Ribbon keeps actions, decisions, and risks without a matching timeline beat under **Additional insights**.

Meeting Markdown's intelligence toggle controls pulses, cards, clarifications, questions, topic, and summary together. TXT remains a transcript format; JSON always retains the complete exported state. Markdown omits removed cards, dismissed questions, and superseded clarifications. Human-readable meeting dates use local time, including older recordings saved before UTC timestamps were introduced.

JSON preserves provider/model metadata, state, evidence, pulse assessments, and original/corrected segment text. Dashboard access tokens, process-liveness fields, and the duplicate `state_json` are excluded. Exports contain recording references where applicable, **not audio files**; the JSON document is not a portable recording backup.

History JSON preserves every currently persisted history field. Markdown includes cleaned/raw text according to the selected toggles; TXT includes the saved text and any raw version. Both retain cleanup provider/model information and uploaded-file or batch source names.

## Regression coverage

- `tests/test_meeting_export_features.py`: newer features, web/bulk parity, reversible manual/spoken corrections, raw JSON, provider metadata, legacy/UTC dates, and history field coverage.
- Existing exporter, bulk, Qt dialog, and web authorization tests: selection/filtering, file collisions, content toggles, and token stripping.
- `webui/tests/report-export.cjs`: full/summary document content, current citation revisions, unmatched Ribbon insights, and incomplete-transcript download gating.

Run the Python export tests with `python -m pytest tests/test_meeting_export*.py tests/test_history_export*.py tests/test_export_dialog_base.py tests/test_meeting_web_auth.py` (expand globs if the shell does not). Run dashboard checks with `npm test` and `npm run build` from `webui`.
