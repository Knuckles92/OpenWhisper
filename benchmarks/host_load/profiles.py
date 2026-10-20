"""Transparent service-time models, not predictions of particular GPUs."""
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Profile:
    family: str
    model: str
    intercept_s: float
    seconds_per_audio_second: float
    evidence: str

    def metadata(self):
        return asdict(self)


PROFILES = {
    "gpu-parakeet": Profile("parakeet", "parakeet-v3", .012, .0063,
        "Calibrated approximation: owner reported 70.7 s/462 ms and 3 s/31 ms; "
        "other 70.7 s/400 ms and 83 s/563 ms observations show variation. Not measured here."),
    "cpu-parakeet": Profile("parakeet", "parakeet-v3", .030, .247,
        "Calibrated approximation: owner reported 8.8 s/2.2 s on laptop CPU. "
        "One point cannot identify overhead and slope; intercept is assumed."),
    "gpu-whisper-turbo": Profile("local_whisper", "turbo", .070, .040,
        "Hypothetical sensitivity profile ONLY; no supplied Whisper calibration."),
    "cpu-whisper-small": Profile("local_whisper", "small", .150, .600,
        "Hypothetical sensitivity profile ONLY; no supplied Whisper calibration."),
}
