"""Starts and stops the background parts of automation with the app."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_started = False


def start(controller) -> None:
    """After the window shows: control for the live API, then hook delivery."""
    global _started
    if _started:
        return
    _started = True
    try:
        from services.automation import app_control

        app_control.install(controller)
    except Exception:
        logger.exception("Could not register live API control")
    for name, starter in (
        ("webhooks", _start_webhooks),
        ("programs", _start_programs),
        ("MCP Events", _start_mcp_events),
    ):
        try:
            starter()
        except Exception:
            logger.exception("Could not start %s", name)


def _start_webhooks() -> None:
    from services.automation import webhooks

    webhooks.start()


def _start_programs() -> None:
    from services.automation import programs

    programs.start()


def _start_mcp_events() -> None:
    from services.agent_mcp import events_ext

    events_ext.start_delivery()


def reload(kind: str) -> None:
    """Settings changed ``kind``: webhooks, programs, filters or mcp_events."""
    try:
        if kind == "webhooks":
            from services.automation import webhooks

            webhooks.reload()
        elif kind == "programs":
            from services.automation import programs

            programs.reload()
        elif kind == "mcp_events":
            from services.agent_mcp import events_ext

            events_ext.reload()
    except Exception:
        logger.exception("Could not apply %s changes", kind)


def stop() -> None:
    global _started
    if not _started:
        return
    _started = False
    for name, module in (
        ("webhooks", "services.automation.webhooks"),
        ("programs", "services.automation.programs"),
        ("MCP Events", "services.agent_mcp.events_ext"),
    ):
        try:
            import importlib

            mod = importlib.import_module(module)
            (getattr(mod, "stop_delivery", None) or mod.stop)()
        except Exception:
            logger.debug("Could not stop %s", name, exc_info=True)
    try:
        from services.automation.control import set_app_control

        set_app_control(None)
    except Exception:
        logger.debug("Could not clear live API control", exc_info=True)
