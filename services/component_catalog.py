"""Bundled user-facing profiles for optional downloadable components.

The Downloads window uses this catalog to explain what a component is and
where it comes from, without contacting the network.  Install URLs, sizes,
and SHA-256 pins stay in ``services.components`` — this module is copy and
links only.
"""

from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Mapping, Tuple


NVIDIA_CUBLAS_URL: Final[str] = "https://developer.nvidia.com/cublas"
NVIDIA_PYPI_URL: Final[str] = "https://pypi.org/project/nvidia-cublas-cu12/"
PI_HOME_URL: Final[str] = "https://pi.dev"
NODEJS_URL: Final[str] = "https://nodejs.org"
WESPEAKER_REPO_URL: Final[str] = "https://github.com/wenet-e2e/wespeaker"
WESPEAKER_MODEL_URL: Final[str] = (
    "https://huggingface.co/Wespeaker/wespeaker-voxceleb-resnet34-LM"
)


@dataclass(frozen=True)
class ComponentDetails:
    """Immutable user-facing metadata for one downloadable component."""

    component_id: str
    display_name: str
    summary: str
    description: str
    origin_name: str
    origin_url: str
    origin_label: str
    source_name: str
    source_url: str
    source_label: str
    maintainer: str
    family: str
    requires: str
    payload: str
    local_format: str
    license: str
    best_for: str
    limitations: Tuple[str, ...]
    compact_tags: str
    source_note: str
    source_urls: Tuple[str, ...]


_SOURCE_NOTE: Final[str] = (
    "Download URLs and SHA-256 pins ship with the app. Archives are verified "
    "before extract, and an interruption leaves either the previous install "
    "or the new one — never a mixture."
)

_CATALOG: dict[str, ComponentDetails] = {
    "gpu-accel": ComponentDetails(
        component_id="gpu-accel",
        display_name="GPU Acceleration",
        summary=(
            "NVIDIA CUDA runtime (cuBLAS) for 2-4x faster local transcription. "
            "Requires an NVIDIA graphics card."
        ),
        description=(
            "NVIDIA CUDA 12 libraries that faster-whisper's engine "
            "(CTranslate2) loads for GPU inference: cuBLAS, NVRTC, and the "
            "CUDA runtime. OpenWhisper downloads the official NVIDIA PyPI "
            "wheels and extracts only the DLLs — you do not need the CUDA "
            "Toolkit installer. The driver on the machine must already "
            "provide CUDA 12 (version 525 or newer)."
        ),
        origin_name="NVIDIA CUDA / cuBLAS",
        origin_url=NVIDIA_CUBLAS_URL,
        origin_label="NVIDIA ↗",
        source_name="PyPI NVIDIA CUDA 12 wheels",
        source_url=NVIDIA_PYPI_URL,
        source_label="PyPI ↗",
        maintainer="NVIDIA (wheels); OpenWhisper extracts the runtime DLLs",
        family="CUDA runtime",
        requires="NVIDIA GPU and a CUDA 12 driver (525+). No CUDA Toolkit.",
        payload="cuBLAS 12.9, NVRTC, and the CUDA 12 runtime",
        local_format="Windows CUDA DLLs in the component folder",
        license="NVIDIA CUDA license (binary libraries)",
        best_for=(
            "Windows machines with an NVIDIA GPU that want local Whisper "
            "transcription two to four times faster than CPU."
        ),
        limitations=(
            "Requires a compatible NVIDIA GPU and driver; AMD and Intel "
            "graphics are not supported.",
            "About 633 MB to download and 959 MB installed.",
            "cuDNN is not included — CTranslate2 4.8 does not load it.",
            "Pascal and earlier cards run in int8 rather than float16.",
        ),
        compact_tags="NVIDIA CUDA",
        source_note=_SOURCE_NOTE,
        source_urls=(NVIDIA_CUBLAS_URL, NVIDIA_PYPI_URL),
    ),
    "meeting-agent": ComponentDetails(
        component_id="meeting-agent",
        display_name="Meeting Intelligence Agent",
        summary=(
            "Node runtime plus the Pi agent that maintains live meeting "
            "insights (key points, decisions, action items) during Meeting "
            "Mode. Requires an OpenRouter API key."
        ),
        description=(
            "A portable Node.js 22 LTS runtime plus the OpenWhisper sidecar "
            "built around the Pi coding agent. In Meeting Mode the agent "
            "maintains live insights — key points, decisions, and action "
            "items — with meeting-state-only tools. It cannot run a shell, "
            "touch the filesystem, or open its own network connections. "
            "An OpenRouter API key is required while the agent is running. "
            "The shipped Direct agent still works without this component."
        ),
        origin_name="Pi coding agent",
        origin_url=PI_HOME_URL,
        origin_label="Pi ↗",
        source_name="Node.js 22 LTS (nodejs.org) plus the OpenWhisper sidecar zip",
        source_url=NODEJS_URL,
        source_label="Node.js ↗",
        maintainer="OpenWhisper (sidecar); Pi (agent SDK); Node.js project",
        family="Pi sidecar",
        requires="An OpenRouter API key. Used by Meeting Mode.",
        payload=(
            "Portable Node 22 (node.exe on Windows, node on Linux) plus the "
            "Pi sidecar (bundle.cjs)"
        ),
        local_format=(
            "Flat extract: platform Node runtime and bundle.cjs side by side"
        ),
        license="Node.js MIT; Pi per its license; sidecar with OpenWhisper",
        best_for=(
            "Meeting Mode sessions that should track topics, decisions, and "
            "action items live as the conversation unfolds."
        ),
        limitations=(
            "Needs an OpenRouter API key and a network connection while "
            "the agent runs.",
            "Offered on Windows x64 and Linux x86_64/aarch64. macOS is not "
            "supported for this downloadable payload.",
            "Download size depends on the platform Node archive.",
            "Meeting Mode still works without it — the Direct agent and "
            "Me/Others labels do not depend on Pi.",
        ),
        compact_tags="Pi agent",
        source_note=_SOURCE_NOTE,
        source_urls=(PI_HOME_URL, NODEJS_URL),
    ),
    "speaker-id": ComponentDetails(
        component_id="speaker-id",
        display_name="Speaker Identification",
        summary=(
            "Speaker-embedding model (WeSpeaker ONNX) that separates remote "
            "voices into individual speakers during Meeting Mode."
        ),
        description=(
            "A WeSpeaker ResNet34-LM ONNX speaker-embedding model. During "
            "Meeting Mode it embeds remote voices so the Other channel can "
            "be split into individual speakers. This payload is listed but "
            "not offered in Downloads until the archive is published."
        ),
        origin_name="WeSpeaker",
        origin_url=WESPEAKER_REPO_URL,
        origin_label="Original ↗",
        source_name="Wespeaker/wespeaker-voxceleb-resnet34-LM",
        source_url=WESPEAKER_MODEL_URL,
        source_label="Hugging Face ↗",
        maintainer="WeSpeaker (model); OpenWhisper (Meeting Mode integration)",
        family="Speaker embeddings",
        requires="Meeting Mode. Local ONNX runtime.",
        payload="WeSpeaker ResNet34-LM ONNX embedding model",
        local_format="ONNX model file in the component folder",
        license="Apache-2.0 (WeSpeaker)",
        best_for=(
            "Meetings with several remote speakers where Me/Others channel "
            "labels are not enough."
        ),
        limitations=(
            "Not published in this build — the row stays hidden until the "
            "pinned archive is ready.",
            "Adds a local ONNX model of about 26 MB.",
            "Labels are embeddings, not guaranteed identities.",
        ),
        compact_tags="ONNX",
        source_note=_SOURCE_NOTE,
        source_urls=(WESPEAKER_MODEL_URL, WESPEAKER_REPO_URL),
    ),
}

COMPONENT_CATALOG: Final[Mapping[str, ComponentDetails]] = MappingProxyType(_CATALOG)


def get_component_details(component_id: str) -> ComponentDetails:
    """Return bundled metadata, raising KeyError for unknown components."""
    return COMPONENT_CATALOG[component_id]
