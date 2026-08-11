"""AMI Meeting Corpus manifest, download, and reference transcript parsing."""
from __future__ import annotations

import os
import shutil
import unicodedata
import urllib.request
import wave
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence
from xml.etree import ElementTree

ANNOTATIONS_URL = (
    "https://groups.inf.ed.ac.uk/ami/AMICorpusAnnotations/"
    "ami_public_manual_1.6.2.zip"
)
AUDIO_BASE_URL = (
    "https://groups.inf.ed.ac.uk/ami/AMICorpusMirror/amicorpus/HeadsetAudio"
)


@dataclass(frozen=True)
class MeetingSpec:
    """One public meeting selected for the dogfood suite."""

    meeting_id: str
    description: str

    @property
    def audio_filename(self) -> str:
        """Return the official AMI headset-mix filename."""
        return f"{self.meeting_id}.Mix-Headset.wav"

    @property
    def audio_url(self) -> str:
        """Return the official AMI audio URL."""
        return f"{AUDIO_BASE_URL}/{self.audio_filename}"


# These are naturally occurring, non-scenario research meetings rather than
# the scripted/read speech used by many ASR smoke tests. The set deliberately
# includes strong accents, specialist vocabulary, interruptions, and overlap.
DEFAULT_MEETINGS: tuple[MeetingSpec, ...] = (
    MeetingSpec("IN1001", "Web-based video browsing; strong French accents"),
    MeetingSpec("IN1002", "Posterior probability methods"),
    MeetingSpec("IN1005", "PLSA methods for indexing web pages"),
    MeetingSpec("IN1007", "Keyword spotting and spectral transforms"),
    MeetingSpec("IN1008", "Audio-based interactive tables; French accents"),
    MeetingSpec("IN1009", "Audio-based interactive tables; French accents"),
    MeetingSpec("IN1012", "Speech researchers discuss Interspeech 2005"),
    MeetingSpec("IN1013", "Approaches for coding spectral information"),
    MeetingSpec("IN1014", "Conference-room recording equipment"),
    MeetingSpec("IN1016", "Slide content for speech recognition"),
)


@dataclass(frozen=True)
class ReferenceWord:
    """One manually transcribed AMI word with meeting-clock timing."""

    text: str
    start_s: float
    end_s: float
    speaker: str


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_reference_words(
    annotations_dir: Path, meeting_id: str
) -> list[ReferenceWord]:
    """Read and chronologically merge a meeting's manual speaker transcripts.

    Args:
        annotations_dir: Extracted AMI manual-annotation root.
        meeting_id: AMI meeting identifier, for example ``IN1002``.

    Returns:
        Timed lexical word tokens. Punctuation and non-lexical events such as
        laughs, coughs, gaps, and disfluency markers are excluded.

    Raises:
        FileNotFoundError: If no speaker word files exist for the meeting.
        ValueError: If the files contain no usable timed word tokens.
    """
    word_dir = Path(annotations_dir) / "words"
    paths = sorted(word_dir.glob(f"{meeting_id}.*.words.xml"))
    if not paths:
        raise FileNotFoundError(
            f"No AMI word annotations found for {meeting_id} in {word_dir}"
        )

    words: list[tuple[ReferenceWord, int]] = []
    ordinal = 0
    for path in paths:
        parts = path.name.split(".")
        speaker = parts[1] if len(parts) >= 3 else path.stem
        root = ElementTree.parse(path).getroot()
        for node in root.iter():
            if _local_name(node.tag) != "w" or node.get("punc") == "true":
                continue
            text = unicodedata.normalize("NFKC", node.text or "").strip()
            if not text:
                continue
            try:
                start_s = float(node.attrib["starttime"])
                end_s = float(node.attrib["endtime"])
            except (KeyError, TypeError, ValueError):
                continue
            words.append((ReferenceWord(text, start_s, end_s, speaker), ordinal))
            ordinal += 1

    # Timing ties happen for overlap and zero-duration tokens. Preserve source
    # order as the final key so repeated runs remain byte-for-byte comparable.
    words.sort(key=lambda item: (item[0].start_s, item[0].end_s, item[1]))
    result = [item[0] for item in words]
    if not result:
        raise ValueError(f"No usable AMI reference words found for {meeting_id}")
    return result


def annotation_root(data_dir: Path) -> Path:
    """Return the extracted manual-annotation root below ``data_dir``."""
    return Path(data_dir) / "annotations"


def audio_path(data_dir: Path, meeting_id: str) -> Path:
    """Return the expected local AMI audio path."""
    return Path(data_dir) / "audio" / f"{meeting_id}.Mix-Headset.wav"


def _download(
    url: str,
    destination: Path,
    progress: Callable[[str], None] = print,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and destination.stat().st_size > 0:
        return
    progress(f"Downloading {url}")
    temp_path = destination.with_suffix(destination.suffix + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": "OpenWhisper/benchmark"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response, temp_path.open(
            "wb"
        ) as output:
            shutil.copyfileobj(response, output, length=1024 * 1024)
        os.replace(temp_path, destination)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def _valid_wav(path: Path) -> bool:
    try:
        with wave.open(str(path), "rb") as wav_file:
            return (
                wav_file.getnchannels() >= 1
                and wav_file.getsampwidth() == 2
                and wav_file.getnframes() > 0
            )
    except (OSError, EOFError, wave.Error):
        return False


def prepare_corpus(
    data_dir: Path,
    meetings: Sequence[MeetingSpec] = DEFAULT_MEETINGS,
    progress: Callable[[str], None] = print,
) -> None:
    """Download and validate manual annotations plus selected meeting audio.

    Args:
        data_dir: Local AMI benchmark data directory.
        meetings: Meetings whose headset-mix WAV files should be present.
        progress: Human-readable progress callback.
    """
    data_dir = Path(data_dir)
    archive = data_dir / "ami_public_manual_1.6.2.zip"
    root = annotation_root(data_dir)
    if not (root / "words").is_dir():
        _download(ANNOTATIONS_URL, archive, progress)
        progress(f"Extracting {archive}")
        root.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(root)

    def prepare_meeting(spec: MeetingSpec) -> str:
        destination = audio_path(data_dir, spec.meeting_id)
        if destination.exists() and not _valid_wav(destination):
            destination.unlink()
        _download(spec.audio_url, destination, progress)
        if not _valid_wav(destination):
            raise ValueError(f"Downloaded audio is not a valid PCM WAV: {destination}")
        # Parse now so a missing/corrupt reference fails before a long model run.
        parse_reference_words(root, spec.meeting_id)
        return spec.meeting_id

    # The upstream mirror commonly limits each connection to roughly 1 MB/s.
    # Four bounded transfers keep preparation reasonable without hammering the
    # public research server or allowing unbounded disk/network concurrency.
    with ThreadPoolExecutor(max_workers=min(4, max(1, len(meetings)))) as pool:
        futures = [pool.submit(prepare_meeting, spec) for spec in meetings]
        for future in as_completed(futures):
            progress(f"Ready: {future.result()}")


def select_meetings(ids: Iterable[str]) -> tuple[MeetingSpec, ...]:
    """Resolve meeting ids against the curated manifest."""
    requested = [item.strip().upper() for item in ids if item.strip()]
    if not requested:
        return DEFAULT_MEETINGS
    by_id = {item.meeting_id: item for item in DEFAULT_MEETINGS}
    unknown = [item for item in requested if item not in by_id]
    if unknown:
        raise ValueError(f"Unknown curated AMI meeting id(s): {', '.join(unknown)}")
    return tuple(by_id[item] for item in requested)
