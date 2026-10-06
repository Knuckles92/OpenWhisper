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
    with OpenAI(api_key=request["api_key"], max_retries=0,
                timeout=httpx.Timeout(120., connect=10.)) as client:
        with open(request["audio_path"], "rb") as audio:
            response = client.audio.transcriptions.create(
                file=audio, model=request["model"], response_format=request["response_format"],
                **hints)
    return {"text": response if isinstance(response, str) else response.text}


def main():
    port = int(os.environ.pop("OPENWHISPER_WORKER_PORT"))
    token = os.environ.pop("OPENWHISPER_WORKER_TOKEN")
    connection = socket.create_connection(("127.0.0.1", port), timeout=10.)
    connection.sendall(token.encode("ascii"))
    connection.settimeout(None)
    protocol = connection.makefile("w", encoding="utf-8", buffering=1)
    incoming = connection.makefile("r", encoding="utf-8")
    # Libraries that write progress to stdout must not corrupt protocol data.
    # A frozen GUI executable may have no standard Python streams at all.
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")
    sys.stdout = sys.stderr
    try:
        os.dup2(sys.stderr.fileno(), 1)
    except OSError:
        pass

    def watch_parent():
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

    threading.Thread(target=watch_parent, name="worker-parent-watch", daemon=True).start()
    model = None
    for line in incoming:
        request = {}
        try:
            request = json.loads(line)

            def progress(done, total):
                protocol.write(json.dumps({"id": request["id"], "progress": [done, total]}) + "\n")
                protocol.flush()

            op = request["op"]
            if op == "whisper_load":
                from faster_whisper import WhisperModel
                from services.whisper_sources import cached_model_path, is_custom_model, model_revision
                name = request["model"]
                path = cached_model_path(name) if is_custom_model(name) or model_revision(name) else name
                model = WhisperModel(path, **request["options"])
                result = {}
            elif op == "discover_whisper_models":
                from services.whisper_sources import discover_hub_models_in_process
                result = {"models": discover_hub_models_in_process(
                    request["repo_id"], request.get("subfolder", ""))}
            elif op == "whisper_transcribe":
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
                result = {"segments": [{"text": s.text, "start": s.start, "end": s.end}
                                       for s in segments],
                          "info": {"language": info.language,
                                   "language_probability": info.language_probability}}
            elif op == "download_model":
                from services.hf_access import _download_model_files_in_process
                result = {"path": _download_model_files_in_process(request["model"], progress)}
            elif op == "openai_transcribe":
                result = openai_transcribe(request)
            elif op == "ping":
                result = {"ready": True}
            else:
                raise ValueError("Unknown isolated worker operation")
            response = {"id": request["id"], "result": result}
        except Exception as exc:
            response = {"id": request.get("id"), "error": str(exc),
                        "status_code": getattr(exc, "status_code", None),
                        "code": getattr(exc, "code", None)}
        protocol.write(json.dumps(response, ensure_ascii=True) + "\n")
        protocol.flush()


if __name__ == "__main__":
    main()
