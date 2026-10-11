"""Actions, live events and hooks: OpenWhisper's local automation surface.

See docs/design/automation/design.md. Modules here stay light to import;
anything that needs fastapi, httpx or subprocess imports it on use.
"""
