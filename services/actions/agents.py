"""Agent actions: hand words to an installed coding agent, or ask it (stream C2).

Contract: docs/design/automation/design.md, "services/actions/agents.py".
Both calls block; run them on a worker.

A handoff opens a visible session the user drives: the agent app's
documented deep link when its handler is registered (links only prefill;
the user sends), else a new terminal running the CLI with the words as one
argv element. An ask runs the CLI headless with no tools in an empty
workspace and returns its answer. Words, prompts, answers and paths are
never logged.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

from services import installed_agents
from services.actions import launch
from services.actions.model import AGENT_NAMES, AGENTS, AgentSurface
from services.actions.runtime import ActionResult
from services.agent_process import popen_agent, run_agent_probe
from services.installed_agents import CLAUDE_CODE, CODEX, OPENCODE

logger = logging.getLogger(__name__)

#: The whole system prompt of an ask.
ASK_INSTRUCTION = "Answer briefly in plain text. You can't use tools or files."
#: Claude Desktop cuts ``q`` near 14,000 characters; longer words go elsewhere.
CLAUDE_APP_TEXT_MAX = 14_000
#: Claude Code's own ``claude-cli://open`` handler takes at most 5,000.
CLAUDE_CLI_TEXT_MAX = 5_000
#: A link travels on the handler's command line (32,767 on Windows).
LINK_MAX_CHARS = 30_000

_MODEL_RE = re.compile(r"[\w.:/@\[\]+-]{1,200}")
_HELP_TIMEOUT_S = 15.0
_STDOUT_MAX_BYTES = 4 * 1024 * 1024
#: Codex features that give the model a tool; each one present is switched off.
_CODEX_TOOL_FEATURES = (
    "shell_tool", "unified_exec", "code_mode", "code_mode_host", "apps",
    "browser_use", "browser_use_external", "browser_use_full_cdp_access",
    "in_app_browser", "computer_use", "image_generation", "view_image",
    "multi_agent", "multi_agent_v2", "plugins", "remote_plugin", "goals",
    "skill_search", "tool_suggest", "skill_mcp_dependency_install",
    "workspace_dependencies", "hooks", "memories", "worktrees", "sleep_tool",
    "request_permissions_tool", "standalone_web_search", "web_search_request",
    "web_search_cached", "js_repl",
)


def available() -> dict[str, bool]:
    """Which installed agents were found by the last scan; never blocks.

    Returns:
        ``{agent_id: found}`` for every agent an action can use, or ``{}``
        before the first scan.
    """
    scan = installed_agents.cached_scan()
    if scan is None:
        return {}
    return {agent_id: scan.get(agent_id) is not None for agent_id in AGENTS}


# ---- handoff ----

def handoff(agent_id: str, text: str, *, folder: str = "", surface: str = "auto") -> ActionResult:
    """Open a visible session of ``agent_id`` with ``text`` ready.

    Args:
        agent_id: ``claude_code``, ``codex`` or ``opencode_cli``.
        text: The words; percent-encoded in links, one argv element otherwise.
        folder: Absolute working folder, or "" for the agent's default (home).
        surface: ``auto`` (app link when its handler is registered, else a
            terminal), ``app`` (links only) or ``terminal``.
    """
    name = AGENT_NAMES.get(agent_id)
    if name is None:
        return ActionResult(False, "Choose Claude Code, Codex or OpenCode for this action.")
    if surface not in AgentSurface.ALL:
        return ActionResult(False, "Choose where the agent opens: app, terminal or automatic.")
    if not text.strip():
        return ActionResult(False, f"There's nothing to send to {name}.")
    if folder and not (os.path.isabs(folder) and os.path.isdir(folder)):
        return ActionResult(False, "This action's working folder doesn't exist anymore. "
                                   "Choose another in Settings.")
    if surface != AgentSurface.TERMINAL:
        failure = ""
        for scheme, uri in app_links(agent_id, text, folder):
            found = launch.handler_available(scheme)
            if found is False or (found is None and surface == AgentSurface.AUTO):
                continue
            try:
                launch.open_uri(uri)
            except launch.LaunchError as exc:
                failure = str(exc)
                logger.info("Agent link for %s didn't open", agent_id)
                continue
            logger.info("Handed off to %s through its %s link", agent_id, scheme)
            return ActionResult(True, f"Opened {name}")
        if surface == AgentSurface.APP:
            return ActionResult(False, failure or _no_app_message(agent_id, text))
    return _handoff_terminal(agent_id, text, folder)


def app_links(agent_id: str, text: str, folder: str = "") -> List[Tuple[str, str]]:
    """``(scheme, link)`` for every app that can take ``text``, best first.

    Links that would cut the words short are left out.
    """
    words = quote(text, safe="")
    place = quote(folder, safe="") if folder else ""
    links: List[Tuple[str, str]] = []
    if agent_id == CLAUDE_CODE:
        if len(text) <= CLAUDE_APP_TEXT_MAX:
            # Claude Desktop asks before using a folder, so name one only when set.
            links.append(("claude", f"claude://code/new?q={words}"
                                    + (f"&folder={place}" if place else "")))
        if len(text) <= CLAUDE_CLI_TEXT_MAX:
            cwd = place or quote(os.path.expanduser("~"), safe="")
            links.append(("claude-cli", f"claude-cli://open?q={words}&cwd={cwd}"))
    elif agent_id == CODEX:
        links.append(("codex", f"codex://new?prompt={words}"
                               + (f"&path={place}" if place else "")))
    return [(scheme, uri) for scheme, uri in links if len(uri) <= LINK_MAX_CHARS]


def _no_app_message(agent_id: str, text: str) -> str:
    if agent_id == CLAUDE_CODE:
        if len(text) > CLAUDE_APP_TEXT_MAX:
            return "That's too long for the Claude app. Choose Terminal for long requests."
        return "Claude Code can't open in an app here. Install the Claude app, or choose Terminal."
    if agent_id == CODEX:
        return "Codex can't open in an app here. Install the ChatGPT app, or choose Terminal."
    return "OpenCode has no app link. Choose Terminal instead."


def terminal_argv(agent_id: str, program: str, text: str) -> List[str]:
    """The command line that starts ``agent_id`` interactively with ``text``.

    The words are always one element, after anything that would let the CLI
    read them as its own options or commands.
    """
    if agent_id == CLAUDE_CODE:
        # Claude Code reads a lone word naming one of its commands (update,
        # purge, ...) as that command even after "--"; words with a space
        # never are one.
        prompt = text if any(ch.isspace() for ch in text) else text + " "
        return [program, "--", prompt]
    if agent_id == CODEX:
        return [program, "--", text]
    return [program, f"--prompt={text}"]


def _handoff_terminal(agent_id: str, text: str, folder: str) -> ActionResult:
    name = AGENT_NAMES[agent_id]
    agent = _find(agent_id)
    if agent is None:
        return ActionResult(False, f"{name} isn't installed.")
    try:
        launch.open_in_terminal(
            terminal_argv(agent_id, agent.path, text),
            cwd=folder or os.path.expanduser("~"),
            env=installed_agents.agent_child_env(),
        )
    except launch.LaunchError as exc:
        return ActionResult(False, str(exc))
    logger.info("Handed off to %s in a terminal", agent_id)
    return ActionResult(True, f"Opened {name} in a terminal")


def _find(agent_id: str) -> Optional[installed_agents.InstalledAgent]:
    """The agent from the last scan, else a fresh lookup (blocking)."""
    scan = installed_agents.cached_scan()
    agent = scan.get(agent_id) if scan else None
    if agent is not None and os.path.isfile(agent.path):
        return agent
    return installed_agents.find_agent(agent_id)


# ---- ask ----

def ask(agent_id: str, text: str, *, model: str = "", timeout_s: float = 120.0,
        cancel: Optional[threading.Event] = None) -> ActionResult:
    """Ask ``agent_id`` with no tools; ``output`` is the answer.

    Args:
        agent_id: ``claude_code``, ``codex`` or ``opencode_cli``.
        text: The question, sent on stdin (OpenCode: as an ACP prompt).
        model: The agent's model id or alias; "" keeps its default.
        timeout_s: Hard wall; the agent and its helpers are killed after it.
        cancel: Set to stop early; the agent is killed the same way.
    """
    name = AGENT_NAMES.get(agent_id)
    if name is None:
        return ActionResult(False, "Choose Claude Code, Codex or OpenCode for this action.")
    if not text.strip():
        return ActionResult(False, f"There's nothing to ask {name}.")
    if model and not _MODEL_RE.fullmatch(model):
        return ActionResult(False, "That model name can't be used. Pick another in Settings.")
    agent = _find(agent_id)
    if agent is None:
        return ActionResult(False, f"{name} isn't installed.")
    cancel = cancel or threading.Event()
    deadline = time.monotonic() + max(1.0, float(timeout_s))
    if agent_id == OPENCODE:
        result = _ask_opencode(agent, text, model, deadline, cancel)
    else:
        result = _ask_headless(agent, text, model, deadline, cancel)
    logger.info("Asked %s: %s", agent_id, "answered" if result.ok else "no answer")
    return result


def _ask_headless(agent: installed_agents.InstalledAgent, text: str, model: str,
                  deadline: float, cancel: threading.Event) -> ActionResult:
    name = AGENT_NAMES[agent.id]
    workdir = tempfile.mkdtemp(prefix="openwhisper-ask-")
    try:
        if agent.id == CLAUDE_CODE:
            argv = _claude_ask_command(agent, model, workdir)
        else:
            argv = _codex_ask_command(agent, model, workdir)
        run = _run_headless(argv, text, deadline=deadline, cancel=cancel)
    except _TooOld as exc:
        return ActionResult(False, str(exc))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    if run.status == "canceled":
        return ActionResult(False, f"Stopped asking {name}.")
    if run.status == "timeout":
        return ActionResult(False, f"{name} didn't answer in time.")
    if run.status == "unstartable":
        return ActionResult(False, f"{name} couldn't be started.")
    if agent.id == CLAUDE_CODE:
        return _claude_answer(run)
    return _codex_answer(run)


class _TooOld(Exception):
    """The installed agent lacks a flag an ask needs; the message says what to do."""


def _claude_ask_command(agent: installed_agents.InstalledAgent, model: str,
                        workdir: str) -> List[str]:
    """``claude -p`` with no tools, no MCP servers, hooks or saved session."""
    help_text = _help(agent, "--help")
    if help_text and "--tools" not in help_text:
        raise _TooOld(f"Claude Code {agent.version} is too old to answer questions "
                      "here. Run `claude update`.")
    mcp_file = os.path.join(workdir, "mcp.json")
    settings_file = os.path.join(workdir, "settings.json")
    with open(mcp_file, "w", encoding="utf-8") as handle:
        json.dump({"mcpServers": {}}, handle)
    with open(settings_file, "w", encoding="utf-8") as handle:
        json.dump({"disableAllHooks": True, "alwaysThinkingEnabled": False}, handle)
    argv = [
        agent.path, "-p", "--output-format", "json",
        "--system-prompt", ASK_INSTRUCTION,
        "--tools", "",
        "--mcp-config", mcp_file, "--strict-mcp-config",
        "--settings", settings_file,
    ]
    for flag in ("--no-session-persistence", "--disable-slash-commands"):
        if not help_text or flag in help_text:
            argv.append(flag)
    if model:
        argv += ["--model", model]
    return argv


def _codex_ask_command(agent: installed_agents.InstalledAgent, model: str,
                       workdir: str) -> List[str]:
    """``codex exec`` without the user's config, with every tool feature off."""
    exec_help = _help(agent, "exec", "--help")
    if exec_help and "--ignore-user-config" not in exec_help:
        spec = installed_agents.AGENT_SPECS[CODEX]
        raise _TooOld(f"Codex {agent.version} is too old to answer questions here. "
                      f"{spec.update_hint}")
    instructions = os.path.join(workdir, "instructions.md")
    with open(instructions, "w", encoding="utf-8") as handle:
        handle.write(ASK_INSTRUCTION)
    argv = [agent.path, "exec", "--json"]
    if not exec_help or "--ephemeral" in exec_help:
        argv.append("--ephemeral")
    # The user's config brings their MCP servers, hooks and notify program;
    # ignoring it keeps sign-in, which lives beside it.
    argv += ["--skip-git-repo-check", "--ignore-user-config", "-s", "read-only",
             "-C", installed_agents.agent_workspace_dir()]
    for override in ("approval_policy=never", "web_search=disabled",
                     "project_doc_max_bytes=0", "model_reasoning_effort=low",
                     f"model_instructions_file={instructions.replace(os.sep, '/')}"):
        argv += ["-c", override]
    for feature in _codex_tool_features(agent):
        argv += ["--disable", feature]
    if not model:
        configured = installed_agents.configured_default_model(CODEX)
        model = configured if _MODEL_RE.fullmatch(configured or "") else ""
    if model:
        argv += ["-m", model]
    argv.append("-")
    return argv


def _codex_tool_features(agent: installed_agents.InstalledAgent) -> List[str]:
    """The tool features this Codex knows; an unknown name would stop the run.

    Without a readable list the shell still goes: a run that fails on the
    flag is better than one that has a shell.
    """
    listing = _help(agent, "features", "list")
    known = {line.split()[0] for line in listing.splitlines() if line.split()}
    return [name for name in _CODEX_TOOL_FEATURES if name in known] or ["shell_tool"]


_help_lock = threading.Lock()
_help_cache: Dict[Tuple[str, str, Tuple[str, ...]], str] = {}


def _help(agent: installed_agents.InstalledAgent, *args: str) -> str:
    """Output of a read-only probe such as ``--help``, cached per version."""
    key = (agent.path, agent.version, args)
    with _help_lock:
        if key in _help_cache:
            return _help_cache[key]
    try:
        result = run_agent_probe(
            [agent.path, *args], text=True, encoding="utf-8", errors="replace",
            timeout=_HELP_TIMEOUT_S, stdin=subprocess.DEVNULL,
            env=installed_agents.agent_child_env(),
            cwd=installed_agents.agent_workspace_dir(), **installed_agents.popen_flags(),
        )
        text = (result.stdout or "") + (result.stderr or "")
    except (OSError, subprocess.SubprocessError, ValueError):
        text = ""
    if text:
        with _help_lock:
            _help_cache[key] = text
    return text


@dataclass
class _Run:
    status: str  # done, timeout, canceled, unstartable
    returncode: Optional[int] = None
    stdout: str = ""
    stderr: str = ""


def _run_headless(argv: List[str], prompt: str, *, deadline: float,
                  cancel: threading.Event) -> _Run:
    """Run ``argv`` with ``prompt`` on stdin; kill its whole tree when done.

    The pipes are binary: a text-mode stdin would turn the prompt's line
    breaks into CRLF on Windows.
    """
    try:
        proc = popen_agent(
            argv, cwd=installed_agents.agent_workspace_dir(),
            env=installed_agents.agent_child_env(),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            **installed_agents.popen_flags(),
        )
    except OSError as exc:
        logger.info("An agent couldn't start (%s)", type(exc).__name__)
        return _Run("unstartable")
    out: List[bytes] = []
    err: deque = deque(maxlen=20)

    def feed() -> None:
        try:
            assert proc.stdin is not None
            proc.stdin.write(prompt.encode("utf-8"))
            proc.stdin.close()
        except (OSError, ValueError):
            pass

    def read_out() -> None:
        assert proc.stdout is not None
        size = 0
        for line in proc.stdout:
            if size < _STDOUT_MAX_BYTES:
                out.append(line)
                size += len(line)

    def read_err() -> None:
        assert proc.stderr is not None
        for raw in proc.stderr:
            line = raw.decode("utf-8", "replace").rstrip()
            if line:
                err.append(line[:400])

    threads = [threading.Thread(target=fn, name=f"agent-ask-{fn.__name__}", daemon=True)
               for fn in (feed, read_out, read_err)]
    for thread in threads:
        thread.start()
    status = "done"
    try:
        while True:
            try:
                proc.wait(timeout=0.1)
                break
            except subprocess.TimeoutExpired:
                pass
            if cancel.is_set():
                status = "canceled"
                break
            if time.monotonic() >= deadline:
                status = "timeout"
                break
    finally:
        # Also ends helpers that outlive the agent while holding its pipes.
        installed_agents.kill_process_tree(proc)
    for thread in threads:
        thread.join(2.0)
    stdout = b"".join(out).decode("utf-8", "replace")
    return _Run(status, proc.returncode, stdout, "\n".join(err))


def _claude_answer(run: _Run) -> ActionResult:
    result = None
    for chunk in [run.stdout.strip(), *reversed(run.stdout.splitlines())]:
        try:
            data = json.loads(chunk)
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("type") == "result":
            result = data
            break
    if result is None:
        return ActionResult(False, _stopped(CLAUDE_CODE, run))
    text = str(result.get("result") or "")
    if result.get("is_error") or result.get("subtype") not in (None, "success"):
        return ActionResult(False, _failure(CLAUDE_CODE, text or str(result.get("subtype"))))
    return _answered(CLAUDE_CODE, text)


def _codex_answer(run: _Run) -> ActionResult:
    answer, error, done = "", "", False
    for line in run.stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        item = event.get("item") if isinstance(event.get("item"), dict) else {}
        if kind == "item.completed" and item.get("type") == "agent_message":
            answer = str(item.get("text") or "")
        elif kind == "turn.completed":
            done = True
        elif kind == "turn.failed":
            error = str((event.get("error") or {}).get("message") or "the turn failed")
        elif kind == "error":
            error = str(event.get("message") or "error")
    if done and answer.strip():
        return _answered(CODEX, answer)  # an earlier error was a retried hiccup
    if error:
        return ActionResult(False, _failure(CODEX, _codex_message(error)))
    if done:
        return _answered(CODEX, answer)
    return ActionResult(False, _stopped(CODEX, run))


def _codex_message(message: str) -> str:
    """Codex wraps provider errors as JSON in the message; the inner text reads better."""
    try:
        data = json.loads(message)
    except ValueError:
        return message
    inner = (data.get("error") or {}).get("message") if isinstance(data, dict) else None
    return inner if isinstance(inner, str) and inner else message


# ---- OpenCode over ACP ----

def _opencode_command(agent: installed_agents.InstalledAgent) -> List[str]:
    return [agent.path, "acp"]


def _ask_opencode(agent: installed_agents.InstalledAgent, text: str, model: str,
                  deadline: float, cancel: threading.Event) -> ActionResult:
    """One ACP session with every OpenCode tool denied, deleted afterwards.

    ``opencode acp`` runs in-process (no background service), and the
    overlay replaces both primary agents' prompts and permissions for this
    process only, as Meeting Mode does.
    """
    from meeting.agent.installed.acp import (
        AcpConnection,
        AcpError,
        config_option,
        initialize,
        model_choices,
    )

    name = AGENT_NAMES[OPENCODE]
    deny_all = [{"action": "*", "resource": "*", "effect": "deny"}]
    overlay = {"agents": {persona: {"system": ASK_INSTRUCTION, "permissions": deny_all}
                          for persona in ("build", "plan")}}
    env = installed_agents.agent_child_env({
        "OPENCODE_CONFIG_CONTENT": json.dumps(overlay),
        "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
        "OPENCODE_DISABLE_AUTOUPDATE": "1",
        "OPENCODE_DISABLE_FILEWATCHER": "1",
    })
    chunks: List[str] = []
    lock = threading.Lock()

    def on_notification(method: str, params: Dict[str, Any]) -> None:
        if method != "session/update":
            return
        update = params.get("update") or {}
        kind = update.get("sessionUpdate")
        with lock:
            if kind == "agent_message_chunk":
                chunk = (update.get("content") or {}).get("text")
                if isinstance(chunk, str):
                    chunks.append(chunk)
            elif kind in ("tool_call", "tool_call_update"):
                chunks.clear()  # narration before a tool call isn't the answer

    def on_request(method: str, params: Dict[str, Any]) -> Any:
        if method == "session/request_permission":
            options = [o for o in params.get("options") or [] if isinstance(o, dict)]
            choice = next((o for o in options if str(o.get("kind", "")).startswith("reject")),
                          None)
            if choice is None:
                return {"outcome": {"outcome": "cancelled"}}
            return {"outcome": {"outcome": "selected", "optionId": choice.get("optionId")}}
        raise AcpError(f"{method} isn't available when asking", -32601)

    def remaining() -> float:
        return max(0.1, deadline - time.monotonic())

    conn = AcpConnection(_opencode_command(agent), env=env,
                         cwd=installed_agents.agent_workspace_dir(),
                         on_notification=on_notification, on_request=on_request)
    try:
        conn.start()
    except AcpError:
        return ActionResult(False, f"{name} couldn't be started.")
    session_id = ""
    can_delete = False
    try:
        info = initialize(conn, remaining())
        can_delete = "delete" in ((info.get("agentCapabilities") or {})
                                  .get("sessionCapabilities") or {})
        session = conn.request("session/new", {
            "cwd": installed_agents.agent_workspace_dir(), "mcpServers": [],
        }, remaining(), cancel_event=cancel)
        session = session if isinstance(session, dict) else {}
        session_id = str(session.get("sessionId") or "")
        if not session_id:
            return ActionResult(False, f"{name} didn't open a session.")
        wanted = model or installed_agents.configured_default_model(OPENCODE)
        if wanted and wanted not in {value for value, _label in model_choices(session)}:
            if model:
                return ActionResult(False, f"OpenCode has no model {model} with your current "
                                           "providers. Pick another in Settings.")
            wanted = ""
        if wanted and wanted != (config_option(session, "model") or {}).get("currentValue"):
            conn.request("session/set_config_option", {
                "sessionId": session_id, "configId": "model", "value": wanted,
            }, min(30.0, remaining()), cancel_event=cancel)
        result = conn.request("session/prompt", {
            "sessionId": session_id, "prompt": [{"type": "text", "text": text}],
        }, remaining(), cancel_event=cancel)
    except AcpError as exc:
        if session_id:
            conn.notify("session/cancel", {"sessionId": session_id})
        if cancel.is_set():
            return ActionResult(False, f"Stopped asking {name}.")
        if time.monotonic() >= deadline:
            return ActionResult(False, f"{name} didn't answer in time.")
        detail = str(exc)
        if "auth" in detail.lower():
            return ActionResult(False, _signed_out(OPENCODE))
        return ActionResult(False, _failure(OPENCODE, detail))
    finally:
        if session_id and can_delete:
            try:
                # Keeps asks out of the user's OpenCode history.
                conn.request("session/delete", {"sessionId": session_id}, 3.0)
            except AcpError:
                logger.debug("Couldn't delete the OpenCode ask session")
        conn.close(wait_s=1.0)
    reason = result.get("stopReason") if isinstance(result, dict) else None
    if reason == "cancelled":
        return ActionResult(False, f"Stopped asking {name}.")
    if reason == "refusal":
        return ActionResult(False, "OpenCode's model wouldn't answer that.")
    with lock:
        answer = "".join(chunks)
    return _answered(OPENCODE, answer)


# ---- messages ----

def _answered(agent_id: str, answer: str) -> ActionResult:
    name = AGENT_NAMES[agent_id]
    answer = answer.strip()
    if not answer:
        return ActionResult(False, f"{name} didn't answer.")
    return ActionResult(True, f"{name} answered", output=answer)


_SIGNED_OUT_WORDS = ("not logged in", "login", "log in", "sign in", "signed out",
                     "authenticat", "unauthorized", "401", "invalid api key")


def _signed_out(agent_id: str) -> str:
    hint = installed_agents.sign_in_hint(agent_id)
    return f"{AGENT_NAMES[agent_id]} is signed out. {hint}".strip()


def _failure(agent_id: str, detail: str) -> str:
    """Friendly copy for an agent's error; signed-out errors get the fix."""
    if any(word in detail.lower() for word in _SIGNED_OUT_WORDS):
        return _signed_out(agent_id)
    detail = " ".join(detail.split())[:200]
    name = AGENT_NAMES[agent_id]
    return f"{name} couldn't answer: {detail}" if detail else f"{name} couldn't answer."


def _stopped(agent_id: str, run: _Run) -> str:
    lines = [line for line in run.stderr.splitlines() if line.strip()]
    if lines and any(word in lines[-1].lower() for word in _SIGNED_OUT_WORDS):
        return _signed_out(agent_id)
    detail = f": {lines[-1][:200]}" if lines else "."
    return f"{AGENT_NAMES[agent_id]} stopped before answering{detail}"
