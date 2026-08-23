"""Progressive speaker diarization for Meeting Mode's loopback channel.

Pipeline: pure-numpy Kaldi-style fbank features (``fbank``), an ONNX
speaker-embedding model on CPU (``embedder``), and online cosine clustering
with periodic agglomerative re-clustering (``clustering``). Everything
degrades gracefully to channel-level Me/Others labels when the model is
missing or inference fails.
"""
from meeting.diarize.clustering import OnlineDiarizer, create_diarizer
from meeting.diarize.embedder import EmbedderUnavailable, SpeakerEmbedder
from meeting.diarize.fbank import apply_cmn, compute_fbank

__all__ = [
    "OnlineDiarizer",
    "create_diarizer",
    "EmbedderUnavailable",
    "SpeakerEmbedder",
    "apply_cmn",
    "compute_fbank",
]
