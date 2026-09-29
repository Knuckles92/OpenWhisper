"""Bundled user-facing profiles for optional downloadable components.

Settings → Downloads uses this catalog to explain what a component is and
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
            "wheels and extracts only the libraries — you do not need the "
            "CUDA Toolkit installer. The driver on the machine must already "
            "provide CUDA 12 (version 525 or newer)."
        ),
        origin_name="NVIDIA CUDA / cuBLAS",
        origin_url=NVIDIA_CUBLAS_URL,
        origin_label="NVIDIA ↗",
        source_name="PyPI NVIDIA CUDA 12 wheels",
        source_url=NVIDIA_PYPI_URL,
        source_label="PyPI ↗",
        maintainer="NVIDIA (wheels); OpenWhisper extracts the runtime libraries",
        family="CUDA runtime",
        requires="NVIDIA GPU and a CUDA 12 driver (525+). No CUDA Toolkit.",
        payload="cuBLAS 12.9, NVRTC, and the CUDA 12 runtime",
        local_format=(
            "CUDA libraries in the component folder (DLLs on Windows, "
            "shared objects on Linux)"
        ),
        license="NVIDIA CUDA license (binary libraries)",
        best_for=(
            "Windows and Linux computers with an NVIDIA GPU that want local "
            "Whisper transcription two to four times faster than CPU."
        ),
        limitations=(
            "Requires a compatible NVIDIA GPU and driver; AMD and Intel "
            "graphics are not supported.",
            "About 633 MB to download and 959 MB installed on Windows; "
            "674 MB and 1.1 GB on Linux x86_64.",
            "cuDNN is not included — CTranslate2 4.8 does not load it.",
            "Cards without float16 (GTX 10 series and older) run int8, which "
            "also needs about half the GPU memory.",
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
    "meeting-agent-opencode": ComponentDetails(
        component_id="meeting-agent-opencode",
        display_name="OpenCode v2",
        summary=(
            "An alternative meeting agent core, built on the OpenCode v2 SDK, that uses "
            "your selected text provider and model."
        ),
        description=(
            "A portable Bun runtime plus the OpenCode v2 SDK, embedded in a supervised "
            "sidecar. Selected as the agent core in Models → Meeting, it maintains meeting "
            "cards, notes, questions, and final reports. It uses the provider, model, and "
            "credentials configured in OpenWhisper, and needs no OpenCode account or CLI. "
            "Only the five meeting tools are exposed to the model; OpenCode's own shell, "
            "file, and web tools are removed. Pi remains the default agent core."
        ),
        origin_name="OpenCode",
        origin_url="https://opencode.ai/v2/docs/build/sdk/",
        origin_label="OpenCode ↗",
        source_name="OpenWhisper component release",
        source_url="https://github.com/Knuckles92/OpenWhisper/releases",
        source_label="GitHub ↗",
        maintainer="OpenWhisper (integration); OpenCode (SDK); Bun (runtime)",
        family="Meeting intelligence",
        requires="A configured text model with tool support. Used by Meeting Mode.",
        payload="Portable Bun runtime, OpenCode v2 SDK, and the meeting sidecar",
        local_format="A self-contained component folder",
        license="Bun and OpenCode MIT; dependency notices included in the download",
        best_for="Meeting Mode with OpenCode's agent loop instead of Pi's.",
        limitations=(
            "The app pins a tested OpenCode SDK and Bun version; updates arrive as new "
            "component versions.",
            "Offered on Windows x64 and Linux x86_64/aarch64. macOS is not supported.",
            "Larger than the Pi agent: up to 130 MB to download and 470 MB installed.",
            "If the engine fails, recording continues and meeting intelligence is marked "
            "unavailable; it does not switch to another agent core.",
            "Finish active meeting and report jobs before updating or removing this component.",
        ),
        compact_tags="OpenCode v2 agent",
        source_note=_SOURCE_NOTE,
        source_urls=("https://opencode.ai/v2/docs/build/sdk/", "https://bun.sh"),
    ),
}

for _id, _name, _source, _license, _description in (
    ("asr-nvidia-cpu", "NVIDIA Speech CPU", "https://github.com/NVIDIA/NeMo-Speech.cpp", "Apache-2.0; Python PSF", "CPU runtime shared by Parakeet and Nemotron."),
    ("asr-nvidia-cuda", "NVIDIA Speech GPU", "https://github.com/NVIDIA/NeMo-Speech.cpp", "Apache-2.0; NVIDIA CUDA; Python PSF", "NVIDIA GPU runtime shared by Parakeet and Nemotron."),
    ("asr-nvidia-vulkan", "NVIDIA Speech GPU (Vulkan)", "https://github.com/NVIDIA/NeMo-Speech.cpp", "Apache-2.0", "Vulkan GPU runtime shared by Parakeet and Nemotron, for NVIDIA GPUs older than Turing, such as the GTX 10 series."),
    ("asr-qwen", "Qwen3-ASR runtime", "https://github.com/QwenLM/Qwen3-ASR", "Apache-2.0 and bundled dependency licenses", "Isolated Python, PyTorch CUDA 12.4, and Qwen3-ASR. Also supports CPU."),
    ("asr-moonshine", "Moonshine runtime", "https://github.com/moonshine-ai/moonshine", "MIT and bundled dependency licenses", "Isolated Moonshine Voice runtime for CPU transcription."),
):
    _CATALOG[_id] = ComponentDetails(
        component_id=_id, display_name=_name, summary=_description,
        description=_description + " Models download separately. Runs in a dedicated process.",
        origin_name=_name, origin_url=_source, origin_label="Project",
        source_name="Pinned upstream archives", source_url=_source, source_label="Project",
        maintainer="Upstream publishers; packaged by OpenWhisper", family="Speech runtime",
        requires=(
            "Windows x64, Linux x86_64 (glibc 2.31+), or Apple Silicon macOS" if _id == "asr-nvidia-cpu"
            else "Windows x64 or Linux x86_64 (glibc 2.31+), and an NVIDIA GPU (Turing or newer) with its driver" if _id == "asr-nvidia-cuda"
            else "Linux x86_64 (glibc 2.31+), and an NVIDIA GPU older than Turing with its driver and the Vulkan loader" if _id == "asr-nvidia-vulkan"
            else "Windows x64"
        ),
        payload=(
            "Verified native libraries" if _id == "asr-nvidia-vulkan"
            else "Verified native libraries; portable Python on Windows" if _id.startswith("asr-nvidia")
            else "Portable Python 3.12 and verified runtime binaries"
        ),
        local_format="Isolated worker process", license=_license,
        best_for=_description, limitations=("Model weights are a separate download.",),
        compact_tags="Local speech", source_note=_SOURCE_NOTE, source_urls=(_source, "https://www.python.org/downloads/release/python-31210/"),
    )
COMPONENT_CATALOG: Final[Mapping[str, ComponentDetails]] = MappingProxyType(_CATALOG)


def get_component_details(component_id: str) -> ComponentDetails:
    """Return bundled metadata, raising KeyError for unknown components."""
    return COMPONENT_CATALOG[component_id]
