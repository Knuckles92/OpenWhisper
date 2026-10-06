"""JSON-lines worker entry point for optional isolated speech runtimes."""
from __future__ import annotations

import array
import json
import os
from pathlib import Path
import sys
import traceback

# Embedded Python deliberately ignores the app environment and script directory.
# A source worker does have this directory on sys.path. Remove it so mlx.py
# (and nvidia.py) cannot shadow the downloaded namespace packages.
worker_dir = Path(__file__).resolve().parent
sys.path[:] = [entry for entry in sys.path if Path(entry).resolve() != worker_dir]
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from services.local_asr.languages import native_language_code, qwen_language_name  # noqa: E402


def use_runtime_packages(runtime):
    """Put a macOS runtime's verified wheel tree ahead of the app's packages.

    Windows runtimes are embedded Pythons with no ``site-packages`` folder.
    """
    if not runtime:
        return
    packages = Path(runtime) / "site-packages"
    if packages.is_dir() and str(packages) not in sys.path:
        sys.path.insert(0, str(packages))


def boost_phrases(family, phrases) -> list[str]:
    """The request's phrases for an engine that boosts them; Nemotron only."""
    if family != "nemotron" or not isinstance(phrases, list):
        return []
    return [phrase for phrase in phrases if isinstance(phrase, str) and phrase.strip()][:50]


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
                if family == "parakeet_mlx":
                    from services.local_asr.mlx import MlxRecognizer
                    engine = MlxRecognizer(request["runtime"], request["model_path"], device)
                elif family in ("parakeet", "nemotron"):
                    from services.local_asr.nvidia import NvidiaRecognizer
                    engine = NvidiaRecognizer(request["runtime"], request["model_path"], device)
                elif family == "moonshine":
                    use_runtime_packages(request.get("runtime"))
                    from services.local_asr.moonshine import MoonshineRecognizer
                    engine = MoonshineRecognizer(request["model_path"], request["model"])
                    device = "cpu"
                elif family == "qwen_asr":
                    use_runtime_packages(request.get("runtime"))
                    import torch
                    from qwen_asr import Qwen3ASRModel
                    if device == "cuda" and not torch.cuda.is_available():
                        raise RuntimeError("CUDA is unavailable for Qwen. Select CPU or install a compatible NVIDIA driver.")
                    if device == "mps" and not torch.backends.mps.is_available():
                        device = "cpu"
                    engine = Qwen3ASRModel.from_pretrained(
                        request["model_path"], device_map=device,
                        dtype=torch.float16 if device in ("cuda", "mps") else torch.float32,
                        max_inference_batch_size=1, max_new_tokens=2048,
                    )
                    if device == "mps":
                        # torch 2.6's MPS matmul cannot broadcast grouped-query
                        # attention (16 query over 8 key/value heads) as SDPA
                        # asks it to; eager attention repeats those heads first.
                        for module in engine.model.modules():
                            config = getattr(module, "config", None)
                            if config is not None:
                                config._attn_implementation = "eager"
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
                if getattr(engine, "gpu_name", ""):
                    result["gpu"] = engine.gpu_name
            elif op in ("transcribe", "stream"):
                if engine is None:
                    raise RuntimeError("No model loaded")
                samples = array.array("f")
                if request.get("audio_path"):
                    with open(request["audio_path"], "rb") as audio:
                        samples.frombytes(audio.read())
                language = request.get("language")
                if family in ("parakeet", "nemotron", "moonshine", "parakeet_mlx"):
                    language = native_language_code(family, language)
                    phrases = boost_phrases(family, request.get("phrases"))
                    if op == "stream":
                        result = {"events": engine.stream(request["session"], samples, language, request.get("finish", False),
                                                          **({"phrases": phrases} if phrases else {}))}
                    elif phrases:
                        try:
                            result = engine.transcribe(samples, language, phrases=phrases)
                        except Exception:
                            # Boosting must never cost the dictation, and its
                            # error may quote a phrase, which is never logged.
                            print("Word boosting failed; decoding without it", file=sys.stderr)
                            result = engine.transcribe(samples, language)
                    else:
                        result = engine.transcribe(samples, language)
                else:
                    import numpy as np
                    code = qwen_language_name(language)
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
