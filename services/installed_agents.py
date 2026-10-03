"""Coding agents the user already has installed: Claude Code, Codex, OpenCode.

Finds each command-line agent (on PATH and in the folders its installers use),
reads its version and sign-in without starting anything long-lived, and lists
the models it offers. Nothing here runs a model: Meeting Mode drives the agent
through :mod:`meeting.agent.installed`, and Settings shows what was found.

Every probe runs with a short timeout and never raises, so a broken install
reads as "not found" or as a problem to show, not as an error.
"""
from __future__ import annotations

import glob
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from services.settings import MeetingAgentCore

logger = logging.getLogger(__name__)

CLAUDE_CODE = MeetingAgentCore.CLAUDE_CODE
CODEX = MeetingAgentCore.CODEX
OPENCODE = MeetingAgentCore.OPENCODE_CLI
#: Display order in Settings.
AGENT_ORDER: Tuple[str, ...] = (CLAUDE_CODE, CODEX, OPENCODE)

_VERSION_TIMEOUT_S = 6.0
_AUTH_TIMEOUT_S = 10.0
_VERSION_RE = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?(?:-([0-9A-Za-z.\-]+))?")


@dataclass(frozen=True)
class AgentSpec:
    """What OpenWhisper knows about one agent before looking for it.

    Attributes:
        id: ``MeetingAgentCore`` value.
        name: Product name shown to the user.
        vendor: Who makes it, for the privacy line.
        commands: Executable names looked up on PATH.
        transport: ``headless`` (one process per pass) or ``acp``.
        install_url: Where to get it.
        install_hint: One-line install command.
        monogram: Two letters for the tile mark.
        accent: Tile accent colour (hex).
        min_version: Oldest version that can run meeting passes.
        update_hint: How to update it, when too old.
    """

    id: str
    name: str
    vendor: str
    commands: Tuple[str, ...]
    transport: str
    install_url: str
    install_hint: str
    monogram: str
    accent: str
    min_version: Tuple[int, ...] = ()
    update_hint: str = ""


AGENT_SPECS: Dict[str, AgentSpec] = {
    CLAUDE_CODE: AgentSpec(
        id=CLAUDE_CODE,
        name="Claude Code",
        vendor="Anthropic",
        commands=("claude",),
        transport="headless",
        install_url="https://docs.anthropic.com/en/docs/claude-code/setup",
        install_hint="npm install -g @anthropic-ai/claude-code",
        monogram="CC",
        accent="#D97757",
        min_version=(2, 0, 0),
        update_hint="Run `claude update`.",
    ),
    CODEX: AgentSpec(
        id=CODEX,
        name="Codex",
        vendor="OpenAI",
        commands=("codex",),
        transport="headless",
        install_url="https://developers.openai.com/codex/cli",
        install_hint="npm install -g @openai/codex",
        monogram="Cx",
        accent="#10A37F",
        min_version=(0, 120, 0),
        update_hint="Run `npm install -g @openai/codex` or update the Codex app.",
    ),
    OPENCODE: AgentSpec(
        id=OPENCODE,
        name="OpenCode",
        vendor="OpenCode",
        commands=("opencode",),
        transport="acp",
        install_url="https://opencode.ai/docs/",
        install_hint="npm install -g opencode-ai",
        monogram="OC",
        accent="#6D5DD3",
        min_version=(2, 0, 0),
        update_hint="Run `opencode upgrade`.",
    ),
}


@dataclass(frozen=True)
class InstalledAgent:
    """One agent found on this computer.

    Attributes:
        id: ``MeetingAgentCore`` value.
        path: Absolute path of the executable that will run.
        version: Version text as the agent printed it (``2.1.281``).
        account: How it is signed in, for display (``Claude Team``,
            ``ChatGPT``), or "" when unknown.
        signed_in: False when the agent reported no sign-in, None when it
            cannot say without starting a service.
        problem: Why it cannot run meeting passes, or "".
    """

    id: str
    path: str
    version: str
    account: str = ""
    signed_in: Optional[bool] = None
    problem: str = ""

    @property
    def spec(self) -> AgentSpec:
        return AGENT_SPECS[self.id]

    @property
    def usable(self) -> bool:
        """True when OpenWhisper can start meeting passes with it."""
        return not self.problem and self.signed_in is not False


@dataclass(frozen=True)
class AgentModel:
    """One choice in an agent's model list; ``value`` "" is its own default."""

    value: str
    label: str


# ---- process helpers ----

def agent_child_env(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Environment for an agent process: the user's, minus OpenWhisper's keys.

    OpenWhisper loads its ``.env`` into its own environment; those keys belong
    to OpenWhisper's built-in engines and must not change which account the
    user's agent bills. Variables the user exported themselves pass through.
    """
    env = dict(os.environ)
    try:
        from services.credentials import dotenv_injected_names

        for name in dotenv_injected_names():
            env.pop(name, None)
    except Exception:
        logger.debug("Could not read the .env key list", exc_info=True)
    for name in ("OPENWHISPER_SIDECAR_TOKEN", "OPENWHISPER_LLM_API_KEY",
                 "OPENWHISPER_LLM_BASE_URL"):
        env.pop(name, None)
    if extra:
        env.update(extra)
    return env


def agent_workspace_dir() -> str:
    """An empty folder, outside any repository, that agents run in.

    Agents read instructions from their working folder and its parents
    (CLAUDE.md, AGENTS.md); a folder under the temp directory keeps a source
    checkout's files out of meeting passes. It is stable, so OpenCode files
    every pass under one project.
    """
    path = os.path.join(tempfile.gettempdir(), "openwhisper-agent-workspace")
    os.makedirs(path, exist_ok=True)
    return path


def popen_flags() -> Dict[str, object]:
    """Keyword arguments that keep an agent process windowless and killable."""
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}  # type: ignore[attr-defined]
    return {"start_new_session": True}


def kill_process_tree(proc: Optional[subprocess.Popen], wait_s: float = 3.0) -> None:
    """Stop ``proc`` and everything it started. Never raises.

    Agents start helpers of their own (Codex runs a code-mode host), so ending
    only the parent can leave them behind.
    """
    if proc is None or proc.poll() is not None:
        return
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True, timeout=wait_s, **popen_flags(),
            )
        else:
            import signal

            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                proc.terminate()
        proc.wait(timeout=wait_s)
    except Exception:
        try:
            proc.kill()
            proc.wait(timeout=1.0)
        except Exception:
            logger.debug("Could not stop agent process %s", proc.pid, exc_info=True)


def _run(cmd: Sequence[str], timeout_s: float) -> Tuple[Optional[int], str]:
    """Run a short probe; return (exit code, stdout+stderr), (None, "") on failure."""
    try:
        result = subprocess.run(
            list(cmd), capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout_s, stdin=subprocess.DEVNULL,
            env=agent_child_env(), cwd=agent_workspace_dir(), **popen_flags(),
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        logger.debug("Agent probe failed: %s", cmd[:2], exc_info=True)
        return None, ""
    return result.returncode, (result.stdout or "") + (result.stderr or "")


def parse_version(text: str) -> Tuple[Tuple[int, ...], str]:
    """First version number in ``text``, as a sort key and as printed.

    A pre-release sorts before its release (``0.158.0-alpha.2`` < ``0.158.0``).
    Returns ``((), "")`` when there is none.
    """
    match = _VERSION_RE.search(text or "")
    if not match:
        return (), ""
    major, minor, patch, pre = match.groups()
    numbers = (int(major), int(minor), int(patch or 0))
    return numbers + ((0,) if pre else (1,)), match.group(0)


# ---- where each agent lives ----

def _home() -> str:
    return os.path.expanduser("~")


def _npm_global_bins() -> List[str]:
    bins: List[str] = []
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            bins.append(os.path.join(appdata, "npm"))
    else:
        prefix = os.environ.get("NPM_CONFIG_PREFIX")
        if prefix:
            bins.append(os.path.join(prefix, "bin"))
        bins.extend([
            os.path.join(_home(), ".npm-global", "bin"),
            os.path.join(_home(), ".bun", "bin"),
            "/opt/homebrew/bin",
            "/usr/local/bin",
        ])
    return bins


def _names(command: str) -> List[str]:
    if sys.platform == "win32":
        return [f"{command}.exe", f"{command}.cmd"]
    return [command]


def candidate_paths(agent_id: str) -> List[str]:
    """Every existing executable that could be ``agent_id``, PATH first.

    Several installs can coexist (a desktop app often bundles a newer CLI than
    the one on PATH); :func:`find_agent` runs the newest.
    """
    spec = AGENT_SPECS[agent_id]
    home = _home()
    local = os.environ.get("LOCALAPPDATA", "")
    found: List[str] = []
    for command in spec.commands:
        path = shutil.which(command)
        if path:
            found.append(path)
    dirs: List[str] = [os.path.join(home, ".local", "bin")]
    patterns: List[str] = []
    if agent_id == CLAUDE_CODE:
        dirs.append(os.path.join(home, ".claude", "local"))
    elif agent_id == CODEX:
        if sys.platform == "win32" and local:
            # The Codex desktop app keeps versioned CLIs under bin\<hash>\.
            patterns.append(os.path.join(local, "OpenAI", "Codex", "bin", "*", "codex.exe"))
            dirs.append(os.path.join(local, "OpenAI", "Codex", "bin"))
            dirs.append(os.path.join(local, "Programs", "OpenAI", "Codex", "bin"))
        elif sys.platform == "darwin":
            for root in ("/Applications", os.path.join(home, "Applications")):
                dirs.append(os.path.join(root, "Codex.app", "Contents", "Resources"))
    elif agent_id == OPENCODE:
        dirs.append(os.path.join(home, ".opencode", "bin"))
        if sys.platform == "win32":
            dirs.append(os.path.join(home, "scoop", "shims"))
            if local:
                dirs.append(os.path.join(local, "Microsoft", "WinGet", "Links"))
    dirs.extend(_npm_global_bins())
    for directory in dirs:
        for command in spec.commands:
            for name in _names(command):
                found.append(os.path.join(directory, name))
    for pattern in patterns:
        found.extend(sorted(glob.glob(pattern)))

    unique: List[str] = []
    seen = set()
    for path in found:
        if not path or not os.path.isfile(path):
            continue
        if sys.platform != "win32" and not os.access(path, os.X_OK):
            continue
        key = os.path.normcase(os.path.realpath(path))
        if key not in seen:
            seen.add(key)
            unique.append(os.path.abspath(path))
    return unique


def _probe_version(path: str) -> Tuple[Tuple[int, ...], str]:
    code, output = _run([path, "--version"], _VERSION_TIMEOUT_S)
    if code != 0:
        return (), ""
    return parse_version(output)


def find_agent(agent_id: str) -> Optional[InstalledAgent]:
    """The newest working install of ``agent_id``, without its sign-in.

    Returns:
        The agent with its path, version, and any version problem, or None
        when no candidate answers ``--version``.
    """
    spec = AGENT_SPECS[agent_id]
    best: Optional[Tuple[Tuple[int, ...], str, str]] = None
    for path in candidate_paths(agent_id):
        key, text = _probe_version(path)
        if not key:
            continue
        if best is None or key > best[0]:
            best = (key, text, path)
    if best is None:
        return None
    key, text, path = best
    problem = ""
    if spec.min_version and key[:len(spec.min_version)] < spec.min_version:
        wanted = ".".join(str(n) for n in spec.min_version)
        problem = (f"OpenWhisper needs {spec.name} {wanted} or newer. "
                   f"{spec.update_hint}").strip()
    return InstalledAgent(id=agent_id, path=path, version=text, problem=problem)


# ---- sign-in ----

_CLAUDE_PLANS = {
    "pro": "Claude Pro", "max": "Claude Max", "team": "Claude Team",
    "enterprise": "Claude Enterprise", "free": "Claude",
}
_CLAUDE_PROVIDERS = {
    "bedrock": "Amazon Bedrock", "vertex": "Google Vertex AI",
    "foundry": "Microsoft Foundry",
}


def _claude_sign_in(path: str) -> Tuple[Optional[bool], str]:
    code, output = _run([path, "auth", "status"], _AUTH_TIMEOUT_S)
    start = output.find("{")
    if start < 0:
        return None, ""
    try:
        status = json.loads(output[start:output.rfind("}") + 1])
    except ValueError:
        return None, ""
    if not isinstance(status, dict):
        return None, ""
    if status.get("loggedIn") is False:
        return False, ""
    provider = str(status.get("apiProvider") or "")
    if provider in _CLAUDE_PROVIDERS:
        return True, _CLAUDE_PROVIDERS[provider]
    plan = str(status.get("subscriptionType") or "").lower()
    if plan in _CLAUDE_PLANS:
        return True, _CLAUDE_PLANS[plan]
    method = str(status.get("authMethod") or "").lower()
    if "key" in method:
        return True, "API key"
    return (True, "Claude account") if status.get("loggedIn") else (None, "")


def _codex_sign_in(path: str) -> Tuple[Optional[bool], str]:
    code, output = _run([path, "login", "status"], _AUTH_TIMEOUT_S)
    text = output.lower()
    if code is None:
        return None, ""
    if "not logged in" in text or code != 0:
        return False, ""
    if "chatgpt" in text:
        return True, "ChatGPT"
    if "api key" in text:
        return True, "API key"
    return True, ""


def with_sign_in(agent: InstalledAgent) -> InstalledAgent:
    """``agent`` with its sign-in filled in where it can be read safely.

    OpenCode only reports its providers through its background service, which
    OpenWhisper never starts; its sign-in stays unknown here and shows up as
    the model list.
    """
    if agent.problem:
        return agent
    signed_in: Optional[bool] = None
    account = ""
    if agent.id == CLAUDE_CODE:
        signed_in, account = _claude_sign_in(agent.path)
    elif agent.id == CODEX:
        signed_in, account = _codex_sign_in(agent.path)
    return InstalledAgent(
        id=agent.id, path=agent.path, version=agent.version,
        account=account, signed_in=signed_in, problem=agent.problem,
    )


def sign_in_hint(agent_id: str) -> str:
    """What to run when ``agent_id`` reports no sign-in."""
    return {
        CLAUDE_CODE: "Run `claude` once and sign in, or `claude auth login`.",
        CODEX: "Run `codex login`.",
        OPENCODE: "Run `opencode auth login`.",
    }.get(agent_id, "")


# ---- scanning (cached) ----

_scan_lock = threading.Lock()
_scan_result: Optional[Dict[str, Optional[InstalledAgent]]] = None


def scan_agents(refresh: bool = False) -> Dict[str, Optional[InstalledAgent]]:
    """Find every supported agent and its sign-in. Blocking; seconds at most.

    Results are cached for the app session; ``refresh`` looks again, for
    example after the user installs one.

    Returns:
        ``{agent_id: InstalledAgent or None}`` in :data:`AGENT_ORDER`.
    """
    global _scan_result
    with _scan_lock:
        if _scan_result is not None and not refresh:
            return dict(_scan_result)

    def scan_one(agent_id: str) -> Optional[InstalledAgent]:
        try:
            agent = find_agent(agent_id)
            return with_sign_in(agent) if agent is not None else None
        except Exception:
            logger.warning("Scanning for %s failed", agent_id, exc_info=True)
            return None

    with ThreadPoolExecutor(max_workers=len(AGENT_ORDER),
                            thread_name_prefix="agent-scan") as pool:
        found = dict(zip(AGENT_ORDER, pool.map(scan_one, AGENT_ORDER)))
    with _scan_lock:
        _scan_result = found
    logger.info(
        "Installed agents: %s",
        ", ".join(f"{a}={found[a].version if found[a] else '-'}" for a in AGENT_ORDER),
    )
    return dict(found)


def cached_scan() -> Optional[Dict[str, Optional[InstalledAgent]]]:
    """The last scan without blocking, or None before the first one."""
    with _scan_lock:
        return dict(_scan_result) if _scan_result is not None else None


def resolve_agent(agent_id: str) -> Optional[InstalledAgent]:
    """The agent a meeting will run, from the cached scan or a fresh lookup."""
    cached = cached_scan()
    if cached is not None and cached.get(agent_id) is not None:
        agent = cached[agent_id]
        if agent is not None and os.path.isfile(agent.path):
            return agent
    agent = find_agent(agent_id)
    return with_sign_in(agent) if agent is not None else None


# ---- the agents' own configuration ----

def _strip_jsonc(text: str) -> str:
    """Drop // and /* */ comments and trailing commas outside strings."""
    out: List[str] = []
    i, n = 0, len(text)
    in_string = False
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
        elif text.startswith("//", i):
            end = text.find("\n", i)
            i = n if end < 0 else end
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
        else:
            out.append(ch)
            i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def _read_json(path: str, jsonc: bool = False) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
        data = json.loads(_strip_jsonc(text) if jsonc else text)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _claude_config_dir() -> str:
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(_home(), ".claude")


def _codex_home() -> str:
    return os.environ.get("CODEX_HOME") or os.path.join(_home(), ".codex")


def _opencode_config_dir() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(_home(), ".config")
    return os.path.join(base, "opencode")


def configured_default_model(agent_id: str) -> str:
    """The model the user set as the agent's default, or "" when unset."""
    if agent_id == CLAUDE_CODE:
        data = _read_json(os.path.join(_claude_config_dir(), "settings.json")) or {}
        model = data.get("model")
    elif agent_id == CODEX:
        try:
            import tomllib

            with open(os.path.join(_codex_home(), "config.toml"), "rb") as handle:
                model = tomllib.load(handle).get("model")
        except (OSError, ValueError, ImportError):
            model = None
    elif agent_id == OPENCODE:
        model = None
        for name in ("opencode.jsonc", "opencode.json", "config.json"):
            data = _read_json(os.path.join(_opencode_config_dir(), name), jsonc=True)
            if data and isinstance(data.get("model"), str):
                model = data["model"]
                break
    else:
        model = None
    return model.strip() if isinstance(model, str) else ""


_CLAUDE_MODELS: Tuple[AgentModel, ...] = (
    AgentModel("haiku", "Haiku (fastest)"),
    AgentModel("sonnet", "Sonnet"),
    AgentModel("opus", "Opus"),
    AgentModel("fable", "Fable"),
)


def _codex_models() -> List[AgentModel]:
    data = _read_json(os.path.join(_codex_home(), "models_cache.json")) or {}
    models: List[AgentModel] = []
    for entry in data.get("models") or []:
        if not isinstance(entry, dict) or entry.get("visibility") not in (None, "list"):
            continue
        slug = entry.get("slug")
        if isinstance(slug, str) and slug:
            models.append(AgentModel(slug, str(entry.get("display_name") or slug)))
    return models


def default_model_label(agent_id: str) -> str:
    """The first entry of every model list: the agent's own default."""
    configured = configured_default_model(agent_id)
    name = AGENT_SPECS[agent_id].name
    return f"{name} default ({configured})" if configured else f"{name} default"


def list_models(agent: InstalledAgent, timeout_s: float = 20.0) -> List[AgentModel]:
    """The models ``agent`` offers, its own default first. Blocking.

    Claude Code takes model aliases; Codex lists what its last catalog fetch
    cached; OpenCode reports every model its configured providers allow, which
    takes a short ACP handshake. A failed lookup still returns the default.
    """
    models = [AgentModel("", default_model_label(agent.id))]
    if agent.id == CLAUDE_CODE:
        models.extend(_CLAUDE_MODELS)
    elif agent.id == CODEX:
        models.extend(_codex_models())
    elif agent.id == OPENCODE and agent.usable:
        try:
            from meeting.agent.installed.acp import list_opencode_models

            models.extend(
                AgentModel(value, label)
                for value, label in list_opencode_models(agent.path, timeout_s=timeout_s)
            )
        except Exception:
            logger.warning("Could not list OpenCode models", exc_info=True)
    return models


def model_display_name(agent_id: str, model: str,
                       models: Optional[Iterable[AgentModel]] = None) -> str:
    """Short model name for a summary line (``Haiku``, ``gpt-6-sol``)."""
    if not model:
        configured = configured_default_model(agent_id)
        return configured.rsplit("/", 1)[-1] if configured else "default model"
    for entry in models or ():
        if entry.value == model:
            return re.sub(r"\s*\(.*\)$", "", entry.label)
    for entry in _CLAUDE_MODELS if agent_id == CLAUDE_CODE else ():
        if entry.value == model:
            return re.sub(r"\s*\(.*\)$", "", entry.label)
    return model.rsplit("/", 1)[-1]


def describe_choice(agent_id: str, model: str) -> str:
    """``Claude Code · Haiku`` — the agent and model a meeting will use."""
    spec = AGENT_SPECS.get(agent_id)
    if spec is None:
        return ""
    return f"{spec.name} · {model_display_name(agent_id, model)}"
