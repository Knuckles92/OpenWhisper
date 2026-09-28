"""Read/edit adapter for serving persisted meetings without a live recorder."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Tuple

from meeting.interfaces import OpResult
from meeting.stored import open_store, stored_endpoint
from meeting.web.auth import generate_token_pair


class ArchivedMeetingDashboard:
    """Minimal engine-shaped object used by ``MeetingWebServer`` for history.

    The web layer was intentionally built against a small engine surface. This
    adapter supplies that surface from a persisted state snapshot, allowing the
    dashboard to remain useful after an application restart without starting
    capture, ASR, diarization, or an agent.
    """

    def __init__(
        self,
        repository: Any,
        meeting: Dict[str, Any],
        *,
        spool_root: str,
        llm_provider: str = "openrouter",
        llm_model: str = "",
        llm_endpoint: Optional[Dict[str, Any]] = None,
        agent_core_kind: str = "pi",
        sidecar_payload_dir: Optional[str] = None,
        model_lease: Optional[Tuple[Callable[[], bool], Callable[[], None]]] = None,
    ) -> None:
        self.repository = repository
        self.meeting_id = str(meeting["id"])
        self.model_lease = model_lease
        from services.settings import MeetingAgentCore

        # An installed agent brings its own model; the row's text endpoint
        # would only mislead it.
        installed = agent_core_kind in MeetingAgentCore.INSTALLED
        self.options = SimpleNamespace(
            spool_root=spool_root,
            llm_provider=llm_provider if installed else (
                meeting.get("agent_provider") or llm_provider),
            llm_model=llm_model if installed else (meeting.get("agent_model") or llm_model),
            llm_endpoint=None if installed else (stored_endpoint(meeting) or llm_endpoint),
            agent_core_kind=agent_core_kind,
            sidecar_payload_dir=sidecar_payload_dir,
        )
        # Historical dashboards must never claim work is still in flight.
        self.store = open_store(repository, self.meeting_id, meeting, historical=True)
        self._server = None

    def attach_server(self, server: Any) -> None:
        """Attach the server after construction so token rotation can update URLs."""
        self._server = server

    @property
    def host_url(self) -> str:
        """Return the current host capability URL, if the server is running."""
        return self._server.host_url if self._server is not None else ""

    def is_running(self) -> bool:
        """Return whether the attached archive server is accepting requests."""
        return bool(self._server is not None and self._server.is_running())

    def is_active(self) -> bool:
        """Archived dashboards never own an active recording session."""
        return False

    def apply_client_action(
        self, actor_type: str, actor_id: Optional[str], op: Dict[str, Any]
    ) -> List[OpResult]:
        """Apply dashboard edits through the normal persisted state store."""
        return self.store.apply(actor_type, actor_id, [dict(op)])

    def undo(self, seq: int, actor_id: Optional[str]) -> List[OpResult]:
        """Undo a persisted dashboard edit by event sequence."""
        return self.store.undo(seq, actor_id)

    def add_guest(self, display_name: str) -> Dict[str, Any]:
        """Create a guest participant if an archived guest URL is opened."""
        name = (display_name or "").strip()[:80] or "Guest"
        result = self.store.apply("system", None, [{
            "op": "upsert_participant",
            "display_name": name,
            "kind": "guest",
            "is_provisional": False,
        }])[0]
        if not result.ok or not result.effect:
            raise ValueError(result.reason or "guest_rejected")
        return dict(result.effect["participant"])

    def get_transcript(
        self, after_start_s: float = -1.0, limit: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """Return transcript segments for the selected archived meeting."""
        return self.repository.get_segments(
            self.meeting_id, after_start_s=after_start_s, limit=limit
        )

    def regenerate_tokens(self) -> Dict[str, str]:
        """Rotate capability links for the meeting served by this adapter."""
        host_token, guest_token = generate_token_pair()
        self.repository.replace_tokens(self.meeting_id, host_token, guest_token)
        server = self._server
        if server is not None:
            server.invalidate_connections()
        return {
            "host_url": server.host_url if server is not None else "",
            "guest_url": server.guest_url if server is not None else "",
        }

    def end(self) -> None:
        """Ignore live-session controls for an already-ended meeting."""

    def pause(self) -> None:
        """Ignore live-session controls for an already-ended meeting."""

    def resume(self) -> None:
        """Ignore live-session controls for an already-ended meeting."""

    def set_cloud_enabled(self, enabled: bool) -> None:
        """Reject cloud toggles because no intelligence worker is running."""
        raise RuntimeError("AI insights are unavailable for archived playback")

    def shutdown(self) -> None:
        """Stop the attached history dashboard server."""
        server, self._server = self._server, None
        if server is not None:
            server.stop()
