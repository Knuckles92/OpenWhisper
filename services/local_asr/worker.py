"""JSON-lines worker entry point for optional isolated speech runtimes."""
from __future__ import annotations

import array
import json
import os
from pathlib import Path
import sys
import traceback

# Embedded Python deliberately ignores the app environment and script directory.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def main():
    protocol = os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8", buffering=1)
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    engine = None
    family = None
    for line in sys.stdin:
        request = {}
        try:
            request = json.loads(line)
            op = request["op"]
            if op == "load":
                family = request["backend"]
                device = request["device"]
                if family in ("parakeet", "nemotron"):
                    from services.local_asr.nvidia import NvidiaRecognizer
                    engine = NvidiaRecognizer(request["runtime"], request["model_path"], device)
                elif family == "moonshine":
                    from services.local_asr.moonshine import MoonshineRecognizer
                    engine = MoonshineRecognizer(request["model_path"], request["model"])
                    device = "cpu"
                elif family == "qwen_asr":
                    import torch
                    from qwen_asr import Qwen3ASRModel
                    if device == "cuda" and not torch.cuda.is_available():
                        raise RuntimeError("CUDA is unavailable for Qwen. Select CPU or install a compatible NVIDIA driver.")
                    engine = Qwen3ASRModel.from_pretrained(
                        request["model_path"], device_map=device,
                        dtype=torch.float16 if device == "cuda" else torch.float32,
                        max_inference_batch_size=1, max_new_tokens=2048,
                    )
                    generate = engine.model.generate
                    def checked_generate(*args, **kwargs):
                        output = generate(*args, **kwargs)
                        if output.sequences.shape[1] - kwargs["input_ids"].shape[1] >= kwargs["max_new_tokens"]:
                            raise RuntimeError("Qwen reached its output limit. Try a shorter audio selection.")
                        return output
                    engine.model.generate = checked_generate
                else:
                    raise ValueError("Unknown speech backend")
                result = {"device": device}
            elif op in ("transcribe", "stream"):
                if engine is None:
                    raise RuntimeError("No model loaded")
                samples = array.array("f")
                if request.get("audio_path"):
                    with open(request["audio_path"], "rb") as audio:
                        samples.frombytes(audio.read())
                language = request.get("language")
                if family in ("parakeet", "nemotron", "moonshine"):
                    if op == "stream":
                        result = {"events": engine.stream(request["session"], samples, language, request.get("finish", False))}
                    else:
                        result = engine.transcribe(samples, language)
                else:
                    import numpy as np
                    code = None if language == "auto" else language
                    if code in ("en", "en-US"):
                        code = "English"
                    text = engine.transcribe(audio=(np.asarray(samples, dtype=np.float32), 16000), language=code)[0].text
                    result = dict(text=text, segments=[dict(text=text, start=0., end=len(samples)/16000)] if text else [])
            elif op == "cancel_stream":
                engine.cancel_stream(request["session"])
                result = {}
            elif op == "shutdown":
                if engine is not None and hasattr(engine, "close"):
                    engine.close()
                return
            else:
                raise ValueError("Unknown worker operation")
            response = {"id": request["id"], "result": result}
        except Exception as exc:
            traceback.print_exc(file=sys.stderr)
            response = {"id": request.get("id"), "error": str(exc)}
        protocol.write(json.dumps(response, ensure_ascii=True) + "\n")


if __name__ == "__main__":
    main()

