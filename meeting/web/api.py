"""FastAPI application for the meeting dashboard: SPA, REST API, WebSocket.

``create_app`` wires every pinned route against a ``MeetingEngine`` and a
``MeetingRepository``. All blocking engine/repository calls run through
``asyncio.to_thread`` so the event loop never stalls on SQLite or engine
locks. When the built React frontend (``webui/dist``) is absent, the
dashboard route serves a short notice saying how to build it.
"""
from __future__ import annotations

import asyncio
import base64
import functools
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, Optional, Set

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

import config
from meeting.content import meeting_display_title, summarize_meeting_content
from meeting.custom_report import start_custom_report
from meeting.export.json_export import export_json
from meeting.export.markdown import export_markdown
from meeting.export.transcript_txt import export_transcript_txt
from meeting.audio_playback import build_playback
from meeting.refinalize import (
    FinalizationBusyError,
    rerun_finalization,
    rerun_speakers,
)
from meeting.persist.data_lifecycle import delete_meeting_data
from meeting.state.custom_reports import MAX_REQUEST_CHARS
from meeting.state.schema import MeetingState, parse_state_json
from meeting.stored import compact_finalization_list_fields, load_state, open_store
from meeting.time_utils import meeting_duration_s
from meeting.web.auth import resolve_role
from meeting.web.ws import WsHub
from services.titles import normalize_title

logger = logging.getLogger(__name__)

#: An End request slower than this is logged with where its time went.
_SLOW_END_WARN_S = 1.0

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

#: Why a report request was turned down, in words the requester can act on.
_REPORT_REJECTIONS = {
    "report_running": "A report is already being written for this meeting.",
    "meeting_not_ready": (
        "Reports are written once the meeting has ended and its insights "
        "have finished."
    ),
    "report_limit_reached": (
        "This meeting is holding the maximum number of reports. Delete one "
        "to make room."
    ),
    "invalid_request": "Describe the report you want.",
    "request_too_long": "That request is too long.",
    "host_only": "Only the meeting host can request a report.",
    "persistence_error": "The report could not be saved. Retry.",
}

_TRANSCRIPT_PAGE_DEFAULT = 500
_TRANSCRIPT_PAGE_MAX = 1000
_HISTORY_PAGE_DEFAULT = 50
_HISTORY_PAGE_MAX = 100


def _encode_cursor(item: Dict[str, Any]) -> str:
    raw = json.dumps(
        [float(item.get("start_s", 0.0)), str(item.get("id", ""))],
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _history_cursor(meeting: Dict[str, Any]) -> str:
    raw = json.dumps([meeting["started_at"], meeting["id"]], separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode("ascii").rstrip("=")


def _decode_history_cursor(cursor: str) -> tuple[Optional[str], Optional[str]]:
    if not cursor:
        return None, None
    try:
        if len(cursor) > 1024:
            raise ValueError("cursor too long")
        values = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        if not isinstance(values, list) or len(values) != 2:
            raise ValueError("invalid cursor shape")
        started_at, meeting_id = values
        if not isinstance(started_at, str) or not isinstance(meeting_id, str):
            raise ValueError("invalid cursor fields")
        return started_at, meeting_id
    except Exception as exc:
        raise HTTPException(status_code=400, detail="invalid meeting cursor") from exc


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


#: Meeting statuses whose clock is still running (no ``ended_at`` yet).
_RUNNING_STATUSES = {"active", "paused", "ending"}
#: Avatars shown per History row; the full count travels separately.
_DIGEST_PARTICIPANTS = 6


def _cloud_consent_given() -> bool:
    """Whether the desktop consent dialog for AI insights was accepted."""
    try:
        from services.settings import resolve_meeting_cloud_consent
    except Exception:
        logger.exception("Could not read AI insights consent")
        return False
    return resolve_meeting_cloud_consent()


def _remember_cloud_choice(enabled: bool) -> None:
    """Persist the toggle as the next meeting's default, like the desktop."""
    try:
        from services.settings import SettingsKey, settings_manager

        settings_manager.save_setting(
            SettingsKey.MEETING_CLOUD_LAST_ENABLED, bool(enabled),
        )
    except Exception:
        logger.warning("Could not persist the AI insights toggle", exc_info=True)


def _meeting_digest(meeting: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Counts and people for a History row, read from the saved snapshot.

    Never raises: a missing or corrupt snapshot just omits the digest.
    """
    state = parse_state_json(meeting.get("state_json"))
    if state is None:
        return None
    try:
        cards = state.get("cards") if isinstance(state.get("cards"), dict) else {}

        def live_count(key: str) -> int:
            items = cards.get(key)
            if not isinstance(items, list):
                return 0
            return sum(
                1 for item in items
                if isinstance(item, dict) and item.get("status") != "removed"
            )

        questions = state.get("questions")
        if isinstance(questions, dict):
            questions = list(questions.values())
        open_questions = sum(
            1 for q in (questions if isinstance(questions, list) else [])
            if isinstance(q, dict) and q.get("status") == "open"
        )
        raw_people = state.get("participants")
        people = [
            p for p in (raw_people.values() if isinstance(raw_people, dict) else [])
            if isinstance(p, dict) and p.get("id")
        ]
        # "Me" first, then join order, so avatar colors match the live view.
        people.sort(key=lambda p: (
            p.get("kind") != "me", str(p.get("created_at") or ""), str(p.get("id")),
        ))
        return {
            "decisions": live_count("decisions"),
            "action_items": live_count("action_items"),
            "risks": live_count("risks"),
            "open_questions": open_questions,
            "participant_count": len(people),
            "participants": [
                {
                    "id": str(p.get("id")),
                    "display_name": str(p.get("display_name") or ""),
                    "kind": str(p.get("kind") or ""),
                    "created_at": str(p.get("created_at") or ""),
                }
                for p in people[:_DIGEST_PARTICIPANTS]
            ],
        }
    except Exception:
        logger.debug("Could not build History digest for %s", meeting.get("id"),
                     exc_info=True)
        return None


def _public_meeting(
    meeting: Optional[Dict[str, Any]],
    repository: Optional[Any] = None,
) -> Dict[str, Any]:
    """Strip a repository meeting dict down to client-safe fields."""
    if not meeting:
        return {}
    public = {key: meeting.get(key) for key in _PUBLIC_MEETING_KEYS}
    public["display_title"] = meeting_display_title(meeting)
    public["duration_s"] = meeting_duration_s(
        meeting, running=str(meeting.get("status") or "") in _RUNNING_STATUSES,
    )
    digest = _meeting_digest(meeting)
    if digest is not None:
        public["digest"] = digest
    public.update(compact_finalization_list_fields(meeting))
    summary = meeting.get("content_summary")
    if isinstance(summary, dict) or repository is not None:
        if not isinstance(summary, dict):
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


#: Served in place of the dashboard when ``webui/dist`` is missing. The
#: bundle is tracked in git and shipped with the app, so this only shows in
#: a checkout whose build output was deleted.
_MISSING_BUNDLE_PAGE = (
    "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
    "<title>OpenWhisper Meeting</title></head><body>"
    "<p>The dashboard bundle is missing; run "
    "<code>npm run build --prefix webui</code>.</p></body></html>"
)


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
    review_stores: Dict[str, Any] = {}

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
        """A past meeting's persisted state document (blocking)."""
        return load_state(meeting, meeting_id, historical=True).to_dict()

    async def _state_for(meeting_id: str,
                         meeting: Dict[str, Any]) -> Dict[str, Any]:
        """Live snapshot for the current meeting, stored snapshot otherwise.

        Both branches block (store lock / JSON parse of a whole document), so
        both run on a worker thread.
        """
        store = getattr(engine, "store", None)
        if store is not None and getattr(engine, "meeting_id", None) == meeting_id:
            return await asyncio.to_thread(store.snapshot)
        if meeting_id in review_stores:
            return await asyncio.to_thread(review_stores[meeting_id].snapshot)
        return await asyncio.to_thread(_stored_state, meeting_id, meeting)

    async def _json_body(request: Request) -> Dict[str, Any]:
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="invalid JSON body")
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="body must be an object")
        return body

    @app.get("/m/{token}", response_class=HTMLResponse)
    async def dashboard(token: str) -> Response:
        role = await _resolve(token)
        if role is None:
            raise HTTPException(status_code=403, detail="invalid or expired link")
        index_path = os.path.join(dist_dir, "index.html")
        if os.path.isfile(index_path):
            return FileResponse(index_path, media_type="text/html")
        return HTMLResponse(_MISSING_BUNDLE_PAGE)

    @app.websocket("/ws")
    async def ws_endpoint(websocket: WebSocket) -> None:
        await hub.handle_connection(websocket)

    @app.get("/api/session")
    async def api_session(token: str = "") -> Dict[str, Any]:
        role = await _require(token)
        meeting = await _current_meeting()
        store = getattr(engine, "store", None)
        state = await asyncio.to_thread(store.snapshot) if store is not None else {}
        if role != "host":
            # Same rule as the WS hello and fan-out: tailored reports may draw
            # on material outside this meeting, so guests never receive them.
            state = {key: value for key, value in state.items()
                     if key != "custom_reports"}
        from meeting.voice_help import voice_command_guide
        guide = await asyncio.to_thread(voice_command_guide)
        return {"role": role, "meeting": _public_meeting(meeting), "state": state,
                "voice_commands": guide}

    @app.get("/api/transcript")
    async def api_transcript(token: str = "", cursor: str = "",
                             limit: int = _TRANSCRIPT_PAGE_DEFAULT) -> Dict[str, Any]:
        await _require(token)
        meeting_id = getattr(engine, "meeting_id", None)
        if not meeting_id:
            return {"items": [], "next_cursor": None}
        return await _transcript_page(meeting_id, cursor, limit)

    @app.get("/api/meetings")
    async def api_meetings(token: str = "", cursor: str = "",
                           limit: int = _HISTORY_PAGE_DEFAULT, q: str = "",
                           since: str = "", has_decisions: bool = False,
                           has_actions: bool = False,
                           needs_attention: bool = False) -> Dict[str, Any]:
        await _require(token, host_only=True)
        started_at, meeting_id = _decode_history_cursor(cursor)
        page_limit = max(1, min(limit, _HISTORY_PAGE_MAX))
        rows = await asyncio.to_thread(
            repository.list_past_meeting_summaries, limit=page_limit + 1,
            query=q, cursor_started_at=started_at, cursor_id=meeting_id,
            include_running=True, started_after=since,
        )
        has_more = len(rows) > page_limit
        page = rows[:page_limit]
        # Digest filters inspect one bounded window. A continuation is returned
        # even for an empty filtered page, so older matches remain reachable.
        def public_page() -> list[Dict[str, Any]]:
            result = []
            for row in page:
                public = _public_meeting(row)
                digest = public.get("digest") or {}
                if has_decisions and not digest.get("decisions"):
                    continue
                if has_actions and not digest.get("action_items"):
                    continue
                if needs_attention and public.get("insights_tone") != "warning":
                    continue
                result.append(public)
            return result
        return {
            "meetings": await asyncio.to_thread(public_page),
            "next_cursor": _history_cursor(page[-1]) if has_more and page else None,
        }

    @app.get("/api/meetings/{meeting_id}")
    async def api_meeting_detail(meeting_id: str, token: str = "",
                                 include_transcript: bool = True) -> Dict[str, Any]:
        await _require(token, host_only=True)
        meeting = await asyncio.to_thread(repository.get_meeting, meeting_id)
        if meeting is None:
            raise HTTPException(status_code=404, detail="unknown meeting")
        transcript = (await _transcript_page(
            meeting_id, "", _TRANSCRIPT_PAGE_DEFAULT
        )) if include_transcript else {"items": [], "next_cursor": None}
        public_meeting = await asyncio.to_thread(
            _public_meeting, meeting, repository
        )
        return {
            "meeting": public_meeting,
            "state": await _state_for(meeting_id, meeting),
            "segments": transcript["items"],
            "transcript_next_cursor": transcript["next_cursor"],
            "transcript_included": include_transcript,
        }

    @app.get("/api/meetings/{meeting_id}/state")
    async def api_meeting_state(meeting_id: str, token: str = "") -> Dict[str, Any]:
        """Follow report/review progress without transcript or content queries."""
        await _require(token, host_only=True)
        meeting = await asyncio.to_thread(repository.get_meeting, meeting_id)
        if meeting is None:
            raise HTTPException(status_code=404, detail="unknown meeting")
        return {"state": await _state_for(meeting_id, meeting)}

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

    async def retitle_saved_meeting(meeting_id: str, title: str) -> Dict[str, Any]:
        """Shared local action; callers establish authorization before dispatch."""
        try:
            title = normalize_title(title)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
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
            if meeting_id in review_stores:
                results = await asyncio.to_thread(review_stores[meeting_id].apply, "host", None,
                                                  [{"op": "set_title", "text": title}])
                ok = bool(results and results[0].ok)
            else:
                await asyncio.to_thread(repository.rename_meeting, meeting_id, title)
                ok = True
        return {"ok": ok, "title": title}

    async def refresh_saved_meeting_title(meeting_id: str) -> None:
        store = getattr(engine, "store", None)
        if store is not None and getattr(engine, "meeting_id", None) == meeting_id:
            changed = await asyncio.to_thread(store.refresh_title)
            if changed:
                # A reconnect sends a full hello without inventing an audit seq.
                hub.schedule_invalidate_connections(resync=True)
        cached = review_stores.get(meeting_id)
        if cached is not None:
            await asyncio.to_thread(cached.refresh_title)

    app.state.retitle_saved_meeting = retitle_saved_meeting
    app.state.refresh_saved_meeting_title = refresh_saved_meeting_title

    @app.post("/api/meetings/{meeting_id}/rename")
    async def api_rename_meeting(meeting_id: str, request: Request,
                                 token: str = "") -> Dict[str, Any]:
        await _require(token, host_only=True)
        body = await _json_body(request)
        return await retitle_saved_meeting(meeting_id, body.get("title"))

    @app.post("/api/meetings/{meeting_id}/review")
    async def api_insight_review(meeting_id: str, request: Request, token: str = "") -> Dict[str, Any]:
        await _require(token, host_only=True)
        body = await _json_body(request)
        action = body.get("op")
        if action not in ("start", "review_answer", "review_skip", "review_reopen"):
            raise HTTPException(status_code=400, detail="invalid review action")
        if meeting_id in insights_running:
            raise HTTPException(status_code=409, detail="Wait for final insights to finish")
        meeting = await asyncio.to_thread(repository.get_meeting, meeting_id)
        if meeting is None:
            raise HTTPException(status_code=404, detail="unknown meeting")
        from meeting.insight_review import start_review
        store = getattr(engine, "store", None) if getattr(engine, "meeting_id", None) == meeting_id else None
        if store is None:
            if meeting_id not in review_stores:
                review_stores[meeting_id] = open_store(
                    repository, meeting_id, meeting, historical=True)
            store = review_stores[meeting_id]
        if meeting_id in insights_running:
            raise HTTPException(status_code=409, detail="A meeting update is already running")
        insights_running.add(meeting_id)
        try:
            if action == "start":
                result = await asyncio.to_thread(start_review, store, repository)
            else:
                results = await asyncio.to_thread(store.apply, "host", None, [body])
                result = {"ok": bool(results and results[0].ok),
                          "error": results[0].reason if results and not results[0].ok else None}
            return {**result, "state": await asyncio.to_thread(store.snapshot)}
        finally:
            insights_running.discard(meeting_id)

    def _report_in_flight(store: Any) -> bool:
        """True while a tailored report is being written against this meeting.

        A finalization re-run replaces the whole state document, so it must
        not start while a report worker is about to write into it.
        """
        if store is None:
            return False
        return any(
            report.get("status") == "running"
            for report in store.snapshot().get("custom_reports") or []
        )

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
        from services.meeting_rerun import rerun_options

        options = await asyncio.to_thread(rerun_options, meeting)
        # Check-and-claim with no await between: a double-click cannot start
        # two agent cores writing the same past meeting.
        if meeting_id in insights_running:
            raise HTTPException(
                status_code=409,
                detail="insights are already running for this meeting",
            )
        live_review_store = (getattr(engine, "store", None)
                             if getattr(engine, "meeting_id", None) == meeting_id else review_stores.get(meeting_id))
        if live_review_store is not None:
            live_snapshot = live_review_store.snapshot()
            if (live_snapshot.get("finalization") or {}).get("status") == "running":
                raise HTTPException(status_code=409, detail="Wait for the post-meeting steps to finish")
            if (live_snapshot.get("insight_review") or {}).get("status") == "running":
                raise HTTPException(status_code=409, detail="Wait for insight review to finish")
        if _report_in_flight(live_review_store):
            raise HTTPException(status_code=409, detail="Wait for the report being written to finish")
        review_stores.pop(meeting_id, None)
        insights_running.add(meeting_id)
        try:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                insights_executor,
                functools.partial(
                    rerun_finalization, repository, meeting_id,
                    from_step="failed",
                    model_lease=getattr(engine, "model_lease", None),
                    **options,
                ),
            )
            replace_state = getattr(live_review_store, "replace_document", None)
            if callable(replace_state) and result.get("state"):
                replace_state(MeetingState.from_dict(result["state"]))
            return result
        except FinalizationBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
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
        from services.meeting_rerun import resolve_speaker_pass

        gate = await asyncio.to_thread(resolve_speaker_pass)
        if not gate.ok:
            raise HTTPException(status_code=400, detail=gate.reason)
        if meeting_id in insights_running:
            raise HTTPException(
                status_code=409,
                detail="a post-meeting pass is already running for this meeting",
            )
        store = (getattr(engine, "store", None) if getattr(engine, "meeting_id", None) == meeting_id
                 else review_stores.get(meeting_id))
        if store and store.snapshot().get("insight_review", {}).get("status") == "running":
            raise HTTPException(status_code=409, detail="Wait for insight review to finish")
        if _report_in_flight(store):
            raise HTTPException(status_code=409, detail="Wait for the report being written to finish")
        review_stores.pop(meeting_id, None)
        insights_running.add(meeting_id)
        try:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                insights_executor,
                functools.partial(
                    rerun_speakers, repository, meeting_id,
                    gate=gate, store=store,
                    spool_dir=meeting.get("spool_dir"),
                ),
            )
        except FinalizationBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
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
        store = (getattr(engine, "store", None) if getattr(engine, "meeting_id", None) == meeting_id
                 else review_stores.get(meeting_id))
        if meeting_id in insights_running or (store and store.snapshot().get("insight_review", {}).get("status") == "running"):
            raise HTTPException(status_code=409, detail="Wait for the meeting update to finish before deleting")
        review_stores.pop(meeting_id, None)
        meeting = await asyncio.to_thread(repository.get_meeting, meeting_id)
        if meeting is None:
            raise HTTPException(status_code=404, detail="unknown meeting")
        options = getattr(engine, "options", None)
        meetings_root = getattr(options, "spool_root", None) or config.MEETINGS_FOLDER
        await asyncio.to_thread(
            delete_meeting_data, repository, meeting_id, meetings_root
        )
        return {"ok": True}

    @app.get("/api/search")
    async def api_search(token: str = "", q: str = "", mode: str = "keyword") -> Dict[str, Any]:
        await _require(token, host_only=True)
        if mode not in ("keyword", "semantic"):
            raise HTTPException(status_code=400, detail="invalid search mode")
        if mode == "keyword":
            return {"results": await asyncio.to_thread(repository.search_transcripts, q), "mode": "keyword", "message": ""}
        from meeting.semantic_search import search_history
        return await asyncio.to_thread(search_history, repository, q, semantic=mode == "semantic")

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
        # Keep host export metadata; JSON strips capabilities and volatile fields.
        content = await asyncio.to_thread(
            exporter, {**meeting, **_public_meeting(meeting)}, state, segments
        )
        filename = f"meeting-{target_id}.{extension}"
        return Response(
            content=content,
            media_type=media_type,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
            },
        )


    def _report_store(meeting_id: str, meeting: Dict[str, Any]) -> Any:
        """The one store allowed to write this meeting's state.

        The live engine owns the current meeting; a past meeting reuses the
        cached review store so a report and an insight review can never be
        two writers racing over one ``state_json``.
        """
        store = getattr(engine, "store", None)
        if store is not None and getattr(engine, "meeting_id", None) == meeting_id:
            return store
        if meeting_id not in review_stores:
            review_stores[meeting_id] = open_store(
                repository, meeting_id, meeting, historical=True
            )
        return review_stores[meeting_id]

    def _report_endpoint(meeting: Dict[str, Any]) -> Dict[str, Any]:
        """Provider, model, and endpoint for a report on this meeting.

        The meeting's own recorded endpoint wins so a report matches the
        intelligence that produced the record; the engine's current options
        fill in for meetings recorded with AI insights off.
        """
        options = getattr(engine, "options", None)
        from services.settings import MeetingAgentCore

        kind = getattr(options, "agent_core_kind", "")
        if kind in MeetingAgentCore.INSTALLED:
            # The meeting's installed agent writes the report too.
            return {"provider": kind, "model": getattr(options, "llm_model", "") or "",
                    "endpoint": None}
        provider = (meeting.get("agent_provider")
                    or getattr(options, "llm_provider", "") or "openrouter")
        model = (meeting.get("agent_model")
                 or getattr(options, "llm_model", "") or "")
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
        return {"provider": provider, "model": model, "endpoint": endpoint}

    def _find_report(state: Dict[str, Any], report_id: str) -> Dict[str, Any]:
        for report in state.get("custom_reports") or []:
            if report.get("id") == report_id:
                return report
        raise HTTPException(status_code=404, detail="unknown report")

    @app.post("/api/meetings/{meeting_id}/reports")
    async def api_request_report(meeting_id: str, request: Request,
                                 token: str = "") -> Dict[str, Any]:
        """Ask for a report written to the requester's own description."""
        await _require(token, host_only=True)
        body = await _json_body(request)
        text = body.get("request")
        if (not isinstance(text, str) or not text.strip()
                or len(text) > MAX_REQUEST_CHARS):
            raise HTTPException(
                status_code=400,
                detail=(
                    "Describe the report you want, in 1 to "
                    f"{MAX_REQUEST_CHARS} characters."
                ),
            )
        meeting = await asyncio.to_thread(repository.get_meeting, meeting_id)
        if meeting is None:
            raise HTTPException(status_code=404, detail="unknown meeting")
        if meeting_id in insights_running:
            raise HTTPException(
                status_code=409,
                detail="Wait for the meeting's insights to finish",
            )
        store = await asyncio.to_thread(_report_store, meeting_id, meeting)
        endpoint = _report_endpoint(meeting)
        result = await asyncio.to_thread(
            functools.partial(
                start_custom_report, store, repository, text,
                provider=endpoint["provider"], model=endpoint["model"],
                endpoint=endpoint["endpoint"],
            ),
        )
        if not result["ok"]:
            raise HTTPException(
                status_code=409,
                detail=_REPORT_REJECTIONS.get(
                    result["error"] or "",
                    "The report could not be started.",
                ),
            )
        return {**result, "state": await asyncio.to_thread(store.snapshot)}

    @app.delete("/api/meetings/{meeting_id}/reports/{report_id}")
    async def api_delete_report(meeting_id: str, report_id: str,
                                token: str = "") -> Dict[str, Any]:
        """Discard a report, including one stranded by an interrupted run."""
        await _require(token, host_only=True)
        meeting = await asyncio.to_thread(repository.get_meeting, meeting_id)
        if meeting is None:
            raise HTTPException(status_code=404, detail="unknown meeting")
        store = await asyncio.to_thread(_report_store, meeting_id, meeting)
        results = await asyncio.to_thread(
            store.apply, "host", None,
            [{"op": "remove_custom_report", "report_id": report_id}],
        )
        if not results or not results[0].ok:
            reason = results[0].reason if results else "rejected"
            status = 404 if reason == "unknown_report" else 409
            raise HTTPException(status_code=status, detail=reason)
        return {"ok": True, "state": await asyncio.to_thread(store.snapshot)}

    @app.get("/api/meetings/{meeting_id}/reports/{report_id}/download")
    async def api_download_report(meeting_id: str, report_id: str,
                                  token: str = "") -> Response:
        """Download one finished report as a Markdown file."""
        await _require(token, host_only=True)
        meeting = await asyncio.to_thread(repository.get_meeting, meeting_id)
        if meeting is None:
            raise HTTPException(status_code=404, detail="unknown meeting")
        state = await _state_for(meeting_id, meeting)
        report = _find_report(state, report_id)
        if report.get("status") != "ready":
            raise HTTPException(status_code=409, detail="report is not ready")
        return Response(
            content=report.get("markdown") or "",
            media_type="text/markdown",
            headers={
                "Content-Disposition":
                    f'attachment; filename="report-{report_id}.md"',
            },
        )

    @app.post("/api/meeting/notes/request")
    async def api_note_request(request: Request, token: str = "") -> Dict[str, Any]:
        await _require(token)
        body = await _json_body(request)
        text = body.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > 4000:
            raise HTTPException(status_code=400, detail="Enter a note request of 1 to 4000 characters.")
        try:
            future = await asyncio.to_thread(engine.request_note_adjustment, text)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        # The existing scheduler owns execution. Awaiting its Future does not
        # occupy a server worker for the duration of the model call.
        result = await asyncio.shield(asyncio.wrap_future(future))
        return {
            "ok": result.ok,
            "applied": sum(1 for op in result.op_results if op.ok),
            "rejected": sum(1 for op in result.op_results if not op.ok),
            "error": result.error,
        }

    @app.post("/api/meeting/end")
    async def api_meeting_end(token: str = "") -> Dict[str, Any]:
        await _require(token, host_only=True)
        requested = time.perf_counter()
        started: list = []

        def _end() -> None:
            started.append(time.perf_counter())
            engine.end()

        await asyncio.to_thread(_end)
        elapsed = time.perf_counter() - requested
        if elapsed > _SLOW_END_WARN_S:
            # Split the wait so a saturated thread pool is told apart from
            # an engine.end() that blocks (it should only spawn its worker).
            queued = (started[0] - requested) if started else elapsed
            logger.warning(
                "Meeting end request took %.2fs (%.2fs waiting for a worker "
                "thread, %.2fs in engine.end)",
                elapsed, queued, elapsed - queued,
            )
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
        if enabled and not await asyncio.to_thread(_cloud_consent_given):
            raise HTTPException(
                status_code=403,
                detail=(
                    "Turn on AI insights once from the desktop app first; it "
                    "asks for consent before meeting text is sent to the AI "
                    "provider."
                ),
            )
        await asyncio.to_thread(engine.set_cloud_enabled, enabled)
        await asyncio.to_thread(_remember_cloud_choice, enabled)
        return {"ok": True, "enabled": enabled}

    @app.post("/api/meeting/tokens/regenerate")
    async def api_regenerate_tokens(token: str = "") -> Dict[str, Any]:
        await _require(token, host_only=True)
        result = await asyncio.to_thread(engine.regenerate_tokens)
        return {"ok": True, **result}

    return app
