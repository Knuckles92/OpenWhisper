"""Apple Silicon Parakeet adapter, imported only by the isolated speech worker."""

from __future__ import annotations

import array
import platform
import sys
from pathlib import Path


class MlxRecognizer:
    def __init__(self, runtime: str, model_path: str, device: str):
        if sys.platform != "darwin" or platform.machine().lower() not in (
            "arm64",
            "aarch64",
        ):
            raise RuntimeError("Parakeet MLX requires an Apple Silicon Mac.")
        if sys.version_info[:2] != (3, 12):
            raise RuntimeError("The Parakeet MLX runtime requires Python 3.12.")
        if device not in ("metal", "cpu"):
            raise RuntimeError("Parakeet MLX supports the Apple GPU or CPU.")
        # Complete, verified wheels live in the component, not in the GUI's
        # environment. Keep their native libraries and .pyi/data files intact.
        sys.path.insert(0, str(Path(runtime) / "site-packages"))
        import mlx.core as mx
        from parakeet_mlx import from_pretrained
        from parakeet_mlx.audio import get_logmel

        if device == "metal" and not mx.metal.is_available():
            raise RuntimeError(
                "The Apple GPU is unavailable for Parakeet MLX. Select CPU."
            )
        mx.set_default_device(mx.gpu if device == "metal" else mx.cpu)
        self.mx = mx
        self.dtype = mx.bfloat16 if device == "metal" else mx.float32
        self.get_logmel = get_logmel
        # The worker sets HF_HUB_OFFLINE before importing any Hub libraries.
        # Only our pinned, verified local config and weights are loaded here.
        self.model = from_pretrained(str(Path(model_path).resolve()), dtype=self.dtype)
        if self.model.preprocessor_config.sample_rate != 16000:
            raise RuntimeError("Parakeet MLX requires a 16 kHz model.")

    def transcribe(self, samples: array.array, language=None):
        if not samples:
            return dict(text="", segments=[])
        # Audio arrives already resampled to 16 kHz. Use the low-level array
        # API so dictation needs no ffmpeg executable or extra audio file.
        audio = self.mx.array(samples.tolist(), dtype=self.dtype)
        minimum = self.model.preprocessor_config.hop_length
        if len(samples) < minimum:
            audio = self.mx.pad(audio, [(0, minimum - len(samples))])
        mel = self.get_logmel(audio, self.model.preprocessor_config)
        result = self.model.generate(mel)[0]
        duration = len(samples) / 16000
        segments = [
            dict(
                text=s.text,
                start=max(0.0, min(float(s.start), duration)),
                end=max(0.0, min(float(s.end), duration)),
            )
            for s in result.sentences
            if s.text.strip()
        ]
        if result.text.strip() and not segments:
            segments = [dict(text=result.text, start=0.0, end=duration)]
        return dict(text=result.text, segments=segments)

    def close(self):
        self.model = None
        self.mx.clear_cache()
