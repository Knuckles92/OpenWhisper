"""ONNX speaker-embedding wrapper for diarization.

Runs a WeSpeaker-family speaker-verification model with onnxruntime on CPU.
onnxruntime is imported lazily so this module can be imported (and the rest
of Meeting Mode can run) on machines without the dependency; every
onnxruntime failure is normalized into ``EmbedderUnavailable`` so callers
have a single error surface to degrade on.
"""
from __future__ import annotations

import logging
from typing import Optional

import numpy as np

from meeting.diarize.fbank import SAMPLE_RATE, apply_cmn, compute_fbank

logger = logging.getLogger(__name__)

#: Minimum audio length for a usable embedding; shorter segments return None.
MIN_AUDIO_SECONDS = 0.8


class EmbedderUnavailable(Exception):
    """Raised when the ONNX runtime or model cannot produce embeddings."""


class SpeakerEmbedder:
    """Computes L2-normalized speaker embeddings from 16 kHz mono audio."""

    def __init__(self, model_path: str) -> None:
        """Load the ONNX model on the CPU execution provider.

        Args:
            model_path: Path to the speaker-embedding ONNX model file.

        Raises:
            EmbedderUnavailable: onnxruntime is missing, or the model failed
                to load.
        """
        self.model_path = model_path
        self._session = None
        self._input_name: Optional[str] = None
        try:
            import onnxruntime  # imported lazily; optional dependency

            options = onnxruntime.SessionOptions()
            options.inter_op_num_threads = 1
            options.intra_op_num_threads = 1
            self._session = onnxruntime.InferenceSession(
                model_path,
                sess_options=options,
                providers=["CPUExecutionProvider"],
            )
            self._input_name = self._session.get_inputs()[0].name
        except Exception as exc:
            raise EmbedderUnavailable(
                f"Failed to load speaker embedding model '{model_path}': {exc}"
            ) from exc
        logger.info("Speaker embedder loaded: %s (input '%s')",
                    model_path, self._input_name)

    @property
    def available(self) -> bool:
        """True when the ONNX session is loaded and usable."""
        return self._session is not None

    def embed(self, audio_f32_16k: np.ndarray) -> Optional[np.ndarray]:
        """Compute one speaker embedding for a segment of audio.

        Pipeline: 80-dim log-mel fbank -> cepstral mean normalization ->
        batch dim -> ONNX session -> squeeze -> L2 normalization.

        Args:
            audio_f32_16k: 1-D float32 mono audio at 16 kHz in [-1, 1].

        Returns:
            Float32 embedding of shape [D], L2-normalized; None when the
            audio is shorter than ``MIN_AUDIO_SECONDS`` or yields no usable
            features.

        Raises:
            EmbedderUnavailable: The ONNX session failed (any runtime error).
        """
        if self._session is None:
            raise EmbedderUnavailable("Speaker embedder has no loaded session")

        audio = np.asarray(audio_f32_16k, dtype=np.float32).reshape(-1)
        if audio.size < int(MIN_AUDIO_SECONDS * SAMPLE_RATE):
            return None

        feats = compute_fbank(audio)
        if feats.shape[0] == 0:
            return None
        feats = apply_cmn(feats)
        batch = feats[np.newaxis, :, :].astype(np.float32)  # [1, T, 80]

        try:
            outputs = self._session.run(None, {self._input_name: batch})
        except Exception as exc:
            raise EmbedderUnavailable(
                f"Speaker embedding inference failed: {exc}"
            ) from exc

        embedding = np.asarray(outputs[0], dtype=np.float32).squeeze()
        if embedding.ndim != 1:
            embedding = embedding.reshape(-1)
        norm = float(np.linalg.norm(embedding))
        if not np.isfinite(norm) or norm < 1e-8:
            return None
        return (embedding / norm).astype(np.float32)
