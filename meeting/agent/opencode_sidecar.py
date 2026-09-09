"""OpenCode v2 embedded SDK, isolated in a supervised Bun process."""
from __future__ import annotations

import os
import json
from pathlib import Path
import shutil
import tempfile
from typing import Any

from meeting.agent.sidecar import SidecarAgent
from meeting.interfaces import AgentConfig, AgentToolHost

from services.opencode_catalog import SDK_VERSION, BUN_VERSION


class OpenCodeSidecarAgent(SidecarAgent):
    """Use the common meeting protocol with strict request and version checks."""

    require_request_scope = True

    def __init__(self, payload_dir: str):
        super().__init__(payload_dir)
        self._runtime_root: str | None = None
        self._release_component = None

    def initialize(self, cfg: AgentConfig, tools: AgentToolHost) -> None:
        from services.components import current_platform_tag
        if current_platform_tag() != "win_amd64":
            raise RuntimeError("OpenCode v2 beta currently supports Windows x64 only.")
        from services.component_leases import acquire_component
        from services.components import ComponentId
        from services.opencode_component import clean_stale_runtime_dirs
        clean_stale_runtime_dirs()
        self._release_component = acquire_component(ComponentId.MEETING_AGENT_OPENCODE)
        try:
            self._runtime_root = tempfile.mkdtemp(prefix=f"openwhisper-opencode-{os.getpid()}-")
            (Path(self._runtime_root) / ".openwhisper-owner.json").write_text(
                json.dumps({"application": "OpenWhisper", "pid": os.getpid()}), encoding="utf-8")
            super().initialize(cfg, tools)
        except BaseException:
            self.shutdown()
            raise

    def _bundle_path(self) -> str:
        return os.path.join(self._payload_dir, "main.mjs")

    def _resolve_node_cmd(self) -> list[str]:
        runtime = os.path.join(self._payload_dir, "bun.exe")
        if not os.path.isfile(runtime) or not os.path.isfile(self._bundle_path()):
            raise RuntimeError("OpenCode runtime is missing. Install OpenCode v2 (beta) from Downloads.")
        return [runtime, "--no-install", self._bundle_path()]

    def _validate_hello(self, params: dict[str, Any]) -> bool:
        protocols = params.get("text_protocols")
        return (
            isinstance(protocols, list) and all(isinstance(p, str) for p in protocols)
            and params.get("harness") == "opencode"
            and params.get("harness_version") == SDK_VERSION
            and params.get("runtime_version") == BUN_VERSION
            and params.get("request_scoped_tools") == 1
            and params.get("host_prompt") == 1
            and set(protocols) == {"chat", "responses", "anthropic", "google"}
        )

    def _check_sidecar_text_support(self) -> None:
        if self._endpoint_fields()["model_metadata"]["protocol"] not in self._text_protocols:
            raise RuntimeError("Update OpenCode from Downloads to use this text provider.")

    def _validate_new_provider_tools(self, api_key: str) -> None:
        # Actual SDK tool calls validate capability; no separate model probe.
        pass

    def _process_cwd(self) -> str | None:
        return self._runtime_root

    def _build_env(self, api_key: str) -> dict[str, str]:
        env = super()._build_env(api_key)
        from services.opencode_component import isolated_environment
        assert self._runtime_root is not None
        return isolated_environment(env, self._runtime_root)

    def shutdown(self) -> None:
        try:
            super().shutdown()
        finally:
            if self._runtime_root:
                shutil.rmtree(self._runtime_root, ignore_errors=True)
                self._runtime_root = None
            if self._release_component:
                self._release_component()
                self._release_component = None
