"""Live API routes on the in-app listener (stream A fills this in).

Contract: docs/design/automation/design.md, "services/automation/api.py".
Mounted by services/agent_mcp/app.py only; the headless ``--api`` never
serves these routes because there is no running app behind it.
"""

from __future__ import annotations


def create_live_router(settings):
    """The live routes; ``settings`` is the app's SettingsManager."""
    from fastapi import APIRouter

    from services.automation import permissions
    from services.automation.control import ControlError, require_app_control

    router = APIRouter()

    @router.get("/v1/live/state", operation_id="get_live_state")
    def live_state():
        saved = settings.load_all_settings() if settings is not None else {}
        try:
            state = require_app_control().state().to_dict()
        except ControlError as exc:
            from services.agent_api.app import _error

            return _error(503, exc.code, exc.message)
        return {**state, "permissions": permissions.describe(saved)}

    return router
