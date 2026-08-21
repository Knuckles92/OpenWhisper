"""FastAPI application for the meeting dashboard: SPA, REST API, WebSocket.

``create_app`` wires every pinned route against a ``MeetingEngine`` and a
``MeetingRepository``. All blocking engine/repository calls run through
``asyncio.to_thread`` so the event loop never stalls on SQLite or engine
locks. When the built React frontend (``webui/dist``) is absent, a compact
self-contained fallback page is served so the live pipeline can be verified
without the frontend build.
"""
from __future__ import annotations

import asyncio
import base64
import functools
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, Optional, Set

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

import config
from meeting.content import summarize_meeting_content
from meeting.export.json_export import export_json
from meeting.export.markdown import export_markdown
from meeting.export.transcript_txt import export_transcript_txt
from meeting.audio_playback import build_playback
from meeting.refinalize import rerun_finalization
from meeting.respeaker import rerun_speakers
from meeting.persist.data_lifecycle import delete_meeting_data
from meeting.state.schema import (
    FinalizationState,
    MeetingState,
    compact_finalization_list_fields,
)
from meeting.web.auth import resolve_role
from meeting.web.ws import WsHub

logger = logging.getLogger(__name__)

#: Meeting fields safe to expose to dashboard clients. Tokens and the raw
#: state_json snapshot are deliberately excluded.
_PUBLIC_MEETING_KEYS = (
    "id", "title", "status", "started_at", "ended_at",
    "paused_total_s", "cloud_enabled", "asr_model",
)

#: Export format -> (exporter, media type, file extension).
_EXPORTERS = {
    "md": (export_markdown, "text/markdown", "md"),
    "json": (export_json, "application/json", "json"),
    "txt": (export_transcript_txt, "text/plain", "txt"),
}

_TRANSCRIPT_PAGE_DEFAULT = 500
_TRANSCRIPT_PAGE_MAX = 1000


def _encode_cursor(item: Dict[str, Any]) -> str:
    raw = json.dumps(
        [float(item.get("start_s", 0.0)), str(item.get("id", ""))],
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str) -> tuple[Optional[float], Optional[str]]:
    if not cursor:
        return None, None
    try:
        padding = "=" * (-len(cursor) % 4)
        start_s, segment_id = json.loads(
            base64.urlsafe_b64decode(cursor + padding).decode("utf-8")
        )
        return float(start_s), str(segment_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="invalid transcript cursor") from exc


def _meeting_display_title(meeting: Dict[str, Any]) -> str:
    """Return the best persisted label available for a history row."""
    title = str(meeting.get("title") or "").strip()
    if title:
        return title
    try:
        state = json.loads(meeting.get("state_json") or "{}")
    except (TypeError, ValueError):
        return ""
    if not isinstance(state, dict):
        return ""
    state_title = str(state.get("title") or "").strip()
    if state_title:
        return state_title
    topic = state.get("topic")
    if isinstance(topic, dict):
        return str(topic.get("current") or "").strip()
    return ""


def _public_meeting(
    meeting: Optional[Dict[str, Any]],
    repository: Optional[Any] = None,
) -> Dict[str, Any]:
    """Strip a repository meeting dict down to client-safe fields."""
    if not meeting:
        return {}
    public = {key: meeting.get(key) for key in _PUBLIC_MEETING_KEYS}
    public["display_title"] = _meeting_display_title(meeting)
    public.update(compact_finalization_list_fields(meeting))
    if repository is not None:
        summary = summarize_meeting_content(
            repository, str(meeting.get("id") or "")
        )
        public["content_summary"] = summary
        public.update({
            "has_audio": summary["has_audio"],
            "has_transcript": summary["has_transcript"],
            "can_rerun_speakers": summary["can_rerun_speakers"],
        })
    return public


def _webui_dist_dir() -> str:
    """Location of the built React frontend inside the bundle/repo."""
    return os.path.join(config.bundle_root(), "webui", "dist")


def create_app(engine: Any, repository: Any, hub: WsHub) -> FastAPI:
    """Build the dashboard FastAPI app.

    Args:
        engine: The ``MeetingEngine`` (actions, lifecycle, live state).
        repository: A ``MeetingRepository`` for reads and history.
        hub: The shared ``WsHub`` handling WebSocket connections.

    Returns:
        A fully-routed ``FastAPI`` application (docs endpoints disabled).
    """

    #: Consolidation runs hold a worker for a whole LLM round trip. They get a
    #: dedicated single-thread executor so they can never drain the loop's
    #: shared pool and stall authentication for every other client.
    insights_executor = ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="meeting-reinsights"
    )
    #: Meeting ids with a consolidation run in flight (double-click guard).
    insights_running: Set[str] = set()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        hub.on_startup()
        try:
            yield
        finally:
            hub.on_shutdown()
            insights_executor.shutdown(wait=False)

    app = FastAPI(
        title="OpenWhisper Meeting",
        lifespan=lifespan,
        docs_url=None, redoc_url=None, openapi_url=None,
    )

    dist_dir = _webui_dist_dir()
    assets_dir = os.path.join(dist_dir, "assets")
    if os.path.isdir(assets_dir):
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    # ------------------------------------------------------------------
    # Auth helpers
    # ------------------------------------------------------------------

    async def _current_meeting() -> Optional[Dict[str, Any]]:
        meeting_id = getattr(engine, "meeting_id", None)
        if not meeting_id:
            return None
        return await asyncio.to_thread(repository.get_meeting, meeting_id)

    async def _resolve(token: str) -> Optional[str]:
        meeting = await _current_meeting()
        if not meeting:
            return None
        return resolve_role(
            token, meeting.get("host_token"), meeting.get("guest_token")
        )

    async def _require(token: str, host_only: bool = False) -> str:
        role = await _resolve(token)
        if role is None:
            raise HTTPException(status_code=401, detail="invalid token")
        if host_only and role != "host":
            raise HTTPException(status_code=403, detail="host only")
        return role

    async def _require_meeting(token: str, meeting_id: str) -> str:
        role = await _require(token)
        if role == "guest" and meeting_id != getattr(engine, "meeting_id", None):
            raise HTTPException(status_code=403, detail="guest access is current meeting only")
        return role

    async def _transcript_page(meeting_id: str, cursor: str,
                               limit: int) -> Dict[str, Any]:
        start_s, segment_id = _decode_cursor(cursor)
        page_limit = max(1, min(int(limit), _TRANSCRIPT_PAGE_MAX))
        rows = await asyncio.to_thread(
            repository.get_segments_page, meeting_id, start_s, segment_id,
            page_limit + 1,
        )
        has_more = len(rows) > page_limit
        items = rows[:page_limit]
        return {
            "items": items,
            "next_cursor": _encode_cursor(items[-1])
            if has_more and items else None,
        }

    def _stored_state(meeting_id: str, meeting: Dict[str, Any]) -> Dict[str, Any]:
        """Parse a meeting row's persisted state document (blocking)."""
        try:
            raw = json.loads(meeting.get("state_json") or "{}")
            if not isinstance(raw, dict):
                raise ValueError("state_json is not an object")
            raw.setdefault("meeting_id", meeting_id)
            raw.setdefault("title", str(meeting.get("title") or ""))
            raw.setdefault("status", str(meeting.get("status") or "ended"))
            raw.setdefault(
                "cloud_enabled", bool(meeting.get("cloud_enabled", False))
            )
            # Non-live REST snapshots must not expose interrupted in-flight work.
            raw["finalization"] = FinalizationState.normalize_historical(
                raw.get("finalization"),
                cloud_enabled=bool(raw.get("cloud_enabled", False)),
                meeting_status=str(raw.get("status") or "ended"),
            ).to_dict()
            return MeetingState.from_dict(raw).to_dict()
        except (KeyError, TypeError, ValueError):
            logger.exception("Corrupt state_json for meeting %s", meeting_id)
            return MeetingState(
                meeting_id=meeting_id,
                title=str(meeting.get("title") or ""),
                status=str(meeting.get("status") or "ended"),
            ).to_dict()

    async def _state_for(meeting_id: str,
                         meeting: Dict[str, Any]) -> Dict[str, Any]:
        """Live snapshot for the current meeting, stored snapshot otherwise.

        Both branches block (store lock / JSON parse of a whole document), so
        both run on a worker thread.
        """
        store = getattr(engine, "store", None)
        if store is not None and getattr(engine, "meeting_id", None) == meeting_id:
            return await asyncio.to_thread(store.snapshot)
        return await asyncio.to_thread(_stored_state, meeting_id, meeting)

    async def _json_body(request: Request) -> Dict[str, Any]:
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="invalid JSON body")
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="body must be an object")
        return body

    # ------------------------------------------------------------------
    # Dashboard page + WebSocket
    # ------------------------------------------------------------------

    @app.get("/m/{token}", response_class=HTMLResponse)
    async def dashboard(token: str) -> Response:
        role = await _resolve(token)
        if role is None:
            raise HTTPException(status_code=403, detail="invalid or expired link")
        index_path = os.path.join(dist_dir, "index.html")
        if os.path.isfile(index_path):
            return FileResponse(index_path, media_type="text/html")
        return HTMLResponse(_FALLBACK_PAGE)

    @app.websocket("/ws")
    async def ws_endpoint(websocket: WebSocket) -> None:
        await hub.handle_connection(websocket)

    # ------------------------------------------------------------------
    # Session + transcript
    # ------------------------------------------------------------------

    @app.get("/api/session")
    async def api_session(token: str = "") -> Dict[str, Any]:
        role = await _require(token)
        meeting = await _current_meeting()
        store = getattr(engine, "store", None)
        state = await asyncio.to_thread(store.snapshot) if store is not None else {}
        return {"role": role, "meeting": _public_meeting(meeting), "state": state}

    @app.get("/api/transcript")
    async def api_transcript(token: str = "", cursor: str = "",
                             limit: int = _TRANSCRIPT_PAGE_DEFAULT) -> Dict[str, Any]:
        await _require(token)
        meeting_id = getattr(engine, "meeting_id", None)
        if not meeting_id:
            return {"items": [], "next_cursor": None}
        return await _transcript_page(meeting_id, cursor, limit)

    # ------------------------------------------------------------------
    # Past meetings (host only)
    # ------------------------------------------------------------------

    @app.get("/api/meetings")
    async def api_meetings(token: str = "") -> Dict[str, Any]:
        await _require(token, host_only=True)
        rows = await asyncio.to_thread(repository.list_meetings)
        meetings = await asyncio.gather(*[
            asyncio.to_thread(_public_meeting, row, repository)
            for row in rows
        ])
        return {"meetings": meetings}

    @app.get("/api/meetings/{meeting_id}")
    async def api_meeting_detail(meeting_id: str, token: str = "") -> Dict[str, Any]:
        await _require(token, host_only=True)
        meeting = await asyncio.to_thread(repository.get_meeting, meeting_id)
        if meeting is None:
            raise HTTPException(status_code=404, detail="unknown meeting")
        transcript = await _transcript_page(
            meeting_id, "", _TRANSCRIPT_PAGE_DEFAULT
        )
        public_meeting = await asyncio.to_thread(
            _public_meeting, meeting, repository
        )
        return {
            "meeting": public_meeting,
            "state": await _state_for(meeting_id, meeting),
            "segments": transcript["items"],
            "transcript_next_cursor": transcript["next_cursor"],
        }

    @app.get("/api/meetings/{meeting_id}/transcript")
    async def api_meeting_transcript(meeting_id: str, token: str = "",
                                     cursor: str = "",
                                     limit: int = _TRANSCRIPT_PAGE_DEFAULT) -> Dict[str, Any]:
        await _require_meeting(token, meeting_id)
        meeting = await asyncio.to_thread(repository.get_meeting, meeting_id)
        if meeting is None:
            raise HTTPException(status_code=404, detail="unknown meeting")
        return await _transcript_page(meeting_id, cursor, limit)

    @app.get("/api/meetings/{meeting_id}/segments/{segment_id}")
    async def api_segment(meeting_id: str, segment_id: str,
                          token: str = "") -> Dict[str, Any]:
        await _require_meeting(token, meeting_id)
        segment = await asyncio.to_thread(
            repository.get_segment, meeting_id, segment_id
        )
        if segment is None:
            raise HTTPException(status_code=404, detail="unknown segment")
        return {"segment": segment}

    @app.get("/api/meetings/{meeting_id}/audio")
    async def api_meeting_audio(meeting_id: str, token: str = "") -> Response:
        await _require_meeting(token, meeting_id)
        try:
            path = await asyncio.to_thread(
                build_playback, repository, meeting_id
            )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return FileResponse(
            path, media_type="audio/wav", filename=f"meeting-{meeting_id}.wav",
            content_disposition_type="inline",
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/api/meetings/{meeting_id}/rename")
    async def api_rename_meeting(meeting_id: str, request: Request,
                                 token: str = "") -> Dict[str, Any]:
        await _require(token, host_only=True)
        body = await _json_body(request)
        title = str(body.get("title") or "").strip()
        if not title:
            raise HTTPException(status_code=400, detail="title required")
        store = getattr(engine, "store", None)
        if store is not None and getattr(engine, "meeting_id", None) == meeting_id:
            results = await asyncio.to_thread(
                engine.apply_client_action, "host", None,
                {"op": "set_title", "text": title},
            )
            ok = bool(results and results[0].ok)
        else:
            meeting = await asyncio.to_thread(repository.get_meeting, meeting_id)
            if meeting is None:
                raise HTTPException(status_code=404, detail="unknown meeting")
            await asyncio.to_thread(repository.rename_meeting, meeting_id, title)
            ok = True
        return {"ok": ok, "title": title}

    @app.post("/api/meetings/{meeting_id}/reinsights")
    async def api_rerun_insights(meeting_id: str,
                                 token: str = "") -> Dict[str, Any]:
        """Retry failed post-meeting steps, including redecode when needed."""
        await _require(token, host_only=True)
        if getattr(engine, "meeting_id", None) == meeting_id and engine.is_active():
            raise HTTPException(
                status_code=409,
                detail="cannot re-run insights on the active meeting",
            )
        meeting = await asyncio.to_thread(repository.get_meeting, meeting_id)
        if meeting is None:
            raise HTTPException(status_code=404, detail="unknown meeting")
        # The meeting's own recorded provider/model win; the engine's options
        # fill in for meetings recorded with cloud intelligence off.
        options = getattr(engine, "options", None)
        provider = (meeting.get("agent_provider")
                    or getattr(options, "llm_provider", "") or "openrouter")
        model = meeting.get("agent_model") or getattr(options, "llm_model", "") or ""
        endpoint = getattr(options, "llm_endpoint", None)
        raw_endpoint = meeting.get("agent_endpoint_json")
        if isinstance(raw_endpoint, dict):
            endpoint = raw_endpoint
        elif isinstance(raw_endpoint, str) and raw_endpoint.strip():
            try:
                parsed = json.loads(raw_endpoint)
            except Exception:
                parsed = None
            if isinstance(parsed, dict):
                endpoint = parsed
        speaker_api_key = ""
        try:
            from services.transcript_cleanup import find_api_key

            speaker_api_key = find_api_key("openai") or ""
        except Exception:
            speaker_api_key = ""
        language = getattr(options, "asr_language", None)
        # Check-and-claim with no await between: a double-click cannot start
        # two agent cores writing the same past meeting.
        if meeting_id in insights_running:
            raise HTTPException(
                status_code=409,
                detail="insights are already running for this meeting",
            )
        insights_running.add(meeting_id)
        try:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                insights_executor,
                functools.partial(
                    rerun_finalization, repository, meeting_id,
                    from_step="failed",
                    provider=provider, model=model, endpoint=endpoint,
                    agent_core_kind=getattr(options, "agent_core_kind", "pi"),
                    sidecar_payload_dir=getattr(options, "sidecar_payload_dir", None),
                    asr_model_name=str(meeting.get("asr_model") or "auto"),
                    language=language,
                    speaker_api_key=speaker_api_key,
                ),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        finally:
            insights_running.discard(meeting_id)

    @app.post("/api/meetings/{meeting_id}/respeakers")
    async def api_rerun_speakers(meeting_id: str,
                                 token: str = "") -> Dict[str, Any]:
        """Re-run OpenAI speaker identification on a past meeting."""
        await _require(token, host_only=True)
        if getattr(engine, "meeting_id", None) == meeting_id and engine.is_active():
            raise HTTPException(
                status_code=409,
                detail="cannot re-run speakers on the active meeting",
            )
        meeting = await asyncio.to_thread(repository.get_meeting, meeting_id)
        if meeting is None:
            raise HTTPException(status_code=404, detail="unknown meeting")
        content = await asyncio.to_thread(
            summarize_meeting_content, repository, meeting_id
        )
        if not content["can_rerun_speakers"]:
            raise HTTPException(
                status_code=400,
                detail=(
                    "no system-audio recording is available for speaker "
                    "identification"
                ),
            )
        try:
            from services.settings import (
                resolve_meeting_audio_upload_consent,
                resolve_meeting_speaker_id_backend,
            )
            from services.transcript_cleanup import find_api_key
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        if resolve_meeting_speaker_id_backend() != "openai":
            raise HTTPException(
                status_code=400,
                detail="speaker identification is set to on-device",
            )
        if not resolve_meeting_audio_upload_consent():
            raise HTTPException(
                status_code=400,
                detail="audio-upload consent has not been given",
            )
        api_key = find_api_key("openai") or ""
        if not api_key:
            raise HTTPException(
                status_code=400,
                detail="no OpenAI API key is configured",
            )
        if meeting_id in insights_running:
            raise HTTPException(
                status_code=409,
                detail="a post-meeting pass is already running for this meeting",
            )
        insights_running.add(meeting_id)
        store = None
        if getattr(engine, "meeting_id", None) == meeting_id:
            store = getattr(engine, "store", None)
        try:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                insights_executor,
                functools.partial(
                    rerun_speakers, repository, meeting_id,
                    api_key=api_key, store=store,
                    spool_dir=meeting.get("spool_dir"),
                ),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        finally:
            insights_running.discard(meeting_id)

    @app.delete("/api/meetings/{meeting_id}")
    async def api_delete_meeting(meeting_id: str, token: str = "") -> Dict[str, Any]:
        await _require(token, host_only=True)
        if getattr(engine, "meeting_id", None) == meeting_id and engine.is_active():
            raise HTTPException(status_code=409,
                                detail="cannot delete the active meeting")
        meeting = await asyncio.to_thread(repository.get_meeting, meeting_id)
        if meeting is None:
            raise HTTPException(status_code=404, detail="unknown meeting")
        options = getattr(engine, "options", None)
        meetings_root = getattr(options, "spool_root", None) or config.MEETINGS_FOLDER
        await asyncio.to_thread(
            delete_meeting_data, repository, meeting_id, meetings_root
        )
        return {"ok": True}

    # ------------------------------------------------------------------
    # Search + export (host only)
    # ------------------------------------------------------------------

    @app.get("/api/search")
    async def api_search(token: str = "", q: str = "") -> Dict[str, Any]:
        await _require(token, host_only=True)
        results = await asyncio.to_thread(repository.search_transcripts, q)
        return {"results": results}

    @app.get("/api/events")
    async def api_events(token: str = "", before_seq: Optional[int] = None,
                         limit: int = 100) -> Dict[str, Any]:
        await _require(token, host_only=True)
        meeting_id = getattr(engine, "meeting_id", None)
        if not meeting_id:
            return {"events": []}
        events = await asyncio.to_thread(
            repository.list_events, meeting_id, before_seq, limit
        )
        return {"events": events}

    @app.get("/api/export/{fmt}")
    async def api_export(fmt: str, token: str = "",
                         meeting_id: str = "") -> Response:
        await _require(token, host_only=True)
        entry = _EXPORTERS.get(fmt)
        if entry is None:
            raise HTTPException(status_code=404, detail="unknown export format")
        target_id = meeting_id or getattr(engine, "meeting_id", None) or ""
        if not target_id:
            raise HTTPException(status_code=404, detail="no meeting")
        meeting = await asyncio.to_thread(repository.get_meeting, target_id)
        if meeting is None:
            raise HTTPException(status_code=404, detail="unknown meeting")
        state = await _state_for(target_id, meeting)
        segments = await asyncio.to_thread(repository.get_segments, target_id)
        exporter, media_type, extension = entry
        content = await asyncio.to_thread(
            exporter, _public_meeting(meeting), state, segments
        )
        filename = f"meeting-{target_id}.{extension}"
        return Response(
            content=content,
            media_type=media_type,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
            },
        )

    # ------------------------------------------------------------------
    # Meeting control (host only)
    # ------------------------------------------------------------------

    @app.post("/api/meeting/end")
    async def api_meeting_end(token: str = "") -> Dict[str, Any]:
        await _require(token, host_only=True)
        await asyncio.to_thread(engine.end)
        return {"ok": True}

    @app.post("/api/meeting/pause")
    async def api_meeting_pause(token: str = "") -> Dict[str, Any]:
        await _require(token, host_only=True)
        await asyncio.to_thread(engine.pause)
        return {"ok": True}

    @app.post("/api/meeting/resume")
    async def api_meeting_resume(token: str = "") -> Dict[str, Any]:
        await _require(token, host_only=True)
        await asyncio.to_thread(engine.resume)
        return {"ok": True}

    @app.post("/api/meeting/cloud")
    async def api_meeting_cloud(request: Request, token: str = "") -> Dict[str, Any]:
        await _require(token, host_only=True)
        body = await _json_body(request)
        enabled = bool(body.get("enabled"))
        await asyncio.to_thread(engine.set_cloud_enabled, enabled)
        return {"ok": True, "enabled": enabled}

    @app.post("/api/meeting/tokens/regenerate")
    async def api_regenerate_tokens(token: str = "") -> Dict[str, Any]:
        await _require(token, host_only=True)
        result = await asyncio.to_thread(engine.regenerate_tokens)
        return {"ok": True, **result}

    return app


#: Self-contained dark-theme dashboard served when webui/dist is missing.
#: Vanilla JS: connects to /ws, prompts guests for a name (close code 4400),
#: renders live transcript, topic, cards, and questions. Verification aid
#: only; the React build replaces it when present.
_FALLBACK_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OpenWhisper Meeting</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; margin: 0; }
  body { background:#111418; color:#e8eaed; font:14px/1.5 "Segoe UI",system-ui,sans-serif; }
  header { padding:14px 20px; background:#1a1f26; border-bottom:1px solid #2a313b;
           display:flex; gap:12px; align-items:baseline; flex-wrap:wrap; }
  header h1 { font-size:17px; font-weight:600; }
  #status { color:#9aa4b2; font-size:12px; }
  .pill { font-size:11px; padding:2px 8px; border-radius:10px; background:#243041; color:#8ab4f8; }
  main { display:grid; grid-template-columns:1.2fr .8fr; gap:16px; padding:16px 20px;
         max-width:1200px; margin:0 auto; }
  @media (max-width:800px){ main{ grid-template-columns:1fr; } }
  section { background:#1a1f26; border:1px solid #2a313b; border-radius:10px;
            padding:12px 14px; margin-bottom:16px; }
  section h2 { font-size:12px; text-transform:uppercase; letter-spacing:.08em;
               color:#9aa4b2; margin-bottom:8px; }
  #topic { font-size:15px; font-weight:600; }
  #summary { color:#c3c9d1; font-size:13px; margin-top:6px; white-space:pre-wrap; }
  #transcript { max-height:60vh; overflow-y:auto; display:flex; flex-direction:column; gap:6px; }
  .seg { display:flex; gap:8px; }
  .seg .t { color:#6f7a87; font-variant-numeric:tabular-nums; flex:none; }
  .seg .who { color:#8ab4f8; flex:none; }
  .card { margin-bottom:12px; }
  .card h3 { font-size:12px; color:#c3c9d1; margin-bottom:4px; text-transform:capitalize; }
  .card ul { margin-left:18px; }
  .card li { margin:3px 0; }
  .badge { font-size:10px; color:#6f7a87; margin-left:6px; }
  .q { border-left:3px solid #b8860b; padding-left:8px; margin:6px 0; }
  .q .ans { color:#7dcf85; }
</style>
</head>
<body>
<header>
  <h1 id="title">Meeting</h1>
  <span class="pill" id="role-pill">&hellip;</span>
  <span id="status">connecting&hellip;</span>
</header>
<main>
  <div>
    <section><h2>Topic</h2><div id="topic">&mdash;</div><div id="summary"></div></section>
    <section><h2>Transcript</h2><div id="transcript"></div></section>
  </div>
  <div>
    <section><h2>Cards</h2><div id="cards"></div></section>
    <section><h2>Questions</h2><div id="questions"></div></section>
  </div>
</main>
<script>
"use strict";
var token = decodeURIComponent(location.pathname.split("/").pop());
var CARD_KEYS = ["live_notes","key_points","decisions","action_items","risks","timeline","user_notes"];
var state = null, meeting = null, role = null, segs = {}, ws = null, pingTimer = null;
function $(id){ return document.getElementById(id); }
function esc(s){ var d = document.createElement("div"); d.textContent = String(s == null ? "" : s); return d.innerHTML; }
function fmtT(s){ s = Math.max(0, Math.floor(s || 0)); var m = Math.floor(s / 60); return m + ":" + String(s % 60).padStart(2, "0"); }
function setStatus(s){ $("status").textContent = s; }
function speakerName(sg){
  if (state && sg.speaker_participant_id){
    var p = (state.participants || {})[sg.speaker_participant_id];
    if (p) return p.display_name;
  }
  return sg.channel === "mic" ? "Me" : "Others";
}
function finalizationLabel(){
  if (!state || !state.finalization) return "";
  var f = state.finalization;
  var st = f.status || "";
  var msg = (f.message || "").trim();
  if (st === "running") {
    if (f.total_steps && f.current_step) {
      return "Step " + f.current_step + "/" + f.total_steps + ": " + (msg || "Preparing final insights");
    }
    return msg || "Preparing final insights";
  }
  if (st === "completed") return msg || "Final insights ready";
  if (st === "disabled") return msg || "Cloud insights off";
  if (st === "unavailable") return msg || "Final insights unavailable";
  if (st === "failed") return msg || "Final insights failed";
  return msg || st;
}
function renderHeader(){
  if (!state) return;
  var bits = [state.status || ""];
  bits.push(state.intelligence_online ? "AI online" : "AI offline");
  if (state.cloud_enabled) bits.push("cloud");
  var fin = finalizationLabel();
  if (fin) bits.push(fin);
  $("role-pill").textContent = (role || "") + " \\u00b7 " + bits.join(" \\u00b7 ");
  var t = state.title || (meeting && meeting.title) || "Meeting";
  $("title").textContent = t;
  document.title = t + " \\u2014 OpenWhisper";
}
function addSegments(items){
  if (!items || !items.length) return;
  for (var i = 0; i < items.length; i++){ segs[items[i].id] = items[i]; }
  renderTranscript();
}
function renderTranscript(){
  var el = $("transcript");
  var stick = el.scrollHeight - el.scrollTop - el.clientHeight < 60;
  var arr = Object.values(segs).sort(function(a, b){ return a.start_s - b.start_s; });
  var html = "";
  for (var i = 0; i < arr.length; i++){
    var sg = arr[i];
    html += '<div class="seg"><span class="t">' + fmtT(sg.start_s) + '</span><span class="who">'
          + esc(speakerName(sg)) + ':</span><span>' + esc(sg.text) + '</span></div>';
  }
  el.innerHTML = html;
  if (stick) el.scrollTop = el.scrollHeight;
}
function renderState(){
  if (!state) return;
  renderHeader();
  $("topic").textContent = (state.topic && state.topic.current) || "\\u2014";
  $("summary").textContent = state.rolling_summary || "";
  var html = "";
  for (var i = 0; i < CARD_KEYS.length; i++){
    var key = CARD_KEYS[i];
    var live = ((state.cards || {})[key] || []).filter(function(it){ return it.status !== "removed"; });
    if (!live.length) continue;
    var label = key === "live_notes" ? "Meeting Notes" : key.replace(/_/g, " ");
    html += '<div class="card"><h3>' + esc(label) + '</h3><ul>';
    for (var j = 0; j < live.length; j++){
      var it = live[j];
      var body = "";
      if (key === "live_notes"){
        var d = it.data || {};
        var head = String(d.heading || "").trim();
        var stamp = typeof d.start_s === "number" ? fmtT(d.start_s) : "";
        var bits = (stamp ? [stamp] : []).concat(head ? [head] : []);
        if (bits.length) body += "<strong>" + esc(bits.join(" \\u2014 ")) + "</strong> ";
      }
      body += esc(it.text);
      html += '<li>' + body + '<span class="badge">' + esc(it.status)
            + (it.pinned ? " \\u00b7 pinned" : "") + '</span></li>';
    }
    html += '</ul></div>';
  }
  $("cards").innerHTML = html || '<div class="badge">No items yet.</div>';
  var qh = "";
  var qs = state.questions || [];
  for (var k = 0; k < qs.length; k++){
    var q = qs[k];
    if (q.status === "dismissed") continue;
    qh += '<div class="q">' + esc(q.text);
    if (q.answer) qh += '<div class="ans">' + esc(q.answer) + '</div>';
    else if (q.suggested_answer) qh += '<div class="badge">suggested: ' + esc(q.suggested_answer) + '</div>';
    qh += '</div>';
  }
  $("questions").innerHTML = qh || '<div class="badge">No questions.</div>';
}
function applyEffect(e){
  if (!e || !state) return;
  if (e.entity === "item" && e.item){
    var arr = state.cards[e.item.card] || (state.cards[e.item.card] = []);
    var idx = arr.findIndex(function(x){ return x.id === e.item.id; });
    if (idx >= 0) arr[idx] = e.item; else arr.push(e.item);
  } else if (e.entity === "topic"){ state.topic = e.topic; }
  else if (e.entity === "rolling_summary"){ state.rolling_summary = e.text; }
  else if (e.entity === "title"){ state.title = e.text; }
  else if (e.entity === "cloud_enabled"){ state.cloud_enabled = e.enabled; }
  else if (e.entity === "participant" && e.participant){
    state.participants[e.participant.id] = e.participant; renderTranscript();
  } else if (e.entity === "question" && e.question){
    var qs = state.questions || (state.questions = []);
    var qi = qs.findIndex(function(x){ return x.id === e.question.id; });
    if (qi >= 0) qs[qi] = e.question; else qs.push(e.question);
  } else if (e.entity === "segment_speaker"){
    var sg = segs[e.segment_id];
    if (sg){ sg.speaker_participant_id = e.participant_id; renderTranscript(); }
  }
}
function handle(msg){
  switch (msg.type){
    case "hello":
      role = msg.role; state = msg.state; meeting = msg.meeting;
      segs = {}; addSegments(msg.segments); renderState(); setStatus("live");
      break;
    case "segments": addSegments(msg.items); break;
    case "patch":
      // Seq guard: a patch that lost a race must never regress newer state.
      (msg.results || []).forEach(function(r){
        if (!state) return;
        if (r.seq != null && r.seq <= state.seq) return;
        applyEffect(r.effect);
        if (r.seq != null) state.seq = r.seq;
      });
      renderState();
      break;
    case "status":
      if (state){
        if (msg.status != null) state.status = msg.status;
        if (msg.intelligence_online != null) state.intelligence_online = msg.intelligence_online;
        if (msg.finalization !== undefined) state.finalization = msg.finalization;
      }
      renderHeader();
      if (state && state.finalization && state.finalization.status === "running"){
        setStatus(finalizationLabel() || "preparing final insights");
      } else if (state && state.finalization && state.finalization.status){
        var fs = state.finalization.status;
        if (fs === "completed" || fs === "disabled" || fs === "unavailable" || fs === "failed"){
          setStatus(finalizationLabel() || fs);
        }
      }
      break;
    case "presence":
      if (msg.participant && state) state.participants[msg.participant.id] = msg.participant;
      if (msg.participant) setStatus(msg.participant.display_name + (msg.event === "joined" ? " joined" : " left"));
      break;
    case "meeting_ended":
      if (state) state.status = msg.status || "ended";
      renderHeader();
      if (state && state.finalization && state.finalization.status === "running"){
        setStatus(finalizationLabel() || "meeting ended — preparing final insights");
      } else {
        setStatus("meeting ended");
      }
      break;
  }
}
function guestId(){
  var id = "";
  try { id = sessionStorage.getItem("ow_guest_id") || ""; } catch (e) { return ""; }
  if (!id){
    id = "g" + Date.now().toString(36) + Math.random().toString(36).slice(2, 10);
    try { sessionStorage.setItem("ow_guest_id", id); } catch (e) { /* private mode */ }
  }
  return id;
}
function connect(name){
  var url = (location.protocol === "https:" ? "wss://" : "ws://") + location.host
          + "/ws?token=" + encodeURIComponent(token);
  if (name){
    url += "&name=" + encodeURIComponent(name);
    // Stable key so reconnects reuse one participant, never a new one.
    var gid = guestId();
    if (gid) url += "&guest_id=" + encodeURIComponent(gid);
  }
  setStatus("connecting\\u2026");
  ws = new WebSocket(url);
  ws.onopen = function(){
    pingTimer = setInterval(function(){
      try { ws.send(JSON.stringify({type: "ping"})); } catch (e) {}
    }, 20000);
  };
  ws.onmessage = function(ev){ try { handle(JSON.parse(ev.data)); } catch (e) {} };
  ws.onclose = function(ev){
    clearInterval(pingTimer);
    if (ev.code === 4400){
      var n = (prompt("Enter your display name to join:") || "").trim();
      if (n){ sessionStorage.setItem("ow_name", n); connect(n); }
      else setStatus("A display name is required to join.");
      return;
    }
    if (ev.code === 4401){ setStatus("Invalid or expired link."); return; }
    setStatus("Disconnected \\u2014 reconnecting\\u2026");
    setTimeout(function(){ connect(name); }, 2000);
  };
}
connect(sessionStorage.getItem("ow_name") || "");
</script>
</body>
</html>
"""
