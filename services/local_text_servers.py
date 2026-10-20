"""What a local inference server says about itself and its models.

Every server lists its models on the OpenAI ``/models`` route, but each one
reports what a model is for and how much context it has somewhere else. This
module reads those routes so cleanup and meetings know a model's real limits
and reasoning control instead of guessing, and so embedding, speech and
rerank models stay out of a chat-model list. It also identifies which server
answers an address, since LocalAI and llama.cpp builds before 2026-10 both
listen on port 8080.

Upstream routes, checked against each project's source in October 2026:

* Ollama ``POST /api/show``: ``capabilities`` (completion, tools, thinking,
  embedding) and ``num_ctx`` among the Modelfile ``parameters``; ``GET /``
  answers "Ollama is running". The OpenAI route accepts ``reasoning_effort``,
  where ``"none"`` turns thinking off.
* vLLM ``GET /v1/models``: ``max_model_len`` on each model; ``GET /version``
  needs no key. ``reasoning_effort`` is a request field, and ``"none"`` sets
  the template's ``enable_thinking`` off.
* llama.cpp ``GET /v1/models``: ``owned_by: "llamacpp"`` and ``meta.n_ctx``
  (router mode: ``context_length``); ``GET /props`` carries ``build_info``.
  ``reasoning_effort: "none"`` disables thinking. With ``--api-key`` only
  ``/health`` stays public.
* LM Studio ``GET /api/v1/models`` (0.4+): ``type`` llm/embedding,
  ``max_context_length``, ``loaded_instances[].config.context_length`` and
  ``capabilities.reasoning.allowed_options``; ``/api/v0/models`` before that.
* LocalAI ``GET /v1/models/capabilities``: per-model ``capabilities`` (chat,
  embeddings, tts, transcript, rerank, tools, thinking) and ``context_size``;
  ``GET /.well-known/localai.json`` is public. ``reasoning_effort: "none"``
  disables thinking.
"""
from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

from services.text_model_catalog import TextModelSpec, remember_server_specs

logger = logging.getLogger(__name__)

#: Cleanup "Off" asks a thinking model not to think; servers that accept the
#: OpenAI effort field take "none" for that.
_EFFORT_OFF = {"off": "none"}
#: Last-resort name markers for servers that say nothing about a model.
_NON_CHAT_NAME = re.compile(r"embed|rerank|whisper|(^|[-_/])tts([-_.]|$)", re.I)
_PROBE_TIMEOUT_S = 0.6


def server_root(base_url: str) -> str:
    """The server origin and path prefix in front of its ``/v1`` routes."""
    parts = urlsplit(base_url.rstrip("/"))
    path = parts.path[:-3] if parts.path.endswith("/v1") else parts.path
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def _http(base_url: str, api_key: Optional[str], timeout: float):
    import httpx

    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    return httpx.Client(base_url=server_root(base_url), headers=headers,
                        timeout=timeout, follow_redirects=False)


def _json(http, method: str, path: str, **kwargs) -> Any:
    response = http.request(method, path, **kwargs)
    response.raise_for_status()
    return response.json()


def _spec(context: int = 0, *, tools: bool = True, reasoning: bool = False,
          effort: Optional[dict] = None) -> TextModelSpec:
    """A chat spec from what the server reported; 0 means it did not say.

    ``effort`` is set when the server takes ``reasoning_effort`` for this
    model, mapping cleanup levels the model cannot take to None (omitted).
    """
    limits = {}
    if context > 0:
        context = max(1024, min(context, 2_000_000))
        limits = {"context_window": context,
                  "max_output_tokens": min(4096, context // 2)}
    return TextModelSpec(
        tools=tools, reasoning=reasoning, **limits,
        reasoning_format="openai" if effort is not None else "",
        thinking_levels=effort or None,
    )


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _ollama(http, model_ids: List[str], _rows: Dict[str, dict]) -> Dict[str, Optional[TextModelSpec]]:
    def show(model: str) -> Optional[TextModelSpec]:
        data = _json(http, "POST", "/api/show", json={"model": model})
        capabilities = data.get("capabilities") or []
        if "completion" not in capabilities:
            return None
        found = re.search(r"(?m)^num_ctx\s+(\d+)", data.get("parameters") or "")
        thinking = "thinking" in capabilities
        # Without num_ctx Ollama allocates 4096 tokens unless the GPU has
        # 23 GiB or more, which this client cannot see.
        return _spec(int(found.group(1)) if found else 4096,
                     tools="tools" in capabilities, reasoning=thinking,
                     effort=dict(_EFFORT_OFF) if thinking else None)

    # /api/show only reads installed metadata; it never pulls or loads a model.
    with ThreadPoolExecutor(max_workers=4) as executor:
        return dict(zip(model_ids, executor.map(show, model_ids)))


def _vllm(_http, model_ids: List[str], rows: Dict[str, dict]) -> Dict[str, Optional[TextModelSpec]]:
    # Whether the model thinks is not reported; the effort field is accepted
    # either way and only reaches a template that reads it.
    return {m: _spec(_int(rows[m].get("max_model_len")), effort=dict(_EFFORT_OFF))
            for m in model_ids}


def _llama_cpp(_http, model_ids: List[str], rows: Dict[str, dict]) -> Dict[str, Optional[TextModelSpec]]:
    return {
        m: _spec(_int((rows[m].get("meta") or {}).get("n_ctx"))
                 or _int(rows[m].get("context_length")),
                 effort=dict(_EFFORT_OFF))
        for m in model_ids
    }


def _lm_studio(http, model_ids: List[str], _rows: Dict[str, dict]) -> Dict[str, Optional[TextModelSpec]]:
    try:
        entries = {e.get("key"): e for e in _json(http, "GET", "/api/v1/models").get("models") or []}
        legacy = False
    except Exception:
        entries = {e.get("id"): e for e in _json(http, "GET", "/api/v0/models").get("data") or []}
        legacy = True
    specs: Dict[str, Optional[TextModelSpec]] = {}
    for model in model_ids:
        entry = entries.get(model)
        if entry is None:
            specs[model] = _spec()
            continue
        if entry.get("type") not in ("llm", "vlm"):
            specs[model] = None
            continue
        capabilities = entry.get("capabilities")
        if legacy:
            specs[model] = _spec(_int(entry.get("max_context_length")),
                                 tools="tool_use" in (capabilities or []))
            continue
        capabilities = capabilities or {}
        loaded = [(i.get("config") or {}).get("context_length")
                  for i in entry.get("loaded_instances") or []]
        context = next((_int(c) for c in loaded if _int(c)), 0) or _int(entry.get("max_context_length"))
        options = set((capabilities.get("reasoning") or {}).get("allowed_options") or [])
        levels = {"low", "medium", "high"} & options
        # Which effort values the OpenAI route takes beyond low/medium/high is
        # undocumented, so Off leaves the model's own default alone.
        effort = ({"off": None, **{lvl: lvl if lvl in levels else None
                                   for lvl in ("low", "medium", "high")}}
                  if levels else None)
        specs[model] = _spec(context, tools=capabilities.get("trained_for_tool_use") is not False,
                             reasoning=bool(options), effort=effort)
    return specs


def _localai(http, model_ids: List[str], _rows: Dict[str, dict]) -> Dict[str, Optional[TextModelSpec]]:
    entries = {e.get("id"): e for e in _json(http, "GET", "/v1/models/capabilities").get("data") or []}
    specs: Dict[str, Optional[TextModelSpec]] = {}
    for model in model_ids:
        entry = entries.get(model)
        if entry is None:
            specs[model] = _by_name(model)
            continue
        capabilities = entry.get("capabilities") or []
        if "chat" not in capabilities:
            specs[model] = None
            continue
        thinking = "thinking" in capabilities
        specs[model] = _spec(_int(entry.get("context_size")), tools="tools" in capabilities,
                             reasoning=thinking, effort=dict(_EFFORT_OFF) if thinking else None)
    return specs


def _by_name(model: str) -> Optional[TextModelSpec]:
    return None if _NON_CHAT_NAME.search(model) else _spec()


_DESCRIBE: Dict[str, Callable[..., Dict[str, Optional[TextModelSpec]]]] = {
    "ollama": _ollama,
    "vllm": _vllm,
    "llama_cpp": _llama_cpp,
    "lm_studio": _lm_studio,
    "localai": _localai,
}


def discover_chat_models(
    base_url: str,
    server: Optional[str],
    api_key: Optional[str] = None,
    *,
    timeout: float = 5.0,
) -> List[str]:
    """Return the chat models at ``base_url`` and remember their specs.

    Args:
        base_url: The server's OpenAI-compatible ``/v1`` URL.
        server: Which server answers there (a local server preset id), or
            None to identify it first.
        api_key: Bearer key for a server started with one.
        timeout: Per-request timeout.

    Raises when the model list itself cannot be read; a server that will
    not describe its models still lists them, judged by name.
    """
    with _http(base_url, api_key, timeout) as http:
        rows = _json(http, "GET", base_url.rstrip("/") + "/models").get("data") or []
        rows = {row["id"]: row for row in rows if isinstance(row, dict) and isinstance(row.get("id"), str)}
        model_ids = list(rows)
        if server is None:
            server = _identify(http, rows)
        try:
            specs = _DESCRIBE[server](http, model_ids, rows)
        except Exception as exc:
            logger.debug("%s did not describe its models: %s", server or "Server",
                         type(exc).__name__)
            specs = {model: _by_name(model) for model in model_ids}
    remember_server_specs(base_url, {m: s for m, s in specs.items() if s is not None})
    return sorted(m for m, spec in specs.items() if spec is not None)


_OWNERS = {"vllm": "vllm", "llamacpp": "llama_cpp"}


def _identify(http, rows: Optional[Dict[str, dict]] = None) -> Optional[str]:
    """Name the local server answering ``http``'s address, or None.

    ``rows`` are its ``/models`` entries when already read, whose
    ``owned_by`` names vLLM and llama.cpp outright.
    """
    import httpx

    owner = _server_from_owners(rows)
    if owner is not None:
        return owner

    def get(path: str):
        return _probe(http, path, httpx.HTTPError)

    answer = get("/.well-known/localai.json")
    if answer is None:
        return None
    status, body = answer
    if status == 200 and isinstance(body, dict) and "endpoints" in body:
        return "localai"
    for server, answers in _FINGERPRINTS:
        if answers(get):
            return server
    return None


def _server_from_owners(rows: Optional[Dict[str, dict]]) -> Optional[str]:
    for row in (rows or {}).values():
        if row.get("owned_by") in _OWNERS:
            return _OWNERS[row["owned_by"]]
    return None


def _probe(http, path: str, http_error: type) -> Optional[Tuple[int, Any]]:
    """GET ``path`` as ``(status, body)``, or None when the request fails."""
    try:
        response = http.get(path)
    except http_error:
        return None
    try:
        body = response.json()
    except ValueError:
        body = response.text
    return response.status_code, body


def _answers_ollama(get) -> bool:
    _status, body = get("/") or (0, None)
    return isinstance(body, str) and "Ollama is running" in body


def _answers_llama_cpp(get) -> bool:
    status, body = get("/props") or (0, None)
    if not isinstance(body, dict):
        return False
    if "build_info" in body:
        return True
    return status == 401 and (body.get("error") or {}).get("message") == "Invalid API Key"


def _answers_vllm(get) -> bool:
    status, body = get("/version") or (0, None)
    if not (status == 200 and isinstance(body, dict) and "version" in body):
        return False
    status, body = get("/v1/models") or (0, None)
    owners = set()
    if isinstance(body, dict):
        owners = {row.get("owned_by") for row in body.get("data") or [] if isinstance(row, dict)}
    # With --api-key the model list is closed but /version stays open.
    return status == 401 or "vllm" in owners


def _answers_lm_studio(get) -> bool:
    for path in ("/api/v1/models", "/api/v0/models"):
        status, body = get(path) or (0, None)
        if status == 200 and isinstance(body, dict) and ("models" in body or "data" in body):
            return True
    return False


#: Checked in order after LocalAI; each takes the address's GET probe.
_FINGERPRINTS: Tuple[Tuple[str, Callable[..., bool]], ...] = (
    ("ollama", _answers_ollama),
    ("llama_cpp", _answers_llama_cpp),
    ("vllm", _answers_vllm),
    ("lm_studio", _answers_lm_studio),
)


def identify_server(base_url: str, timeout: float = _PROBE_TIMEOUT_S) -> Optional[str]:
    """Name the local server preset answering ``base_url``, or None.

    Uses only routes that need no key, apart from llama.cpp's, whose key
    refusal still names it. A port nothing listens on costs one refused
    connection.
    """
    with _http(base_url, None, timeout) as http:
        return _identify(http)


def detect_running_servers(base_urls: List[str]) -> Dict[str, str]:
    """Return ``{base_url: server}`` for the loopback addresses that answer.

    Probes run in parallel with a short timeout; nothing here retries.
    """
    from services.text_llm import is_loopback_url

    candidates = list(dict.fromkeys(url for url in base_urls if is_loopback_url(url)))
    if not candidates:
        return {}

    def probe(url: str) -> Tuple[str, Optional[str]]:
        try:
            return url, identify_server(url)
        except Exception:
            return url, None

    with ThreadPoolExecutor(max_workers=len(candidates)) as executor:
        return {url: server for url, server in executor.map(probe, candidates) if server}
