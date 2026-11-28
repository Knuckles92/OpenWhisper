# OpenWhisper MCP Tool & Permission Reference

All tools run over the authenticated Streamable HTTP endpoint (default
`http://127.0.0.1:8767/mcp`, or the host's Tailscale URL). Check
`get_capabilities` before calling any permission-gated tool.

## 1. Status, Search & History Retrieval (Baseline Read-Only)

Always available while MCP is enabled. Never deletes history, edits transcript
text, or returns raw audio files.

| Tool | Purpose |
| --- | --- |
| `get_status` | Verify history DB availability, read-only status, paired `clients`, and `capabilities` |
| `get_capabilities` | Exact map of enabled action permissions and writable setting keys |
| `get_history_digest` | Fetch a chronological batch of dictations, meetings, and (if allowed) walkthroughs with text + annotations; supports `only_unannotated=true` |
| `search_history` | Literal substring search across dictations, meeting titles, and meeting speech (`since`/`before` require timezone, e.g. `2026-11-25T00:00:00Z`) |
| `list_transcriptions` | Paginated dictations & file transcriptions, newest first |
| `get_transcription` | Full cleaned/raw text and source metadata (use `get_record_annotation` or `get_history_digest` for notes/tags) |
| `list_meetings` | Paginated meetings, newest first (`q` matches title; use `search_history` for speech) |
| `get_meeting` | Meeting metadata and lifecycle status (use `get_record_annotation` or `get_history_digest` for notes/tags) |
| `list_meeting_segments` | Transcript segments in speaking order (`start_s` inclusive, `end_s` exclusive, in seconds) |
| `get_meeting_segment` | Resolve one evidence segment ID to exact speaker, timestamps, and text |
| `get_meeting_insights` | Saved summary, key points, decisions, action items, questions, and custom reports |
| `get_record_annotation` | Read agent summary, notes, tags, and `external_refs` for a transcription, meeting, or walkthrough |
| `list_annotations` | Browse annotated records filtered by `record_kind`, `tag`, `external_system`, or `q` |
| `list_tags` | List all tags currently used on saved records with counts |

### Multi-Computer Parameters
- `include_remote=true`: include copies stored on this host for paired computers.
- `include_clients=true`: also query online paired clients that opted into history sharing.
- `device_id`: scope a list/search to one client, or route a detail read (`get_transcription`, `get_meeting`, etc.) to that live client.
- `device_name`: the same, by the client's current name in `get_status` `clients` instead of its ID (case and spacing don't count). Pass one or the other. A name two clients share is refused: use `device_id`.

## 2. Organizing History & Agent Notes

| Tool | Permission in Settings → API & MCP | Notes |
| --- | --- | --- |
| `retitle_transcription` | **Rename dictations** (`retitle_transcriptions`) | 1–200 chars; local only; preserves `source_name` & text |
| `retitle_meeting` | **Rename finished meetings** (`retitle_meetings`) | 1–200 chars; finished local meetings only |
| `set_record_annotation` | **Attach agent notes & tags** (`manage_annotations`) | Attach/merge `summary`, `notes`, `tags`, `external_refs`, `author` on any local transcription, meeting, or walkthrough |
| `delete_record_annotation` | **Attach agent notes & tags** (`manage_annotations`) | Remove an annotation without touching the record |
| `organize_records` | Requires each item's matching permission (`retitle_*` and/or `manage_annotations`) | Batch-update up to 25 local records in one call; supports `dry_run=true` |

## 3. Preferences

| Tool | Permission | Notes |
| --- | --- | --- |
| `get_settings` | **Read app preferences** (`settings_access`) | Returns exact keys, valid choices, bounds, current values, and `writable` flags |
| `update_settings` | **Read app preferences** + per-key checkbox | Atomic transaction; returns `updated`, `previous`, and `restore` |

## 4. Walkthroughs

| Tool | Permission | Notes |
| --- | --- | --- |
| `list_walkthroughs` | **Read saved walkthroughs** (`read_walkthroughs`) | Lists `#001` references, folder IDs, timestamps, shot counts, write-up status, and annotation indicators |
| `get_walkthrough` | **Read saved walkthroughs** (`read_walkthroughs`) | Pass `ref="#001"`, `"latest"`, or folder ID; optional `spoken=true`, `include_images=true` |
| `start_walkthrough`, `finish_walkthrough`, `cancel_walkthrough`, `set_walkthrough_paused`, `snap_walkthrough`, `choose_walkthrough_snap_mode`, `edit_walkthrough_shots`, `run_walkthrough_writeup`, `take_screenshot` | **Control walkthroughs & screenshots** (`control_walkthroughs`) | Live walkthrough capture and AI write-up control |

## 5. Personalization & Learned Cleanup Rules

Read tools (`get_personalization_summary`, `list_dictionary`, `list_snippets`,
`list_cleanup_rules`, `get_app_styles`, `list_walkthrough_styles`, `list_languages`,
`list_profiles`, `list_transforms`) require no extra write switch.

- **Manage dictionary, snippets & styles** (`manage_personalization`):
  - Dictionary: `add_dictionary_term`, `bulk_add_dictionary_terms` (up to 50, `dry_run`), `remove_dictionary_term`
  - Snippets: `upsert_snippet`, `bulk_upsert_snippets` (up to 20, `dry_run`, `skip_existing`), `delete_snippet`
  - Styles & Languages: `set_app_style_tone`, `set_app_style_override`, `remove_app_style_override`, `upsert_walkthrough_style`, `delete_walkthrough_style`, `set_active_language`, `set_quick_record_profile`
- **Manage learned cleanup rules** (`manage_cleanup_rules`):
  - `add_cleanup_rule` (1–500 chars, supports `dry_run=true`), `update_cleanup_rule`, `remove_cleanup_rule` (by 0-based `index` or `rule` text)

## 6. Live Recording, Actions, Meetings, Events & App Control

- **Live read**: `get_live_state`, `get_meeting_live_state`, `get_meeting_dashboard_state`, `list_actions`, `get_engine_status`, `list_models`, `list_audio_devices`, `get_remote_status`
- **Live events** (`live_events`, on by default): `recent_events` (and optional MCP Events webhook subscriptions via `events/list`, `events/subscribe`, `events/unsubscribe` when MCP Events is enabled)
- **Run actions** (`run_actions`): `run_action`, `run_transform`, `notify_user` (agent/channel actions also need `agent_prompts`)
- **Start recordings** (`control_recording`): `start_dictation`, `stop_dictation`, `cancel_dictation`
- **Control Meeting Mode** (`control_meetings`): `start_meeting`, `pause_meeting`, `resume_meeting`, `end_meeting`, `background_meeting`, `retry_meeting_finalization`, `manage_recovered_meeting`, `open_meeting_view`, `update_live_meeting`, `manage_meeting_card_item`, `manage_meeting_question`, `update_meeting_transcript_or_speaker`, `request_meeting_note_adjustment`, `request_meeting_custom_report`, `undo_live_meeting_action`
- **Control app** (`control_app`): `select_model`, `download_model`, `select_audio_device`, `reconnect_remote`, `rename_paired_computer` (host only: the name shown for a paired client, by `device_id` or `device_name`; empty `name` restores its paired name), `transcribe_audio_file`, `control_window`
