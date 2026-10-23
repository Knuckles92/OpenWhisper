"""Process boundary for native Whisper and potentially stalled network operations.

Launched through main.py after the same native library bootstrap as the app.
Secrets arrive over an authenticated local socket, never in process arguments.
"""
from __future__ import annotations

import json
import os
import socket
import select
import sys
import threading


#: Optional transcription fields, sent only when the request carries them.
_OPENAI_HINTS = ("prompt", "language", "languages", "keywords")


def openai_transcribe(request: dict) -> dict:
    import httpx
    from openai import OpenAI

    hints = {name: request[name] for name in _OPENAI_HINTS if request.get(name)}
    # The parent enforces an absolute deadline as well. Disabling
    # automatic retries avoids duplicate paid requests on cancel.
    # No base_url means OpenAI (or the SDK's OPENAI_BASE_URL).
    with OpenAI(api_key=request["api_key"], base_url=request.get("base_url"), max_retries=0,
                timeout=httpx.Timeout(request["read_timeout"], connect=10.)) as client:
        with open(request["audio_path"], "rb") as audio:
            response = client.audio.transcriptions.create(
                file=audio, model=request["model"], response_format=request["response_format"],
                **hints)
    return {"text": response if isinstance(response, str) else response.text}


def _redirect_stdout_to_stderr() -> None:
    # Libraries that write progress to stdout must not corrupt protocol data.
    # A frozen GUI executable may have no standard Python streams at all.
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")
    sys.stdout = sys.stderr
    try:
        os.dup2(sys.stderr.fileno(), 1)
    except OSError:
        pass


def _watch_parent(connection: socket.socket) -> None:
    # A crashed parent cannot run cleanup. Stop even if the main worker
    # thread is stuck in native inference or a network read at that time.
    while True:
        try:
            readable, _, _ = select.select([connection], [], [], .5)
            if readable and connection.recv(1, socket.MSG_PEEK) == b"":
                os._exit(0)
        except OSError:
            os._exit(0)
        threading.Event().wait(.2)


def _progress_writer(protocol, request: dict):
    def progress(done, total):
        protocol.write(json.dumps({"id": request["id"], "progress": [done, total]}) + "\n")
        protocol.flush()

    return progress


def _load_whisper(request: dict):
    from faster_whisper import WhisperModel
    from services.whisper_sources import cached_model_path, is_custom_model, model_revision
    name = request["model"]
    path = name
    if is_custom_model(name) or model_revision(name):
        path = cached_model_path(name)
    return WhisperModel(path, **request["options"])


def _transcribe_whisper(model, request: dict) -> dict:
    if model is None:
        raise RuntimeError("Whisper model is not loaded")
    audio = request["audio_path"]
    if request.get("numpy_audio"):
        import numpy as np
        audio_path = audio
        audio = np.load(audio_path, allow_pickle=False)
        # Don't retain raw preview audio while native inference is
        # running: a parent crash must not leave it on disk.
        os.unlink(audio_path)
    segments, info = model.transcribe(audio, **request["options"])
    return {"segments": [{"text": s.text, "start": s.start, "end": s.end}
                         for s in segments],
            "info": {"language": info.language,
                     "language_probability": info.language_probability}}


def _detect_language_whisper(model, request: dict) -> dict:
    if model is None:
        raise RuntimeError("Whisper model is not loaded")
    audio = request["audio_path"]
    if request.get("numpy_audio"):
        import numpy as np
        audio_path = audio
        audio = np.load(audio_path, allow_pickle=False)
        os.unlink(audio_path)
    else:
        from faster_whisper.audio import decode_audio
        audio = decode_audio(audio, sampling_rate=16000)
    if getattr(audio, "size", 1) == 0:
        return {"language": "", "language_probability": 0.0}
    try:
        language, probability, _all = model.detect_language(
            audio,
            vad_filter=bool(request.get("vad_filter", True)),
            language_detection_segments=int(request.get("language_detection_segments") or 2),
            language_detection_threshold=float(
                request.get("language_detection_threshold") or 0.5
            ),
        )
    except Exception:
        return {"language": "", "language_probability": 0.0}
    return {"language": language or "", "language_probability": float(probability or 0.0)}


def _run_operation(op, request: dict, model, protocol) -> dict:
    """Every operation but ``whisper_load``, which replaces the worker's model."""
    if op == "discover_whisper_models":
        from services.whisper_sources import discover_hub_models_in_process
        return {"models": discover_hub_models_in_process(
            request["repo_id"], request.get("subfolder", ""))}
    if op == "whisper_transcribe":
        return _transcribe_whisper(model, request)
    if op == "whisper_detect_language":
        return _detect_language_whisper(model, request)
    if op == "download_model":
        from services.hf_access import _download_model_files_in_process
        progress = _progress_writer(protocol, request)
        return {"path": _download_model_files_in_process(request["model"], progress)}
    if op == "openai_transcribe":
        return openai_transcribe(request)
    if op == "ping":
        return {"ready": True}
    raise ValueError("Unknown isolated worker operation")


def main():
    port = int(os.environ.pop("OPENWHISPER_WORKER_PORT"))
    token = os.environ.pop("OPENWHISPER_WORKER_TOKEN")
    connection = socket.create_connection(("127.0.0.1", port), timeout=10.)
    connection.sendall(token.encode("ascii"))
    connection.settimeout(None)
    protocol = connection.makefile("w", encoding="utf-8", buffering=1)
    incoming = connection.makefile("r", encoding="utf-8")
    _redirect_stdout_to_stderr()

    threading.Thread(
        target=_watch_parent, args=(connection,), name="worker-parent-watch", daemon=True
    ).start()
    model = None
    for line in incoming:
        request = {}
        try:
            request = json.loads(line)
            op = request["op"]
            if op == "whisper_load":
                model = _load_whisper(request)
                result = {}
            else:
                result = _run_operation(op, request, model, protocol)
            response = {"id": request["id"], "result": result}
        except Exception as exc:
            response = {"id": request.get("id"), "error": str(exc),
                        "status_code": getattr(exc, "status_code", None),
                        "code": getattr(exc, "code", None)}
        protocol.write(json.dumps(response, ensure_ascii=True) + "\n")
        protocol.flush()


if __name__ == "__main__":
    main()
