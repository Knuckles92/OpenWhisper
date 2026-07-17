"""Bundled technical metadata for local Whisper models.

The Model Manager uses this catalog to explain model tradeoffs without
contacting Hugging Face.  Values are curated from the upstream OpenAI Whisper
model table, the Distil-Whisper model cards, and the CTranslate2 conversion
repositories used by faster-whisper.
"""

from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Mapping, Tuple


OPENAI_MODEL_TABLE_URL: Final[str] = (
    "https://github.com/openai/whisper#available-models-and-languages"
)
DISTIL_PAPER_URL: Final[str] = "https://arxiv.org/abs/2311.00430"
RUNTIME_FORMAT: Final[str] = "CTranslate2 conversion (FP16 weights)"
LICENSE: Final[str] = "MIT"


@dataclass(frozen=True)
class ModelDetails:
    """Immutable user-facing metadata for one local Whisper model."""

    model_name: str
    description: str
    origin_name: str
    origin_url: str
    repository_id: str
    repository_url: str
    maintainer: str
    family: str
    language_support: str
    task_support: str
    parameter_count: str
    relative_performance: str
    memory_guidance: str
    download_size_mb: int
    runtime_format: str
    license: str
    best_for: str
    limitations: Tuple[str, ...]
    source_urls: Tuple[str, ...]

    @property
    def download_size(self) -> str:
        """Return the approximate download size in a compact format."""
        if self.download_size_mb >= 1000:
            return f"~{self.download_size_mb / 1000:.1f} GB"
        return f"~{self.download_size_mb} MB"

    @property
    def compact_tags(self) -> str:
        """Return the compact language/family summary used in the dialog."""
        parts = [self.language_support]
        if self.family == "Distil-Whisper":
            parts.append("Distilled")
        return " / ".join(parts)


def _standard_model(
    model_name: str,
    *,
    description: str,
    parameters: str,
    relative_speed: str,
    required_vram: str,
    download_size_mb: int,
    best_for: str,
    limitations: Tuple[str, ...],
    repository_id: str | None = None,
    origin_name: str | None = None,
    repository_url: str | None = None,
    maintainer: str = "Systran",
) -> ModelDetails:
    """Build metadata shared by the original OpenAI Whisper family."""
    english_only = model_name.endswith(".en")
    upstream_name = origin_name or f"openai/whisper-{model_name}"
    repo_id = repository_id or f"Systran/faster-whisper-{model_name}"
    repo_url = repository_url or f"https://huggingface.co/{repo_id}"
    is_turbo = model_name == "turbo"
    if english_only:
        task_support = "English speech transcription"
        language_support = "English only"
    elif is_turbo:
        task_support = "Multilingual transcription and language identification"
        language_support = "Multilingual"
    else:
        task_support = (
            "Multilingual transcription, translation to English, and "
            "language identification"
        )
        language_support = "Multilingual"

    return ModelDetails(
        model_name=model_name,
        description=description,
        origin_name=upstream_name,
        origin_url=f"https://huggingface.co/{upstream_name}",
        repository_id=repo_id,
        repository_url=repo_url,
        maintainer=maintainer,
        family="OpenAI Whisper",
        language_support=language_support,
        task_support=task_support,
        parameter_count=parameters,
        relative_performance=(
            f"{relative_speed} vs. large on an NVIDIA A100 for English "
            "transcription; real-world speed varies by hardware and audio."
        ),
        memory_guidance=(
            f"About {required_vram} VRAM in the upstream reference table; "
            "faster-whisper usage varies with compute type and device."
        ),
        download_size_mb=download_size_mb,
        runtime_format=RUNTIME_FORMAT,
        license=LICENSE,
        best_for=best_for,
        limitations=limitations,
        source_urls=(repo_url, f"https://huggingface.co/{upstream_name}", OPENAI_MODEL_TABLE_URL),
    )


def _distilled_model(
    model_name: str,
    *,
    description: str,
    origin_name: str,
    parameters: str,
    relative_latency: str,
    download_size_mb: int,
    best_for: str,
    limitations: Tuple[str, ...],
) -> ModelDetails:
    """Build metadata shared by English-only Distil-Whisper models."""
    repo_id = f"Systran/faster-distil-whisper-{model_name.removeprefix('distil-')}"
    repo_url = f"https://huggingface.co/{repo_id}"
    origin_url = f"https://huggingface.co/{origin_name}"
    return ModelDetails(
        model_name=model_name,
        description=description,
        origin_name=origin_name,
        origin_url=origin_url,
        repository_id=repo_id,
        repository_url=repo_url,
        maintainer="Systran (conversion); Hugging Face Distil-Whisper (upstream)",
        family="Distil-Whisper",
        language_support="English only",
        task_support="English speech transcription",
        parameter_count=parameters,
        relative_performance=(
            f"{relative_latency} published relative latency vs. Whisper "
            "large-v3; results depend on hardware, decoding, and audio."
        ),
        memory_guidance=(
            "Smaller distilled checkpoint; actual RAM and VRAM usage depends "
            "on faster-whisper compute type and device."
        ),
        download_size_mb=download_size_mb,
        runtime_format=RUNTIME_FORMAT,
        license=LICENSE,
        best_for=best_for,
        limitations=limitations,
        source_urls=(repo_url, origin_url, DISTIL_PAPER_URL),
    )


_CATALOG = {
    "tiny": _standard_model(
        "tiny",
        description="The smallest multilingual Whisper model, optimized for very fast transcription and minimal resource use.",
        parameters="39 million",
        relative_speed="~10x",
        required_vram="1 GB",
        download_size_mb=76,
        best_for="Fast drafts, lightweight devices, and workflows where responsiveness matters more than maximum accuracy.",
        limitations=(
            "Lowest accuracy in the standard Whisper family, especially with noise, accents, or specialized vocabulary.",
            "Multilingual quality varies significantly by language.",
        ),
    ),
    "tiny.en": _standard_model(
        "tiny.en",
        description="The English-only tiny Whisper model, tuned for fast and lightweight English transcription.",
        parameters="39 million",
        relative_speed="~10x",
        required_vram="1 GB",
        download_size_mb=76,
        best_for="Quick English drafts on resource-constrained systems.",
        limitations=(
            "English speech only.",
            "Lower accuracy than larger models on difficult or noisy audio.",
        ),
    ),
    "base": _standard_model(
        "base",
        description="A compact multilingual model that improves accuracy over tiny while remaining suitable for CPU use.",
        parameters="74 million",
        relative_speed="~7x",
        required_vram="1 GB",
        download_size_mb=145,
        best_for="General-purpose multilingual transcription on CPUs and modest hardware.",
        limitations=(
            "Less accurate than small and larger models on challenging audio.",
            "Language performance varies across the multilingual training set.",
        ),
    ),
    "base.en": _standard_model(
        "base.en",
        description="An English-only compact model offering a practical balance of CPU speed and transcription quality.",
        parameters="74 million",
        relative_speed="~7x",
        required_vram="1 GB",
        download_size_mb=145,
        best_for="Everyday English dictation and transcription on CPU-focused systems.",
        limitations=(
            "English speech only.",
            "May struggle with heavy noise, overlapping speakers, or uncommon terminology.",
        ),
    ),
    "small": _standard_model(
        "small",
        description="A mid-sized multilingual Whisper model with a stronger accuracy and speed balance than tiny or base.",
        parameters="244 million",
        relative_speed="~4x",
        required_vram="2 GB",
        download_size_mb=484,
        best_for="Higher-quality multilingual transcription where moderate compute use is acceptable.",
        limitations=(
            "Slower and more memory-intensive than tiny or base.",
            "Still trails medium and large models on difficult audio.",
        ),
    ),
    "small.en": _standard_model(
        "small.en",
        description="The English-only small model, providing stronger recognition than compact variants at moderate cost.",
        parameters="244 million",
        relative_speed="~4x",
        required_vram="2 GB",
        download_size_mb=484,
        best_for="Reliable English transcription when base-class accuracy is not sufficient.",
        limitations=(
            "English speech only.",
            "Requires more memory and processing time than tiny or base.",
        ),
    ),
    "medium": _standard_model(
        "medium",
        description="A high-accuracy multilingual model that approaches large-model quality with lower resource requirements.",
        parameters="769 million",
        relative_speed="~2x",
        required_vram="5 GB",
        download_size_mb=1530,
        best_for="Accuracy-focused multilingual transcription and translation on capable hardware.",
        limitations=(
            "Substantially slower and larger than small or base.",
            "CPU transcription may be slow for interactive workflows.",
        ),
    ),
    "medium.en": _standard_model(
        "medium.en",
        description="The high-capacity English-only medium model for accuracy-focused transcription.",
        parameters="769 million",
        relative_speed="~2x",
        required_vram="5 GB",
        download_size_mb=1530,
        best_for="High-quality English transcription on systems with ample memory or GPU acceleration.",
        limitations=(
            "English speech only.",
            "Large download and relatively high compute requirements.",
        ),
    ),
    "large-v1": _standard_model(
        "large-v1",
        description="The first-generation full-size multilingual Whisper checkpoint, retained for compatibility and comparison.",
        parameters="1.55 billion",
        relative_speed="1x",
        required_vram="10 GB",
        download_size_mb=3090,
        best_for="Reproducing workflows that specifically depend on the original large checkpoint.",
        limitations=(
            "Older and generally less capable than later large revisions.",
            "Highest resource class and slow reference speed.",
        ),
    ),
    "large-v2": _standard_model(
        "large-v2",
        description="The second-generation full-size multilingual Whisper model with improvements over large-v1.",
        parameters="1.55 billion",
        relative_speed="1x",
        required_vram="10 GB",
        download_size_mb=3090,
        best_for="High-accuracy multilingual transcription when compatibility with the v2 checkpoint matters.",
        limitations=(
            "Superseded by large-v3 for most new accuracy-focused use cases.",
            "Large download and high memory requirements.",
        ),
    ),
    "large-v3": _standard_model(
        "large-v3",
        description="The third-generation full-size multilingual Whisper model and the most capable standard checkpoint in this catalog.",
        parameters="1.55 billion",
        relative_speed="1x",
        required_vram="10 GB",
        download_size_mb=3090,
        best_for="Maximum multilingual transcription and translation quality when compute resources permit.",
        limitations=(
            "Largest download and highest memory requirement in the catalog.",
            "Often too slow for interactive CPU-only transcription.",
        ),
    ),
    "turbo": _standard_model(
        "turbo",
        description="An optimized large-v3 variant designed for much faster multilingual transcription with minimal accuracy loss.",
        parameters="809 million",
        relative_speed="~8x",
        required_vram="6 GB",
        download_size_mb=1620,
        best_for="Fast, high-quality multilingual transcription on a GPU; OpenWhisper's automatic GPU choice.",
        limitations=(
            "Not trained for speech translation to English; use a standard multilingual model for translation.",
            "Heavier than compact models and best suited to GPU acceleration.",
        ),
        origin_name="openai/whisper-large-v3-turbo",
        repository_id="mobiuslabsgmbh/faster-whisper-large-v3-turbo",
        repository_url="https://huggingface.co/dropbox-dash/faster-whisper-large-v3-turbo",
        maintainer="Dropbox Dash (formerly Mobius Labs)",
    ),
    "distil-small.en": _distilled_model(
        "distil-small.en",
        description="The smallest English Distil-Whisper checkpoint, distilled from Whisper small.en for memory-constrained use.",
        origin_name="distil-whisper/distil-small.en",
        parameters="166 million",
        relative_latency="~5.6x faster",
        download_size_mb=330,
        best_for="English transcription where a small distilled checkpoint is the primary constraint.",
        limitations=(
            "English speech recognition only; no multilingual transcription or translation.",
            "Its four decoder layers make it slower than some larger Distil-Whisper variants despite its smaller size.",
        ),
    ),
    "distil-medium.en": _distilled_model(
        "distil-medium.en",
        description="An English model distilled from Whisper medium.en, targeting strong quality with substantially lower latency.",
        origin_name="distil-whisper/distil-medium.en",
        parameters="394 million",
        relative_latency="~6.8x faster",
        download_size_mb=790,
        best_for="Fast, accurate English transcription with a balanced distilled checkpoint.",
        limitations=(
            "English speech recognition only; no multilingual transcription or translation.",
            "Published performance figures are benchmark-dependent and may not match local hardware.",
        ),
    ),
    "distil-large-v2": _distilled_model(
        "distil-large-v2",
        description="An English checkpoint distilled from Whisper large-v2 for high accuracy at much lower latency.",
        origin_name="distil-whisper/distil-large-v2",
        parameters="756 million",
        relative_latency="~5.8x faster",
        download_size_mb=1510,
        best_for="High-quality English transcription in workflows standardized on the large-v2 generation.",
        limitations=(
            "English speech recognition only; no multilingual transcription or translation.",
            "Superseded by distil-large-v3 for most new long-form workflows.",
        ),
    ),
    "distil-large-v3": _distilled_model(
        "distil-large-v3",
        description="The latest English Distil-Whisper checkpoint, distilled from large-v3 and optimized for long-form transcription.",
        origin_name="distil-whisper/distil-large-v3",
        parameters="756 million",
        relative_latency="~6.3x faster",
        download_size_mb=1510,
        best_for="Fast, high-quality English transcription, especially for long recordings.",
        limitations=(
            "English speech recognition only; no multilingual transcription or translation.",
            "Large distilled download compared with small and medium variants.",
        ),
    ),
}

MODEL_CATALOG: Final[Mapping[str, ModelDetails]] = MappingProxyType(_CATALOG)


def get_model_details(model_name: str) -> ModelDetails:
    """Return bundled metadata for a concrete faster-whisper model.

    Args:
        model_name: Concrete model name from ``config.WHISPER_MODEL_CHOICES``.

    Returns:
        Immutable technical details for ``model_name``.

    Raises:
        KeyError: If ``model_name`` is not part of the managed model catalog.
    """
    return MODEL_CATALOG[model_name]
