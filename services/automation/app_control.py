"""The desktop app's AppControl for the live API (stream A fills this in).

Contract: docs/design/automation/design.md, "services/automation/control.py".
"""

from __future__ import annotations


def install(controller) -> None:
    """Register this app's control with ``control.set_app_control``; Qt thread."""
