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
# Frozen builds keep collected packages here too; the runtime's copies must win.
sys.path.append(str(Path(__file__).resolve().parents[2]))

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


class UnsupportedGpu(RuntimeError):
    """The runtime's PyTorch build has no code for this GPU; the CPU still works."""

    code = "unsupported_gpu"


def cuda_arch_supported(capability, arch_list) -> bool:
    """Whether a PyTorch build compiled for ``arch_list`` runs on a GPU of ``capability``.

    ``arch_list`` is ``torch.cuda.get_arch_list()``. Code built for sm_XY runs
    on the same major version at an equal or newer minor; PTX (compute_XY) is
    compiled by the driver for any newer GPU. Anything else fails on the first
    kernel with "no kernel image is available", after CUDA reported itself
    available.
    """
    major, minor = capability
    for arch in arch_list:
        kind, _, number = arch.partition("_")
        if not number.isdigit() or len(number) < 2:
            continue
        built = (int(number[:-1]), int(number[-1]))
        if kind == "sm" and built[0] == major and built[1] <= minor:
            return True
        if kind == "compute" and built <= (major, minor):
            return True
    return False


def check_qwen_gpu(torch) -> None:
    capability = torch.cuda.get_device_capability(0)
    if cuda_arch_supported(capability, torch.cuda.get_arch_list()):
        return
    name = torch.cuda.get_device_name(0)
    raise UnsupportedGpu(
        f"Qwen's runtime (PyTorch {torch.__version__}) has no code for the {name} "
        f"(compute capability {capability[0]}.{capability[1]}). Update the runtime "
        "in Downloads, or use the CPU."
    )


def boost_phrases(family, phrases) -> list[str]:
    """The request's phrases for an engine that boosts them; Nemotron only."""
    if family != "nemotron" or not isinstance(phrases, list):
        return []
    return [phrase for phrase in phrases if isinstance(phrase, str) and phrase.strip()][:50]


SAMPLE_RATE = 16000
#: Families whose engines take a native language code and a samples array.
NATIVE_FAMILIES = ("parakeet", "nemotron", "moonshine", "parakeet_mlx", "parakeet_unified")


class WorkerState:
    """The loaded engine and what the last load asked for, kept across requests.

    A load that fails part way leaves ``family`` and ``device`` at what it
    had set so far, with the previous engine still loaded.
    """

    def __init__(self):
        self.engine = None
        self.family = None
        self.device = None


def load_engine(state, request):
    state.family = request["backend"]
    state.device = request["device"]
    family = state.family
    if family == "parakeet_mlx":
        from services.local_asr.mlx import MlxRecognizer
        state.engine = MlxRecognizer(request["runtime"], request["model_path"], state.device)
    elif family in ("parakeet", "nemotron"):
        from services.local_asr.nvidia import NvidiaRecognizer
        state.engine = NvidiaRecognizer(request["runtime"], request["model_path"], state.device)
    elif family == "parakeet_unified":
        load_transcribe(state, request)
    elif family == "moonshine":
        use_runtime_packages(request.get("runtime"))
        from services.local_asr.moonshine import MoonshineRecognizer
        state.engine = MoonshineRecognizer(request["model_path"], request["model"])
        state.device = "cpu"
    elif family == "qwen_asr":
        load_qwen(state, request)
    else:
        raise ValueError("Unknown speech backend")
    result = {"device": state.device}
    if getattr(state.engine, "gpu_name", ""):
        result["gpu"] = state.engine.gpu_name
    return result


def load_transcribe(state, request):
    # Off Windows the binding is a wheel tree beside the native libraries.
    use_runtime_packages(request.get("runtime"))
    from services.local_asr.transcribe_engine import GpuUnavailable, TranscribeRecognizer
    try:
        state.engine = TranscribeRecognizer(request["runtime"], request["model_path"], state.device)
    except GpuUnavailable as exc:
        # Auto retries on the CPU, as for a GPU Qwen's PyTorch can't run.
        raise UnsupportedGpu(str(exc)) from exc
    state.device = state.engine.device


def load_qwen(state, request):
    use_runtime_packages(request.get("runtime"))
    import torch
    from qwen_asr import Qwen3ASRModel
    if state.device == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("CUDA is unavailable for Qwen. Select CPU or install a compatible NVIDIA driver.")
    if state.device == "cuda":
        check_qwen_gpu(torch)
    if state.device == "mps" and not torch.backends.mps.is_available():
        state.device = "cpu"
    device = state.device
    # -1 makes each transcribe call one batch, of the windows
    # it is given: one, or qwen_batch's choice.
    state.engine = Qwen3ASRModel.from_pretrained(
        request["model_path"], device_map=device,
        dtype=torch.float16 if device in ("cuda", "mps") else torch.float32,
        max_inference_batch_size=-1, max_new_tokens=2048,
    )
    if device == "mps":
        use_eager_attention(state.engine.model)
    limit_qwen_output(state.engine.model)


def use_eager_attention(model):
    # torch 2.6's MPS matmul cannot broadcast grouped-query
    # attention (16 query over 8 key/value heads) as SDPA
    # asks it to; eager attention repeats those heads first.
    for module in model.modules():
        config = getattr(module, "config", None)
        if config is not None:
            config._attn_implementation = "eager"


def limit_qwen_output(model):
    """Fail a decode that used up its token budget instead of returning cut-off text."""
    generate = model.generate

    def checked_generate(*args, **kwargs):
        output = generate(*args, **kwargs)
        new_tokens = output.sequences.shape[1] - kwargs["input_ids"].shape[1]
        if new_tokens >= kwargs["max_new_tokens"]:
            raise RuntimeError("Qwen reached its output limit. Try a shorter audio selection.")
        return output
    model.generate = checked_generate


def read_samples(request):
    samples = array.array("f")
    if request.get("audio_path"):
        with open(request["audio_path"], "rb") as audio:
            samples.frombytes(audio.read())
    return samples


def transcribe(state, op, request):
    """A "transcribe" or "stream" request's result."""
    if state.engine is None:
        raise RuntimeError("No model loaded")
    samples = read_samples(request)
    language = request.get("language")
    if state.family in NATIVE_FAMILIES:
        return transcribe_native(state, op, request, samples)
    return transcribe_qwen(state.engine, samples, language)


def transcribe_native(state, op, request, samples):
    engine, family = state.engine, state.family
    language = native_language_code(family, request.get("language"))
    phrases = boost_phrases(family, request.get("phrases"))
    if op == "stream":
        boost = {"phrases": phrases} if phrases else {}
        events = engine.stream(request["session"], samples, language, request.get("finish", False), **boost)
        return {"events": events}
    if not phrases:
        return engine.transcribe(samples, language)
    try:
        return engine.transcribe(samples, language, phrases=phrases)
    except Exception:
        # Boosting must never cost the dictation, and its
        # error may quote a phrase, which is never logged.
        print("Word boosting failed; decoding without it", file=sys.stderr)
        return engine.transcribe(samples, language)


def transcribe_qwen(engine, samples, language):
    import numpy as np
    code = qwen_language_name(language)
    audio = (np.asarray(samples, dtype=np.float32), SAMPLE_RATE)
    text = engine.transcribe(audio=audio, language=code)[0].text
    segments = [dict(text=text, start=0., end=len(samples)/SAMPLE_RATE)] if text else []
    return dict(text=text, segments=segments)


def transcribe_windows(state, request):
    """A "transcribe_batch" request's result: Qwen on CUDA decoding several windows at once."""
    if state.engine is None:
        raise RuntimeError("No model loaded")
    if state.family != "qwen_asr" or state.device != "cuda":
        raise ValueError("Only Qwen on CUDA decodes windows in batches")
    import numpy as np
    import torch
    from services.local_asr.qwen_batch import transcribe_batch

    audios = [np.fromfile(path, dtype=np.float32) for path in request["audio_paths"]]
    texts, limit = transcribe_batch(state.engine, audios, qwen_language_name(request.get("language")),
                                    torch, request["batch_size"])
    return dict(texts=texts, batch_size=limit)


def handle(state, op, request):
    """The result of one request other than "shutdown"."""
    if op == "load":
        return load_engine(state, request)
    if op in ("transcribe", "stream"):
        return transcribe(state, op, request)
    if op == "transcribe_batch":
        return transcribe_windows(state, request)
    if op == "cancel_stream":
        state.engine.cancel_stream(request["session"])
        return {}
    raise ValueError("Unknown worker operation")


def main():
    protocol = os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8", buffering=1)
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    state = WorkerState()
    for line in sys.stdin:
        request = {}
        try:
            request = json.loads(line)
            op = request["op"]
            if op == "shutdown":
                if state.engine is not None and hasattr(state.engine, "close"):
                    state.engine.close()
                return
            result = handle(state, op, request)
            response = {"id": request["id"], "result": result}
        except Exception as exc:
            traceback.print_exc(file=sys.stderr)
            response = {"id": request.get("id"), "error": str(exc)}
            if isinstance(exc, UnsupportedGpu):
                response["code"] = exc.code
        protocol.write(json.dumps(response, ensure_ascii=True) + "\n")


if __name__ == "__main__":
    main()
